from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

import pandas as pd
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent
AUTH_DIR = PROJECT_ROOT / "auth"
AUTH_STATE_PATH = AUTH_DIR / "1688_login_state.json"
AUTH_META_PATH = AUTH_DIR / "1688_login_state.meta.json"
LOG_DIR = PROJECT_ROOT / "logs"
SUCCESS_LOG = LOG_DIR / "success.log"
FAILED_LOG = LOG_DIR / "failed.log"
DEFAULT_LOGIN_URL = "https://login.1688.com/member/signin.htm"
DEFAULT_ORDER_URL_TEMPLATE = "https://air.1688.com/app/ctf-page/trade-order-detail/index.html?orderId={order_id}"
DEFAULT_LOGIN_WINDOW_WIDTH = 1280
DEFAULT_LOGIN_WINDOW_HEIGHT = 900
DEFAULT_AUTH_MAX_AGE_HOURS = 24.0
LOGIN_URL = DEFAULT_LOGIN_URL
ORDER_URL_TEMPLATE = DEFAULT_ORDER_URL_TEMPLATE

ORDER_COLUMN_NAMES = ["订单号", "订单编号", "order_id", "orderId", "订单ID"]

LOGIN_VERIFY_KEYWORDS = [
    "login",
    "passport",
    "请登录",
    "安全验证",
    "滑块",
    "captcha",
    "awsc",
    "baxia",
    "访问受限",
    "风险",
    "身份验证",
]

LOGIN_VERIFY_URL_KEYWORDS = [
    "login",
    "passport",
    "signin",
    "captcha",
    "awsc",
    "baxia",
    "login.taobao.com",
    "login.1688.com",
]

LOGIN_VERIFY_TEXT_KEYWORDS = [
    "请登录",
    "账号登录",
    "密码登录",
    "扫码登录",
    "手机扫码",
    "登录名",
    "安全验证",
    "身份验证",
    "滑块验证",
    "拖动滑块",
    "验证码",
    "访问受限",
    "风险验证",
    "环境存在风险",
    "captcha",
    "awsc",
    "baxia",
]

ORDER_PAGE_KEYWORDS = [
    "订单信息",
    "当前订单状态",
    "商品",
    "买家",
    "卖家",
    "实付款",
    "交易方式",
    "打印订单详情",
]

TOP_ANCHOR_WORDS = ["当前订单状态", "订单信息", "物流信息", "交易方式"]
BOTTOM_ANCHOR_WORDS = ["实付款", "打印订单详情", "订单小计", "交易快照"]

DEFAULT_FIXED_CLIP = {"x": 250, "y": 80, "width": 950, "height": 1500}
FIXED_CLIP_CONFIG = DEFAULT_FIXED_CLIP.copy()
RUN_HEADLESS = False
MANUAL_LOGIN_VIEWPORT = {
    "width": DEFAULT_LOGIN_WINDOW_WIDTH,
    "height": DEFAULT_LOGIN_WINDOW_HEIGHT,
}
ManualWaiter = Callable[[str], None]
StopChecker = Callable[[], bool]
PaymentRecorder = Callable[[Dict[str, Any]], None]


class ManualActionRequired(RuntimeError):
    """Raised when 1688 asks the user to manually log in or verify."""


class OrderPageLoadError(RuntimeError):
    """Raised when the order detail page does not look ready."""


class ScreenshotError(RuntimeError):
    """Raised when a screenshot strategy cannot locate a usable area."""


@dataclass
class BrowserContextBundle:
    browser: Any
    context: Any


def ensure_project_dirs() -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SUCCESS_LOG.touch(exist_ok=True)
    FAILED_LOG.touch(exist_ok=True)


def normalize_order_id(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip().strip("\ufeff").strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return ""

    text = text.replace("\u3000", "").strip()
    text = text.lstrip("'").strip()
    text = re.sub(r"\s+", "", text)

    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]

    if "e" in text.lower():
        try:
            number = Decimal(text)
            if number == number.to_integral_value():
                text = format(number.quantize(Decimal(1)), "f")
        except (InvalidOperation, ValueError):
            pass

    no_commas = text.replace(",", "")
    if no_commas.isdigit():
        text = no_commas

    if not re.fullmatch(r"\d{6,}", text):
        return ""

    return text


def unique_in_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def read_text_file_orders(path: Path) -> List[str]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            lines = path.read_text(encoding=encoding).splitlines()
            return unique_in_order(normalize_order_id(line) for line in lines)
        except UnicodeDecodeError as exc:
            last_error = exc

    raise ValueError(f"无法读取 txt 文件编码: {last_error}")


def read_excel_orders(path: Path) -> List[str]:
    try:
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ValueError(f"Excel 读取失败: {exc}") from exc

    normalized_columns = {str(column).strip().lower(): column for column in df.columns}
    selected_column = None
    for name in ORDER_COLUMN_NAMES:
        key = name.strip().lower()
        if key in normalized_columns:
            selected_column = normalized_columns[key]
            break

    if selected_column is None:
        available = ", ".join(str(column) for column in df.columns)
        expected = ", ".join(ORDER_COLUMN_NAMES)
        raise ValueError(f"Excel 未找到订单号字段。需要字段: {expected}；当前字段: {available}")

    return unique_in_order(normalize_order_id(value) for value in df[selected_column].tolist())


def read_orders(input_file: str | Path) -> List[str]:
    path = Path(input_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在: {path}")

    suffix = path.suffix.lower()
    if suffix == ".txt":
        orders = read_text_file_orders(path)
    elif suffix in {".xlsx", ".xls"}:
        orders = read_excel_orders(path)
    else:
        raise ValueError("输入文件只支持 .txt、.xlsx、.xls")

    if not orders:
        raise ValueError("没有读取到有效订单号")
    return orders


def build_order_url(order_id: str) -> str:
    try:
        return ORDER_URL_TEMPLATE.format(order_id=order_id, orderId=order_id)
    except KeyError as exc:
        raise ValueError("订单详情链接模板必须使用 {order_id} 或 {orderId} 作为订单号占位符") from exc


def auth_state_exists() -> bool:
    return AUTH_STATE_PATH.exists()


def auth_state_is_readable() -> bool:
    if not auth_state_exists():
        return False
    try:
        json.loads(AUTH_STATE_PATH.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def auth_state_age_hours() -> float | None:
    if not AUTH_STATE_PATH.exists():
        return None
    try:
        if AUTH_META_PATH.exists():
            meta = json.loads(AUTH_META_PATH.read_text(encoding="utf-8"))
            authenticated_at = float(meta["authenticated_at_epoch"])
        else:
            authenticated_at = AUTH_STATE_PATH.stat().st_mtime
        return max(0.0, (time.time() - authenticated_at) / 3600.0)
    except Exception:
        return None


def auth_state_is_expired(max_age_hours: float = DEFAULT_AUTH_MAX_AGE_HOURS) -> bool:
    if max_age_hours <= 0:
        return False
    age = auth_state_age_hours()
    return age is not None and age >= max_age_hours


def clear_login_state() -> None:
    AUTH_STATE_PATH.unlink(missing_ok=True)
    AUTH_META_PATH.unlink(missing_ok=True)


def save_login_state(context: Any, mark_authenticated: bool = False) -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(AUTH_STATE_PATH))
    if mark_authenticated or not AUTH_META_PATH.exists():
        AUTH_META_PATH.write_text(
            json.dumps(
                {
                    "authenticated_at_epoch": time.time(),
                    "authenticated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(f"已保存登录状态: {AUTH_STATE_PATH}")


def launch_browser(
    playwright: Any,
    headless: bool,
    window_width: int = DEFAULT_LOGIN_WINDOW_WIDTH,
    window_height: int = DEFAULT_LOGIN_WINDOW_HEIGHT,
    browser_path: str | Path | None = None,
) -> Any:
    args = []
    if not headless:
        args = [
            f"--window-size={window_width},{window_height}",
            "--start-maximized",
        ]

    errors = []
    if browser_path:
        path = Path(browser_path).expanduser()
        try:
            print(f"使用指定浏览器: {path}")
            return playwright.chromium.launch(
                executable_path=str(path),
                headless=headless,
                args=args,
            )
        except Exception as exc:
            errors.append(f"指定浏览器启动失败({path}): {exc}")
            print(errors[-1])

    for channel, label in (("chrome", "本机 Chrome"), ("msedge", "本机 Edge")):
        try:
            print(f"正在尝试启动{label}...")
            browser = playwright.chromium.launch(channel=channel, headless=headless, args=args)
            print(f"已启动{label}。")
            return browser
        except PlaywrightError as exc:
            errors.append(f"{label}不可用: {exc}")

    try:
        print("本机 Chrome/Edge 不可用，尝试启动 Playwright Chromium...")
        browser = playwright.chromium.launch(headless=headless, args=args)
        print("已启动 Playwright Chromium。")
        return browser
    except PlaywrightError as exc:
        errors.append(f"Playwright Chromium 不可用: {exc}")
        detail = "\n".join(errors)
        raise RuntimeError(
            "无法启动浏览器。请安装本机 Chrome 或 Edge，或在界面里选择 chrome.exe/msedge.exe。\n"
            + detail
        ) from exc


def first_login(
    playwright: Any,
    viewport_width: int = 1440,
    viewport_height: int = 1800,
    manual_waiter: ManualWaiter | None = None,
    login_url: str | None = None,
    headless: bool = False,
    login_window_width: int = DEFAULT_LOGIN_WINDOW_WIDTH,
    login_window_height: int = DEFAULT_LOGIN_WINDOW_HEIGHT,
    browser_path: str | Path | None = None,
) -> BrowserContextBundle:
    print("未找到可用登录状态，将打开浏览器进行首次手动登录。")
    login_browser = launch_browser(
        playwright,
        headless=False,
        window_width=login_window_width,
        window_height=login_window_height,
        browser_path=browser_path,
    )
    login_context = login_browser.new_context(
        viewport={"width": login_window_width, "height": login_window_height}
    )
    page = login_context.new_page()

    target_url = login_url or LOGIN_URL
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        print("打开 1688 登录页超时，请在浏览器中手动刷新或继续登录。")

    message = "请在浏览器中手动登录 1688。完成登录或安全验证后继续。"
    if manual_waiter:
        manual_waiter(message)
    else:
        input(message + " 完成后按回车继续...")
    save_login_state(login_context, mark_authenticated=True)
    login_browser.close()

    browser = launch_browser(
        playwright,
        headless=headless,
        window_width=min(max(viewport_width, 1000), 1600),
        window_height=min(max(viewport_height, 700), 1000),
        browser_path=browser_path,
    )
    context = browser.new_context(
        storage_state=str(AUTH_STATE_PATH),
        viewport={"width": viewport_width, "height": viewport_height},
    )
    return BrowserContextBundle(browser=browser, context=context)


def create_browser_context(
    playwright: Any,
    headless: bool = False,
    viewport_width: int = 1440,
    viewport_height: int = 1800,
    manual_waiter: ManualWaiter | None = None,
    login_url: str | None = None,
    login_window_width: int = DEFAULT_LOGIN_WINDOW_WIDTH,
    login_window_height: int = DEFAULT_LOGIN_WINDOW_HEIGHT,
    browser_path: str | Path | None = None,
    auth_max_age_hours: float = DEFAULT_AUTH_MAX_AGE_HOURS,
) -> BrowserContextBundle:
    if auth_state_exists() and auth_state_is_expired(auth_max_age_hours):
        age = auth_state_age_hours()
        print(f"登录状态已超过 {auth_max_age_hours:g} 小时（当前约 {age:.1f} 小时），需要重新登录。")
        clear_login_state()

    if not auth_state_exists():
        return first_login(
            playwright,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            manual_waiter=manual_waiter,
            login_url=login_url,
            headless=headless,
            login_window_width=login_window_width,
            login_window_height=login_window_height,
            browser_path=browser_path,
        )

    if not auth_state_is_readable():
        print("登录状态文件损坏或无法读取，已删除并重新进入首次登录流程。")
        clear_login_state()
        return first_login(
            playwright,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            manual_waiter=manual_waiter,
            login_url=login_url,
            headless=headless,
            login_window_width=login_window_width,
            login_window_height=login_window_height,
            browser_path=browser_path,
        )

    browser = launch_browser(
        playwright,
        headless=headless,
        window_width=min(max(viewport_width, 1000), 1600),
        window_height=min(max(viewport_height, 700), 1000),
        browser_path=browser_path,
    )
    try:
        context = browser.new_context(
            storage_state=str(AUTH_STATE_PATH),
            viewport={"width": viewport_width, "height": viewport_height},
        )
        print(f"已加载登录状态: {AUTH_STATE_PATH}")
        return BrowserContextBundle(browser=browser, context=context)
    except Exception as exc:
        print(f"登录状态加载失败，已删除旧文件并重新登录: {exc}")
        browser.close()
        clear_login_state()
        return first_login(
            playwright,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            manual_waiter=manual_waiter,
            login_url=login_url,
            headless=headless,
            login_window_width=login_window_width,
            login_window_height=login_window_height,
            browser_path=browser_path,
        )


TEXT_COLLECTOR_SCRIPT = """
() => {
    const pieces = [];
    const walk = (node) => {
        if (!node) return;
        if (node.nodeType === Node.TEXT_NODE) {
            const text = (node.nodeValue || '').trim();
            if (text) pieces.push(text);
            return;
        }
        if (
            node.nodeType !== Node.ELEMENT_NODE &&
            node.nodeType !== Node.DOCUMENT_NODE &&
            node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE
        ) {
            return;
        }
        if (node.shadowRoot) {
            walk(node.shadowRoot);
        }
        for (const child of node.childNodes || []) {
            walk(child);
        }
    };
    walk(document.documentElement);
    return pieces.join('\\n').slice(0, 200000);
}
"""


def get_page_text(page: Any) -> str:
    try:
        return page.evaluate(TEXT_COLLECTOR_SCRIPT) or ""
    except Exception:
        return ""


def check_login_or_verify_page(page: Any) -> bool:
    url = (page.url or "").lower()
    if any(keyword.lower() in url for keyword in LOGIN_VERIFY_URL_KEYWORDS):
        return True

    text = get_page_text(page).lower()
    order_hits = [keyword for keyword in ORDER_PAGE_KEYWORDS if keyword.lower() in text]
    if len(order_hits) >= 2:
        return False

    strong_hits = [keyword for keyword in LOGIN_VERIFY_TEXT_KEYWORDS if keyword.lower() in text]
    if any(keyword in {"captcha", "awsc", "baxia"} for keyword in strong_hits):
        return True

    return len(strong_hits) >= 2


def wait_manual_login_or_verify(
    page: Any,
    context: Any,
    manual_waiter: ManualWaiter | None = None,
) -> None:
    if RUN_HEADLESS:
        raise RuntimeError("当前是 --headless 模式，无法手动处理登录或安全验证；请去掉 --headless 后重新运行。")

    message = "检测到需要登录或安全验证。请在浏览器中手动完成，完成后继续。"
    print(message)
    old_viewport = getattr(page, "viewport_size", None)
    try:
        page.set_viewport_size(MANUAL_LOGIN_VIEWPORT)
    except Exception:
        old_viewport = None

    if manual_waiter:
        manual_waiter(message)
    else:
        input("完成后按回车继续...")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)
    save_login_state(context, mark_authenticated=True)
    if old_viewport:
        try:
            page.set_viewport_size(old_viewport)
        except Exception:
            pass


def wait_order_page_loaded(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeoutError:
        print("等待 networkidle 超时，将继续检测页面内容。")

    page.wait_for_timeout(3000)

    if check_login_or_verify_page(page):
        raise ManualActionRequired("检测到登录页或安全验证页")

    text = get_page_text(page)
    hits = [keyword for keyword in ORDER_PAGE_KEYWORDS if keyword in text]
    if not hits:
        raise OrderPageLoadError("页面没有检测到订单详情关键字，可能加载异常、无权限或页面结构已变化。")


HIDE_FLOATING_CSS = """
.quark-watermark,
page-tools,
[class*="float"],
[class*="service-float"],
[class*="sidebar"],
[class*="toolbar"],
[class*="back-top"],
[class*="feedback"],
iframe {
    display: none !important;
    visibility: hidden !important;
}
"""


def hide_floating_elements(page: Any) -> None:
    try:
        page.add_style_tag(content=HIDE_FLOATING_CSS)
    except Exception:
        pass

    try:
        page.evaluate(
            """
            (css) => {
                const inject = (root) => {
                    if (!root || root.querySelector('style[data-order-screenshot-hide]')) return;
                    const style = document.createElement('style');
                    style.setAttribute('data-order-screenshot-hide', 'true');
                    style.textContent = css;
                    root.appendChild(style);
                };
                inject(document.head || document.documentElement);
                const walk = (node) => {
                    if (!node) return;
                    if (node.shadowRoot) {
                        inject(node.shadowRoot);
                        for (const child of node.shadowRoot.querySelectorAll('*')) walk(child);
                    }
                    for (const child of node.children || []) walk(child);
                };
                walk(document.documentElement);
            }
            """,
            HIDE_FLOATING_CSS,
        )
    except Exception:
        pass


def clamp_clip_to_document(page: Any, clip: Dict[str, float]) -> Dict[str, int]:
    dims = page.evaluate(
        """
        () => ({
            width: Math.max(
                document.documentElement.scrollWidth,
                document.body ? document.body.scrollWidth : 0,
                window.innerWidth
            ),
            height: Math.max(
                document.documentElement.scrollHeight,
                document.body ? document.body.scrollHeight : 0,
                window.innerHeight
            )
        })
        """
    )
    x = max(0, int(clip["x"]))
    y = max(0, int(clip["y"]))
    width = max(1, int(clip["width"]))
    height = max(1, int(clip["height"]))

    width = min(width, max(1, int(dims["width"]) - x))
    height = min(height, max(1, int(dims["height"]) - y))
    return {"x": x, "y": y, "width": width, "height": height}


def output_png_path(order_id: str, output_dir: str | Path) -> Path:
    path = Path(output_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{order_id}.png"


def save_screenshot(page: Any, order_id: str, output_dir: str | Path, clip: Dict[str, float]) -> Path:
    final_clip = clamp_clip_to_document(page, clip)
    if final_clip["width"] < 100 or final_clip["height"] < 100:
        raise ScreenshotError(f"截图区域过小: {final_clip}")

    path = output_png_path(order_id, output_dir)
    page.screenshot(path=str(path), clip=final_clip)
    return path


def screenshot_order_precise_area(page: Any, order_id: str, output_dir: str | Path) -> Path:
    Path(output_dir).expanduser().mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    hide_floating_elements(page)

    clip = page.evaluate(
        """
        () => {
            const appRoot = document.querySelector('app-root');
            if (!appRoot || !appRoot.shadowRoot) {
                return null;
            }

            const root = appRoot.shadowRoot;
            const main = root.querySelector('main.main-container');
            if (!main) {
                return null;
            }

            const qTheme = main.querySelector('q-theme');
            if (!qTheme) {
                return null;
            }

            const targets = [
                qTheme.querySelector('.stage-container'),
                qTheme.querySelector('detail-tabs'),
                qTheme.querySelector('.detail-container')
            ].filter(Boolean);

            if (!targets.length) {
                return null;
            }

            const rects = targets.map(el => {
                const r = el.getBoundingClientRect();
                return {
                    x: r.x + window.scrollX,
                    y: r.y + window.scrollY,
                    right: r.right + window.scrollX,
                    bottom: r.bottom + window.scrollY,
                    width: r.width,
                    height: r.height
                };
            }).filter(r => r.width > 300 && r.height > 20);

            if (!rects.length) {
                return null;
            }

            const left = Math.min(...rects.map(r => r.x));
            const top = Math.min(...rects.map(r => r.y));
            const right = Math.max(...rects.map(r => r.right));
            const bottom = Math.max(...rects.map(r => r.bottom));

            const paddingX = 0;
            const paddingTop = 0;
            const paddingBottom = 30;

            return {
                x: Math.max(0, Math.floor(left - paddingX)),
                y: Math.max(0, Math.floor(top - paddingTop)),
                width: Math.ceil((right - left) + paddingX * 2),
                height: Math.ceil((bottom - top) + paddingBottom)
            };
        }
        """
    )

    if not clip:
        raise ScreenshotError("精准 Shadow DOM 定位失败")
    if clip["width"] < 600 or clip["height"] < 300:
        raise ScreenshotError(f"精准截图区域尺寸异常: {clip}")

    return save_screenshot(page, order_id, output_dir, clip)


def screenshot_by_container(page: Any, order_id: str, output_dir: str | Path) -> Path:
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    hide_floating_elements(page)

    clip = page.evaluate(
        """
        () => {
            const appRoot = document.querySelector('app-root');
            const root = appRoot && appRoot.shadowRoot;
            if (!root) return null;

            const main = root.querySelector('main.main-container');
            const qTheme = main && main.querySelector('q-theme');
            const candidates = [
                qTheme && qTheme.querySelector('.detail-container'),
                qTheme,
                main
            ].filter(Boolean);

            for (const el of candidates) {
                const r = el.getBoundingClientRect();
                const clip = {
                    x: Math.max(0, Math.floor(r.x + window.scrollX)),
                    y: Math.max(0, Math.floor(r.y + window.scrollY)),
                    width: Math.ceil(r.width),
                    height: Math.ceil(r.height + 30)
                };
                if (clip.width > 600 && clip.height > 600) {
                    return clip;
                }
            }
            return null;
        }
        """
    )

    if not clip:
        raise ScreenshotError("主容器截图定位失败")
    return save_screenshot(page, order_id, output_dir, clip)


def screenshot_by_anchor(page: Any, order_id: str, output_dir: str | Path) -> Path:
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    hide_floating_elements(page)

    clip = page.evaluate(
        """
        ({topWords, bottomWords}) => {
            const elements = [];
            const collect = (root) => {
                if (!root || !root.querySelectorAll) return;
                for (const el of root.querySelectorAll('*')) {
                    elements.push(el);
                    if (el.shadowRoot) collect(el.shadowRoot);
                }
            };
            collect(document);

            const appRoot = document.querySelector('app-root');
            const shadow = appRoot && appRoot.shadowRoot;
            const main = shadow && shadow.querySelector('main.main-container');
            const mainRect = main ? main.getBoundingClientRect() : null;

            const hasWord = (text, words) => words.some(word => text.includes(word));
            const rectFor = (el) => {
                const r = el.getBoundingClientRect();
                return {
                    x: r.x + window.scrollX,
                    y: r.y + window.scrollY,
                    right: r.right + window.scrollX,
                    bottom: r.bottom + window.scrollY,
                    width: r.width,
                    height: r.height
                };
            };

            const matches = (words) => elements.map(el => {
                const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!text || !hasWord(text, words)) return null;
                const r = rectFor(el);
                if (r.width < 20 || r.height < 8 || r.width > 1300 || r.height > 500) return null;
                return r;
            }).filter(Boolean);

            const tops = matches(topWords);
            const bottoms = matches(bottomWords);
            if (!tops.length && !bottoms.length && !mainRect) return null;

            const topRect = tops.length
                ? tops.reduce((best, item) => item.y < best.y ? item : best, tops[0])
                : null;
            const bottomRect = bottoms.length
                ? bottoms.reduce((best, item) => item.bottom > best.bottom ? item : best, bottoms[0])
                : null;

            const x = mainRect ? Math.max(0, Math.floor(mainRect.x + window.scrollX)) : 250;
            const width = mainRect ? Math.ceil(mainRect.width) : 950;
            const top = topRect ? topRect.y : (mainRect ? mainRect.y + window.scrollY : 80);
            const bottom = bottomRect
                ? bottomRect.bottom
                : (mainRect ? mainRect.bottom + window.scrollY : top + 1500);

            return {
                x,
                y: Math.max(0, Math.floor(top - 40)),
                width,
                height: Math.ceil(bottom - top + 140)
            };
        }
        """,
        {"topWords": TOP_ANCHOR_WORDS, "bottomWords": BOTTOM_ANCHOR_WORDS},
    )

    if not clip:
        raise ScreenshotError("文字锚点截图定位失败")
    if clip["width"] < 600 or clip["height"] < 600:
        raise ScreenshotError(f"文字锚点截图区域尺寸异常: {clip}")
    return save_screenshot(page, order_id, output_dir, clip)


def screenshot_by_fixed_clip(
    page: Any,
    order_id: str,
    output_dir: str | Path,
    clip_config: Dict[str, int],
) -> Path:
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    hide_floating_elements(page)
    return save_screenshot(page, order_id, output_dir, clip_config)


def screenshot_order(page: Any, order_id: str, output_dir: str | Path) -> Tuple[Path, str]:
    attempts = [
        ("精准 Shadow DOM 区域截图", lambda: screenshot_order_precise_area(page, order_id, output_dir)),
        ("主容器截图", lambda: screenshot_by_container(page, order_id, output_dir)),
        ("文字锚点截图", lambda: screenshot_by_anchor(page, order_id, output_dir)),
        ("固定 clip 截图", lambda: screenshot_by_fixed_clip(page, order_id, output_dir, FIXED_CLIP_CONFIG)),
    ]

    errors = []
    for name, fn in attempts:
        try:
            path = fn()
            return path, name
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            print(f"{name}失败，尝试下一个方案。原因: {exc}")

    raise ScreenshotError("所有截图方案均失败；" + "；".join(errors))


PAYMENT_CONTEXT_SCRIPT = """
() => {
    const results = [];
    const seen = new Set();
    const add = (text) => {
        text = (text || '').replace(/\\s+/g, ' ').trim();
        if (!text || seen.has(text)) return;
        seen.add(text);
        results.push(text.slice(0, 1200));
    };
    const walk = (root) => {
        if (!root || !root.querySelectorAll) return;
        for (const el of root.querySelectorAll('*')) {
            const text = (el.innerText || el.textContent || '').trim();
            if (text && /实付款|实付金额|应付款|合计/.test(text)) {
                let node = el;
                for (let i = 0; i < 4 && node; i += 1) {
                    add(node.innerText || node.textContent || '');
                    node = node.parentElement;
                }
            }
            if (el.shadowRoot) walk(el.shadowRoot);
        }
    };
    walk(document);
    return results;
}
"""


def extract_amount_from_text(text: str) -> Tuple[str, str]:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    patterns = [
        r"(?:实付款|实付金额|应付款)[^¥￥0-9]{0,30}([¥￥]?\s*[0-9][0-9,]*(?:\.\d{1,2})?)",
        r"([¥￥]\s*[0-9][0-9,]*(?:\.\d{1,2})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            amount = re.sub(r"\s+", "", match.group(1)).replace(",", "")
            return amount, normalized[:300]

    lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
    for index, line in enumerate(lines):
        if any(word in line for word in ("实付款", "实付金额", "应付款")):
            window = " ".join(lines[index : index + 12])
            match = re.search(r"([¥￥]?\s*[0-9][0-9,]*(?:\.\d{1,2})?)", window)
            if match:
                amount = re.sub(r"\s+", "", match.group(1)).replace(",", "")
                return amount, window[:300]

    return "", ""


def extract_paid_amount(page: Any) -> Dict[str, str]:
    candidates: List[str] = []
    try:
        candidates.extend(page.evaluate(PAYMENT_CONTEXT_SCRIPT) or [])
    except Exception:
        pass

    page_text = get_page_text(page)
    if page_text:
        candidates.append(page_text)

    for text in candidates:
        amount, raw = extract_amount_from_text(text)
        if amount:
            return {"amount": amount, "status": "成功", "raw": raw}

    return {"amount": "", "status": "未找到", "raw": ""}


def export_payment_table(records: List[Dict[str, Any]], output_dir: str | Path) -> Path:
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)
    table_path = output_path / "订单实付款汇总.xlsx"
    columns = [
        "订单号",
        "实付款金额",
        "金额读取状态",
        "截图结果",
        "截图文件",
        "截图方式",
        "失败原因",
        "读取时间",
        "提取参考文本",
    ]
    df = pd.DataFrame(records)
    for column in columns:
        if column not in df.columns:
            df[column] = ""
    df = df[columns]
    df.to_excel(table_path, index=False)
    return table_path


def log_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_success_log(order_id: str) -> None:
    ensure_project_dirs()
    with SUCCESS_LOG.open("a", encoding="utf-8") as file:
        file.write(f"{log_time()}\t{order_id}\n")


def write_failed_log(order_id: str, error: Exception | str) -> None:
    ensure_project_dirs()
    reason = str(error).replace("\r", " ").replace("\n", " ")
    with FAILED_LOG.open("a", encoding="utf-8") as file:
        file.write(f"{log_time()}\t{order_id}\t{reason}\n")


def open_order_page(
    page: Any,
    context: Any,
    order_id: str,
    manual_waiter: ManualWaiter | None = None,
) -> None:
    url = build_order_url(order_id)
    while True:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            wait_order_page_loaded(page)
            return
        except ManualActionRequired:
            wait_manual_login_or_verify(page, context, manual_waiter=manual_waiter)
        except OrderPageLoadError:
            if check_login_or_verify_page(page):
                wait_manual_login_or_verify(page, context, manual_waiter=manual_waiter)
            else:
                raise


def process_order(
    page: Any,
    context: Any,
    order_id: str,
    output_dir: str | Path,
    retries: int,
    manual_waiter: ManualWaiter | None = None,
    should_stop: StopChecker | None = None,
    payment_recorder: PaymentRecorder | None = None,
) -> bool:
    last_payment: Dict[str, str] | None = None
    for attempt in range(1, retries + 1):
        if should_stop and should_stop():
            raise RuntimeError("任务已停止")
        try:
            print(f"[{order_id}] 开始处理，第 {attempt}/{retries} 次尝试。")
            open_order_page(page, context, order_id, manual_waiter=manual_waiter)
            if payment_recorder:
                try:
                    last_payment = extract_paid_amount(page)
                    print(f"[{order_id}] 实付款读取结果: {last_payment['status']} {last_payment['amount']}")
                except Exception as payment_exc:
                    last_payment = {"amount": "", "status": f"读取失败: {payment_exc}", "raw": ""}
                    print(f"[{order_id}] 实付款读取失败: {payment_exc}")
            path, method = screenshot_order(page, order_id, output_dir)
            write_success_log(order_id)
            if payment_recorder:
                payment_recorder(
                    {
                        "订单号": order_id,
                        "实付款金额": (last_payment or {}).get("amount", ""),
                        "金额读取状态": (last_payment or {}).get("status", "未读取"),
                        "截图结果": "成功",
                        "截图文件": str(path),
                        "截图方式": method,
                        "失败原因": "",
                        "读取时间": log_time(),
                        "提取参考文本": (last_payment or {}).get("raw", ""),
                    }
                )
            print(f"[{order_id}] 截图成功: {path} ({method})")
            return True
        except Exception as exc:
            if should_stop and should_stop():
                raise RuntimeError("任务已停止") from exc
            print(f"[{order_id}] 本次失败: {exc}")
            if attempt >= retries:
                write_failed_log(order_id, exc)
                if payment_recorder:
                    payment_recorder(
                        {
                            "订单号": order_id,
                            "实付款金额": (last_payment or {}).get("amount", ""),
                            "金额读取状态": (last_payment or {}).get("status", "未读取"),
                            "截图结果": "失败",
                            "截图文件": "",
                            "截图方式": "",
                            "失败原因": str(exc),
                            "读取时间": log_time(),
                            "提取参考文本": (last_payment or {}).get("raw", ""),
                        }
                    )
                print(f"[{order_id}] 已写入失败日志。")
                return False
            time.sleep(random.uniform(1, 3))
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量截取 1688 订单详情页主体区域")
    parser.add_argument("--input", required=True, help="订单号文件路径，支持 txt、xlsx、xls")
    parser.add_argument("--output", required=True, help="截图保存目录")
    parser.add_argument("--headless", action="store_true", help="使用无头模式运行，默认关闭")
    parser.add_argument("--viewport-width", type=int, default=1440, help="浏览器视口宽度，默认 1440")
    parser.add_argument("--viewport-height", type=int, default=1800, help="浏览器视口高度，默认 1800")
    parser.add_argument("--clip-x", type=int, default=DEFAULT_FIXED_CLIP["x"], help="固定截图 x 坐标，默认 250")
    parser.add_argument("--clip-y", type=int, default=DEFAULT_FIXED_CLIP["y"], help="固定截图 y 坐标，默认 80")
    parser.add_argument("--clip-width", type=int, default=DEFAULT_FIXED_CLIP["width"], help="固定截图宽度，默认 950")
    parser.add_argument("--clip-height", type=int, default=DEFAULT_FIXED_CLIP["height"], help="固定截图高度，默认 1500")
    parser.add_argument("--retries", type=int, default=3, help="每个订单失败重试次数，默认 3")
    parser.add_argument("--delay-min", type=float, default=1.0, help="每个订单之间最短等待秒数，默认 1")
    parser.add_argument("--delay-max", type=float, default=3.0, help="每个订单之间最长等待秒数，默认 3")
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL, help="首次登录入口，默认使用 1688 登录页")
    parser.add_argument("--login-window-width", type=int, default=DEFAULT_LOGIN_WINDOW_WIDTH, help="首次登录浏览器窗口宽度，默认 1280")
    parser.add_argument("--login-window-height", type=int, default=DEFAULT_LOGIN_WINDOW_HEIGHT, help="首次登录浏览器窗口高度，默认 900")
    parser.add_argument("--auth-max-age-hours", type=float, default=DEFAULT_AUTH_MAX_AGE_HOURS, help="登录状态有效期（小时），默认 24；设为 0 表示不按时间强制失效")
    parser.add_argument("--browser-path", default="", help="可选：手动指定 chrome.exe 或 msedge.exe 路径")
    parser.add_argument("--export-payment-table", action="store_true", help="开启后读取每笔订单实付款，并在输出目录导出 Excel 汇总表")
    parser.add_argument(
        "--order-url-template",
        default=DEFAULT_ORDER_URL_TEMPLATE,
        help="订单详情链接模板，使用 {order_id} 作为订单号占位符",
    )
    return parser.parse_args()


def main() -> int:
    global FIXED_CLIP_CONFIG, RUN_HEADLESS, ORDER_URL_TEMPLATE, MANUAL_LOGIN_VIEWPORT

    args = parse_args()
    RUN_HEADLESS = bool(args.headless)
    FIXED_CLIP_CONFIG = {
        "x": args.clip_x,
        "y": args.clip_y,
        "width": args.clip_width,
        "height": args.clip_height,
    }
    ORDER_URL_TEMPLATE = args.order_url_template
    MANUAL_LOGIN_VIEWPORT = {
        "width": args.login_window_width,
        "height": args.login_window_height,
    }

    ensure_project_dirs()
    output_dir = Path(args.output).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        orders = read_orders(args.input)
    except Exception as exc:
        print(f"读取订单失败: {exc}", file=sys.stderr)
        return 2

    print(f"读取到 {len(orders)} 个有效订单号。")
    if args.headless:
        print("当前为 headless 模式；如果中途出现登录或安全验证，请去掉 --headless 后重新运行。")

    bundle: BrowserContextBundle | None = None
    success_count = 0
    failed_count = 0
    payment_records: Dict[str, Dict[str, Any]] = {}

    def record_payment(row: Dict[str, Any]) -> None:
        payment_records[str(row.get("订单号", ""))] = row

    with sync_playwright() as playwright:
        try:
            bundle = create_browser_context(
                playwright,
                headless=args.headless,
                viewport_width=args.viewport_width,
                viewport_height=args.viewport_height,
                login_url=args.login_url,
                login_window_width=args.login_window_width,
                login_window_height=args.login_window_height,
                browser_path=args.browser_path or None,
                auth_max_age_hours=max(0.0, args.auth_max_age_hours),
            )
            page = bundle.context.new_page()

            for index, order_id in enumerate(orders, start=1):
                print(f"进度 {index}/{len(orders)}")
                if process_order(
                    page,
                    bundle.context,
                    order_id,
                    output_dir,
                    max(1, args.retries),
                    payment_recorder=record_payment if args.export_payment_table else None,
                ):
                    success_count += 1
                else:
                    failed_count += 1

                if index < len(orders):
                    delay = random.uniform(min(args.delay_min, args.delay_max), max(args.delay_min, args.delay_max))
                    print(f"等待 {delay:.1f} 秒后继续下一个订单。")
                    time.sleep(delay)

        finally:
            if bundle is not None:
                try:
                    save_login_state(bundle.context)
                except Exception:
                    pass
                bundle.browser.close()

    print(f"任务完成。成功: {success_count}，失败: {failed_count}")
    if args.export_payment_table:
        table_path = export_payment_table([payment_records.get(order_id, {"订单号": order_id}) for order_id in orders], output_dir)
        print(f"实付款汇总表: {table_path}")
    print(f"成功日志: {SUCCESS_LOG}")
    print(f"失败日志: {FAILED_LOG}")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
