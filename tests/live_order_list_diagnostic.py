from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from order_batch.fetcher import DEFAULT_ORDER_LIST_URL, apply_date_filter, collect_current_page


ROOT = Path(__file__).resolve().parents[1]
AUTH_STATE = ROOT / "dist" / "1688订单截图工具_新版8" / "auth" / "1688_login_state.json"

CONTROL_SCRIPT = r"""
() => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 5 && rect.height > 5 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const clean = value => (value || '').replace(/\s+/g, ' ').trim();
    const describe = el => ({
        tag: el.tagName,
        text: clean(el.innerText || el.textContent).slice(0, 120),
        className: String(el.className || '').slice(0, 300),
        role: el.getAttribute('role') || '',
        placeholder: el.getAttribute('placeholder') || '',
        disabled: Boolean(el.disabled),
        outerHTML: el.outerHTML.slice(0, 800)
    });
    const all = Array.from(document.querySelectorAll('*'));
    const orderTime = all.filter(el => visible(el) && clean(el.innerText || el.textContent) === '下单时间').map(describe);
    const customTime = all.filter(el => visible(el) && clean(el.innerText || el.textContent) === '自定义时间').map(describe);
    const dateInputs = Array.from(document.querySelectorAll('input')).filter(el => {
        const hint = [el.placeholder, el.name, el.id, el.className].join(' ');
        return /日期|时间|start|end|date/i.test(hint);
    }).map(describe);
    return {orderTime, customTime, dateInputs};
}
"""


def main() -> int:
    if not AUTH_STATE.exists():
        raise RuntimeError(f"登录状态不存在: {AUTH_STATE}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(storage_state=str(AUTH_STATE), viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.goto(DEFAULT_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2500)

            print(json.dumps({
                "url": page.url,
                "title": page.title(),
                "frames": len(page.frames),
                "controls": [frame.evaluate(CONTROL_SCRIPT) for frame in page.frames],
            }, ensure_ascii=False, indent=2))

            today = date.today()
            applied = apply_date_filter(page, today, today)
            page.wait_for_timeout(3000)
            orders = collect_current_page(page)
            page_text = page.locator("body").inner_text(timeout=10000)
            print(json.dumps({
                "applied": applied,
                "range": [today.isoformat(), today.isoformat()],
                "visible_order_count": len(orders),
                "orders": [
                    {"order_id": item.order_id, "status": item.status, "created_at": item.created_at}
                    for item in orders[:20]
                ],
                "empty_markers": [
                    marker for marker in ("暂无符合条件的订单", "暂无订单", "没有找到", "未找到相关订单")
                    if marker in page_text
                ],
            }, ensure_ascii=False, indent=2))
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
