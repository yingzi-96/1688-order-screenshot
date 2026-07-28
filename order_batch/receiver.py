from __future__ import annotations

import re
from typing import Any

from .models import ReceiverInfo


RECEIVER_CONTEXT_SCRIPT = r"""
() => {
    const values = [];
    const seen = new Set();
    const add = text => {
        text = (text || '').replace(/\s+/g, ' ').trim();
        if (!text || seen.has(text)) return;
        seen.add(text);
        values.push(text.slice(0, 2000));
    };
    const walk = root => {
        if (!root || !root.querySelectorAll) return;
        for (const el of root.querySelectorAll('*')) {
            const text = el.innerText || el.textContent || '';
            if (/收货人|收件人|收货信息|收货地址|收件地址|手机号|手机号码|联系电话|联系手机|收货电话/.test(text)) {
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
    return values;
}
"""


def _first_match(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" ：:，,;；")
    return ""


def parse_receiver_info(page: Any, full_text: str = "") -> ReceiverInfo:
    contexts: list[str] = []
    try:
        contexts.extend(page.evaluate(RECEIVER_CONTEXT_SCRIPT) or [])
    except Exception:
        pass
    if full_text:
        contexts.append(full_text)
    text = "\n".join(contexts)

    phone = _first_match(
        (
            r"(?:手机号|手机号码|联系电话|联系手机|收货电话|电话)\s*[：:]?\s*((?:\+?86[- ]?)?1[3-9]\d{9})",
            r"(?:手机号|手机号码|联系电话|联系手机|收货电话|电话)\s*[：:]?\s*(1\d{2}\*{3,4}\d{4})",
            r"\b((?:\+?86[- ]?)?1[3-9]\d{9})\b",
            r"\b(1\d{2}\*{3,4}\d{4})\b",
        ),
        text,
    )
    receiver = _first_match(
        (
            r"(?:收货人|收件人|收货人姓名|收货姓名|姓名)\s*[：:]?\s*(.{1,30}?)(?=\s*(?:手机号|手机号码|联系电话|联系手机|收货电话|电话|收货地址|收件地址|详细地址|地址|$))",
            r"(?:联系人)\s*[：:]?\s*(.{1,30}?)(?=\s*(?:手机号|手机号码|联系电话|联系手机|收货电话|电话|收货地址|收件地址|详细地址|地址|$))",
            r"(?:收货信息)\s*[：:]?\s*(.{1,30}?)(?=\s*[,，]?\s*(?:(?:\+?86[- ]?)?1[3-9]\d{9}|1\d{2}\*{3,4}\d{4}))",
        ),
        text,
    )
    address = _first_match(
        (
            r"(?:收货地址|收件地址|详细地址|地址)\s*[：:]?\s*(.{6,220}?)(?=\s*(?:邮编|订单信息|买家留言|配送方式|物流信息|$))",
            r"(?:收货地址|收件地址|详细地址|地址)\s*[：:]?\s*([^\n]{6,220})",
        ),
        text,
    )
    if address and phone and phone in address:
        address = address.split(phone, 1)[0].strip(" ，,;；")
    return ReceiverInfo(receiver=receiver, phone=phone, address=address)
