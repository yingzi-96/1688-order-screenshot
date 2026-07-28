from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .filters import parse_order_date
from .models import OrderSummary


DEFAULT_ORDER_LIST_URL = "https://work.1688.com/home/buyer.htm?_path_=buyer2017Base/2017buyerbase_trade/buyList"

ORDER_LIST_SCRIPT = r"""
() => {
    const results = [];
    const seen = new Set();
    const statusWords = [
        '交易关闭', '订单关闭', '已关闭', '已取消', '退款成功', '退款中', '已退款',
        '等待买家付款', '待付款', '未付款', '未支付', '等待卖家发货', '待卖家发货',
        '待发货', '发货超时', '等待买家确认收货', '等待收货', '待收货', '卖家已发货', '已发货',
        '交易成功', '交易完成', '待评价', '已完成', '已支付', '已付款'
    ];
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const deepText = node => {
        const pieces = [];
        const visited = new Set();
        const walk = current => {
            if (!current || visited.has(current)) return;
            visited.add(current);
            if (current.nodeType === Node.TEXT_NODE) {
                const value = normalize(current.textContent);
                if (value) pieces.push(value);
                return;
            }
            if (current.shadowRoot) walk(current.shadowRoot);
            for (const child of current.childNodes || []) walk(child);
        };
        walk(node);
        return normalize(pieces.join(' '));
    };
    const deepLinks = node => {
        const links = [];
        const visited = new Set();
        const walk = root => {
            if (!root || visited.has(root) || !root.querySelectorAll) return;
            visited.add(root);
            for (const link of root.querySelectorAll('a[href]')) links.push(link.href || '');
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        };
        walk(node.shadowRoot || node);
        return links;
    };
    const add = (orderId, href, text, statusText = '') => {
        if (!orderId || seen.has(orderId)) return;
        seen.add(orderId);
        text = normalize(text);
        const normalizedStatus = normalize(statusText);
        const status = statusWords.find(word => normalizedStatus.includes(word)) ||
            statusWords.find(word => text.includes(word)) || '';
        const labeledDateMatch = text.match(
            /(?:下单时间|创建时间|订单时间|成交时间)\s*[：:]?\s*(20\d{2}[-\/]\d{1,2}[-\/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)/
        );
        const dateMatch = labeledDateMatch || text.match(
            /(20\d{2}[-\/]\d{1,2}[-\/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)/
        );
        results.push({
            orderId,
            status,
            createdAt: dateMatch ? dateMatch[1].replaceAll('/', '-') : '',
            detailUrl: href || '',
            rawText: text.slice(0, 4000)
        });
    };
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);

    const handledItems = new Set();
    for (const root of roots) {
        for (const item of root.querySelectorAll('order-item[data-tracker-args*="orderId="]')) {
            if (handledItems.has(item)) continue;
            handledItems.add(item);
            const idMatch = (item.getAttribute('data-tracker-args') || '').match(/orderId=([0-9]{12,24})/i);
            if (!idMatch) continue;
            const orderId = idMatch[1];
            const text = deepText(item);
            const statusHost = item.shadowRoot?.querySelector('order-item-status');
            const statusText = statusHost ? deepText(statusHost) : '';
            const detailUrl = deepLinks(item).find(url => {
                return /trade-order-detail/i.test(url) && new RegExp(`[?&]orderId=${orderId}(?:&|$)`, 'i').test(url);
            }) || '';
            add(orderId, detailUrl, text, statusText);
        }
    }

    for (const root of roots) {
        for (const a of root.querySelectorAll('a[href]')) {
            const href = a.href || '';
            const match = href.match(/(?:orderId|order_id|orderid)=([0-9]{12,24})/i);
            if (!match) continue;
            let container = a;
            for (let i = 0; i < 7 && container.parentElement; i += 1) {
                const next = container.parentElement;
                const value = normalize(next.innerText || next.textContent);
                container = next;
                if (value.length > 80 && statusWords.some(word => value.includes(word))) break;
            }
            add(match[1], href, container.innerText || container.textContent || a.innerText || '');
        }
        const rootText = root.innerText || root.textContent || '';
        for (const match of rootText.matchAll(/(?:订单号|订单编号|订单ID|orderId)\s*[：:]?\s*([0-9]{12,24})/gi)) {
            const index = match.index || 0;
            add(match[1], '', rootText.slice(Math.max(0, index - 300), index + 900));
        }
    }
    return results;
}
"""
OPEN_CUSTOM_DATE_SCRIPT = r"""
() => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const clean = value => (value || '').replace(/\s+/g, '').trim();
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const all = roots.flatMap(root => Array.from(root.querySelectorAll('*')));

    const direct = all.find(el => {
        return el.tagName === 'Q-SELECT' && el.getAttribute('placeholder') === '下单时间' && visible(el);
    });
    if (direct) {
        (direct.shadowRoot?.querySelector('.q-select-selector') || direct).click();
        return true;
    }

    const label = all.find(el => visible(el) && clean(el.innerText || el.textContent) === '下单时间');
    if (!label) return false;
    const labelRect = label.getBoundingClientRect();
    const labelCenterY = labelRect.top + labelRect.height / 2;
    const controls = all.filter(el => {
        if (!visible(el) || el === label || label.contains(el)) return false;
        const rect = el.getBoundingClientRect();
        const centerY = rect.top + rect.height / 2;
        const selectable = el.matches?.('q-select,input,[role="combobox"],button,[class*="select"],[class*="picker"]');
        return selectable && rect.width >= 80 && rect.left >= labelRect.right - 20 &&
            Math.abs(centerY - labelCenterY) < 45;
    }).sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return Math.abs(ar.left - labelRect.right) - Math.abs(br.left - labelRect.right);
    });
    if (!controls.length) return false;
    (controls[0].shadowRoot?.querySelector('.q-select-selector') || controls[0]).click();
    return true;
}
"""

OPEN_DATE_PICKER_SCRIPT = r"""
placeholder => {
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const date = roots.flatMap(root => Array.from(root.querySelectorAll('q-date'))).find(el => {
        return el.getAttribute('placeholder') === placeholder;
    });
    if (!date) return false;
    const dateTime = date.shadowRoot?.querySelector('q-date-time');
    const picker = dateTime?.shadowRoot?.querySelector('ui-datetime');
    (picker || dateTime || date).click();
    return true;
}
"""

PICK_DATE_SCRIPT = r"""
target => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 5 && rect.height > 5 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const all = roots.flatMap(root => Array.from(root.querySelectorAll('*')));
    const item = all.find(el => {
        return el.matches?.(`a.ui-date-item[data-date="${target}"]`) && visible(el);
    });
    if (item) {
        item.click();
        return {calendar: true, picked: true};
    }

    const calendar = all.find(el => el.matches?.('.ui-date-container') && visible(el));
    if (!calendar) return {calendar: false, picked: false};
    const switcher = calendar.querySelector('.ui-date-switch');
    const monthMatch = (switcher?.textContent || '').match(/(20\d{2})-(\d{1,2})/);
    const targetMatch = target.match(/(20\d{2})-(\d{2})-\d{2}/);
    if (!monthMatch || !targetMatch) return {calendar: true, picked: false, error: '无法读取日历月份'};
    const currentMonth = Number(monthMatch[1]) * 12 + Number(monthMatch[2]);
    const targetMonth = Number(targetMatch[1]) * 12 + Number(targetMatch[2]);
    const direction = targetMonth < currentMonth ? '.ui-date-prev' : '.ui-date-next';
    const navigation = calendar.querySelector(direction);
    if (targetMonth === currentMonth || !navigation) {
        return {calendar: true, picked: false, error: '目标日期不可选择'};
    }
    navigation.click();
    return {calendar: true, picked: false, navigated: true};
}
"""

READ_DATE_VALUES_SCRIPT = r"""
() => {
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const result = {};
    for (const date of roots.flatMap(root => Array.from(root.querySelectorAll('q-date')))) {
        const placeholder = date.getAttribute('placeholder') || '';
        if (placeholder === '开始日期' || placeholder === '结束日期') result[placeholder] = date.value || '';
    }
    return result;
}
"""

CLICK_SEARCH_SCRIPT = r"""
() => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const clean = value => (value || '').replace(/\s+/g, '').trim();
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const search = roots.flatMap(root => Array.from(root.querySelectorAll('q-button,button,a,[role="button"]'))).find(el => {
        return visible(el) && /^(查询|搜索)$/.test(clean(el.innerText || el.textContent));
    });
    if (!search) return false;
    search.click();
    return true;
}
"""

SELECT_CUSTOM_DATE_SCRIPT = r"""
() => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const clean = value => (value || '').replace(/\s+/g, '').trim();
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const option = roots.flatMap(root => Array.from(root.querySelectorAll('*'))).find(el => {
        return visible(el) && clean(el.innerText || el.textContent) === '自定义时间';
    });
    if (!option) return false;
    option.click();
    return true;
}
"""

APPLY_DATE_SCRIPT = r"""
({start, end}) => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const inputs = roots.flatMap(root => Array.from(root.querySelectorAll('input'))).filter(visible);
    const hint = el => [el.placeholder, el.name, el.id, el.getAttribute('aria-label')].join(' ');
    const startInput = inputs.find(el => /开始日期|开始时间|start/i.test(hint(el)));
    const endInput = inputs.find(el => /结束日期|结束时间|end/i.test(hint(el)));
    if (!startInput || !endInput) return {filled: false, searched: false};
    const fill = (el, value) => {
        el.focus();
        let proto = el;
        let setter = null;
        while (proto && !setter) {
            setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set || null;
            proto = Object.getPrototypeOf(proto);
        }
        if (setter) setter.call(el, value);
        else el.value = value;
        el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
        el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
        el.blur();
    };
    fill(startInput, start);
    fill(endInput, end);
    const search = roots.flatMap(
        root => Array.from(root.querySelectorAll('q-button,button,a,[role="button"]'))
    ).filter(visible).find(el => /^(查询|搜索)$/.test((el.innerText || el.textContent || '').replace(/\s+/g, '').trim()));
    if (search) search.click();
    return {
        filled: startInput.value === start && endInput.value === end,
        searched: Boolean(search),
        startValue: startInput.value,
        endValue: endInput.value
    };
}
"""
NEXT_PAGE_SCRIPT = r"""
() => {
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 10 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
    };
    const roots = [];
    const collectRoots = root => {
        if (!root || !root.querySelectorAll) return;
        roots.push(root);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) collectRoots(el.shadowRoot);
        }
    };
    collectRoots(document);
    const nodes = roots.flatMap(
        root => Array.from(root.querySelectorAll('button,a,[role="button"]'))
    ).filter(visible);
    const next = nodes.find(el => /下一页|下页|next/i.test([
        el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')
    ].join(' ')));
    if (!next) return false;
    if (next.disabled || next.getAttribute('aria-disabled') === 'true' || /disabled|disable/.test(next.className || '')) return false;
    next.click();
    return true;
}
"""


def _evaluate_frames(page: Any, script: str, arg: dict | None = None) -> list[Any]:
    results = []
    for frame in page.frames:
        try:
            results.append(frame.evaluate(script, arg) if arg is not None else frame.evaluate(script))
        except Exception:
            continue
    return results


def _pick_q_date(page: Any, placeholder: str, target: str) -> bool:
    opened = False
    for frame in page.frames:
        try:
            opened = bool(frame.evaluate(OPEN_DATE_PICKER_SCRIPT, placeholder))
        except Exception:
            continue
        if opened:
            break
    if not opened:
        return False

    page.wait_for_timeout(250)
    for _ in range(240):
        calendar_found = False
        navigated = False
        error = ""
        for frame in page.frames:
            try:
                result = frame.evaluate(PICK_DATE_SCRIPT, target)
            except Exception:
                continue
            if not result:
                continue
            if result.get("picked"):
                page.wait_for_timeout(250)
                return True
            if result.get("calendar"):
                calendar_found = True
                error = str(result.get("error", ""))
            if result.get("navigated"):
                navigated = True
                break
        if navigated:
            page.wait_for_timeout(150)
            continue
        if calendar_found and error:
            print(f"日期 {target} 选择失败: {error}")
        return False
    print(f"日期 {target} 与当前月份相距过远，已停止日历翻页。")
    return False


def _click_order_search(page: Any) -> bool:
    for frame in page.frames:
        try:
            if frame.evaluate(CLICK_SEARCH_SCRIPT):
                return True
        except Exception:
            continue
    return False


def apply_date_filter(page: Any, start: date, end: date) -> bool:
    payload = {"start": start.isoformat(), "end": end.isoformat()}
    opened = False
    for frame in page.frames:
        try:
            opened = bool(frame.evaluate(OPEN_CUSTOM_DATE_SCRIPT))
        except Exception:
            continue
        if opened:
            break
    if not opened:
        print("未找到“下单时间”筛选框。")
        return False

    page.wait_for_timeout(300)
    start_picked = _pick_q_date(page, "开始日期", payload["start"])
    end_picked = _pick_q_date(page, "结束日期", payload["end"]) if start_picked else False
    if start_picked and end_picked:
        values_ok = False
        for frame in page.frames:
            try:
                values = frame.evaluate(READ_DATE_VALUES_SCRIPT) or {}
            except Exception:
                continue
            if values.get("开始日期") == payload["start"] and values.get("结束日期") == payload["end"]:
                values_ok = True
                break
        if not values_ok:
            print("日历已点击，但日期组件没有保存所选值。")
            return False
        if not _click_order_search(page):
            print("日期已选择，但未定位到订单筛选区域的“搜索”按钮。")
            return False
        return True

    # 兼容普通 input 日期控件或旧版订单列表。
    selected = False
    for frame in page.frames:
        try:
            selected = bool(frame.evaluate(SELECT_CUSTOM_DATE_SCRIPT))
        except Exception:
            continue
        if selected:
            break
    if selected:
        page.wait_for_timeout(300)
    for frame in page.frames:
        try:
            result = frame.evaluate(APPLY_DATE_SCRIPT, payload)
        except Exception:
            continue
        if result and result.get("filled") and result.get("searched"):
            return True
    print("未能完成下单时间筛选。")
    return False

def collect_current_page(page: Any) -> list[OrderSummary]:
    merged: dict[str, OrderSummary] = {}
    for result in _evaluate_frames(page, ORDER_LIST_SCRIPT):
        for item in result or []:
            order_id = str(item.get("orderId", "")).strip()
            if not re.fullmatch(r"\d{12,24}", order_id):
                continue
            candidate = OrderSummary(
                order_id=order_id,
                status=str(item.get("status", "")),
                created_at=str(item.get("createdAt", "")),
                detail_url=str(item.get("detailUrl", "")),
                raw_text=str(item.get("rawText", "")),
            )
            existing = merged.get(order_id)
            if existing is None or len(candidate.raw_text) > len(existing.raw_text):
                merged[order_id] = candidate
    return list(merged.values())


def click_next_page(page: Any) -> bool:
    for frame in page.frames:
        try:
            if frame.evaluate(NEXT_PAGE_SCRIPT):
                return True
        except Exception:
            continue
    return False


def fetch_orders(
    page: Any,
    order_list_url: str,
    start: date,
    end: date,
    check_login: Callable[[Any], bool] | None = None,
    manual_login: Callable[[], None] | None = None,
    max_pages: int = 100,
) -> list[OrderSummary]:
    print(f"打开订单列表: {order_list_url}")
    page.goto(order_list_url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2000)
    if check_login and check_login(page) and manual_login:
        manual_login()
        page.goto(order_list_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

    filtered_in_ui = apply_date_filter(page, start, end)
    if filtered_in_ui:
        print(f"已在订单列表填写日期范围: {start.isoformat()} 至 {end.isoformat()}")
        page.wait_for_timeout(2500)
    else:
        print("未定位到订单列表日期控件，将抓取分页后在本地严格按日期过滤。")

    orders: dict[str, OrderSummary] = {}
    previous_ids: set[str] = set()
    for page_number in range(1, max_pages + 1):
        current = collect_current_page(page)
        current_ids = {item.order_id for item in current}
        print(f"订单列表第 {page_number} 页读取到 {len(current)} 个订单。")
        for item in current:
            existing = orders.get(item.order_id)
            if existing is None or len(item.raw_text) > len(existing.raw_text):
                orders[item.order_id] = item
        parsed_dates = [parse_order_date(item.created_at) for item in current]
        parsed_dates = [item_date for item_date in parsed_dates if item_date is not None]
        if not filtered_in_ui and parsed_dates and max(parsed_dates) < start:
            print("当前页订单日期均早于开始日期，停止继续翻页。")
            break
        if not current_ids or current_ids == previous_ids or not click_next_page(page):
            break
        previous_ids = current_ids
        page.wait_for_timeout(1800)
    print(f"订单列表抓取完成，去重后共 {len(orders)} 个订单。")
    return list(orders.values())
