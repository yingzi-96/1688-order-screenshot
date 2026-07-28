from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import OrderSummary, ReceiverInfo
from .receiver import parse_receiver_info
from .storage import category_output_dir


CLASSIFY_LABELS = {
    "none": "不分类",
    "receiver": "收货人",
    "phone": "手机号",
}


def capture_order(
    page: Any,
    context: Any,
    order: OrderSummary,
    base_output_dir: str | Path,
    classify_by: str,
    retries: int,
    open_order_page: Callable[..., None],
    screenshot_order: Callable[..., tuple[Path, str]],
    get_page_text: Callable[[Any], str],
    manual_waiter: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    extract_payment: Callable[[Any], dict] | None = None,
) -> dict:
    if classify_by not in CLASSIFY_LABELS:
        raise ValueError(f"不支持的分类方式: {classify_by}")

    last_error = ""
    last_receiver = ReceiverInfo()
    last_category_dir = ""
    last_payment = {"amount": "", "status": "未读取", "raw": ""}
    read_time = ""

    for attempt in range(1, max(1, retries) + 1):
        if should_stop and should_stop():
            raise RuntimeError("任务已停止")
        try:
            print(f"[{order.order_id}] 开始处理，第 {attempt}/{max(1, retries)} 次尝试。")
            open_order_page(page, context, order.order_id, manual_waiter=manual_waiter)
            last_receiver = parse_receiver_info(page, get_page_text(page))
            category_dir = category_output_dir(base_output_dir, last_receiver, classify_by)
            last_category_dir = str(category_dir)
            print(
                f"[{order.order_id}] 收货信息：收货人={last_receiver.receiver or '未识别'}，"
                f"手机号={last_receiver.phone or '未识别'}，分类目录={category_dir}"
            )

            if extract_payment:
                read_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    last_payment = extract_payment(page) or {"amount": "", "status": "未读取", "raw": ""}
                    log_amount = str(last_payment.get("amount", "")).replace("¥", "￥")
                    print(
                        f"[{order.order_id}] 实付款读取结果: "
                        f"{last_payment.get('status', '')} {log_amount}"
                    )
                except Exception as payment_exc:
                    last_payment = {"amount": "", "status": f"读取失败: {payment_exc}", "raw": ""}
                    print(f"[{order.order_id}] 实付款读取失败: {payment_exc}")

            screenshot_path, method = screenshot_order(page, order.order_id, category_dir)
            print(f"[{order.order_id}] 截图成功: {screenshot_path} ({method})")
            return {
                "订单号": order.order_id,
                "订单状态": order.status,
                "下单时间": order.created_at,
                "收货人": last_receiver.receiver,
                "手机号": last_receiver.phone,
                "地址": last_receiver.address,
                "实付款金额": last_payment.get("amount", ""),
                "金额读取状态": last_payment.get("status", "未读取"),
                "读取时间": read_time,
                "提取参考文本": last_payment.get("raw", ""),
                "分类方式": CLASSIFY_LABELS[classify_by],
                "分类目录": str(category_dir),
                "截图文件": str(screenshot_path),
                "截图方式": method,
                "截图结果": "成功",
                "失败原因": "",
            }
        except Exception as exc:
            if should_stop and should_stop():
                raise RuntimeError("任务已停止") from exc
            last_error = str(exc)
            print(f"[{order.order_id}] 本次处理失败: {exc}")
            if attempt < max(1, retries):
                time.sleep(random.uniform(1, 3))

    return {
        "订单号": order.order_id,
        "订单状态": order.status,
        "下单时间": order.created_at,
        "收货人": last_receiver.receiver,
        "手机号": last_receiver.phone,
        "地址": last_receiver.address,
        "实付款金额": last_payment.get("amount", ""),
        "金额读取状态": last_payment.get("status", "未读取"),
        "读取时间": read_time,
        "提取参考文本": last_payment.get("raw", ""),
        "分类方式": CLASSIFY_LABELS[classify_by],
        "分类目录": last_category_dir,
        "截图文件": "",
        "截图方式": "",
        "截图结果": "失败",
        "失败原因": last_error,
    }