from __future__ import annotations

from datetime import date, datetime

from .models import BatchStats, OrderSummary


PAID_KEYWORDS = (
    "已支付",
    "已付款",
    "等待卖家发货",
    "待卖家发货",
    "待发货",
    "发货超时",
    "卖家已发货",
    "已发货",
    "等待买家确认收货",
    "等待收货",
    "待收货",
    "交易成功",
    "交易完成",
    "待评价",
    "已完成",
)
UNPAID_KEYWORDS = ("未支付", "未付款", "等待买家付款", "待付款", "等待付款")
CANCELLED_KEYWORDS = ("已取消", "已关闭", "交易关闭", "订单关闭", "已作废")


def classify_status(status: str, raw_text: str = "") -> str:
    text = status.strip() or raw_text
    if any(keyword in text for keyword in CANCELLED_KEYWORDS):
        return "cancelled"
    if any(keyword in text for keyword in UNPAID_KEYWORDS):
        return "unpaid"
    if any(keyword in text for keyword in PAID_KEYWORDS):
        return "paid"
    return "other"


def parse_order_date(value: str) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def filter_paid_orders(orders: list[OrderSummary], start: date, end: date) -> tuple[list[OrderSummary], BatchStats]:
    paid: list[OrderSummary] = []
    stats = BatchStats(scanned_total=len(orders))
    for order in orders:
        created = parse_order_date(order.created_at)
        if created is None:
            stats.ignored_date_unknown += 1
            continue
        if not (start <= created <= end):
            stats.ignored_out_of_range += 1
            continue
        stats.total += 1
        kind = classify_status(order.status, order.raw_text)
        if kind == "paid":
            paid.append(order)
            stats.paid += 1
        elif kind == "unpaid":
            stats.ignored_unpaid += 1
        elif kind == "cancelled":
            stats.ignored_cancelled += 1
        else:
            stats.ignored_other += 1
    return paid, stats
