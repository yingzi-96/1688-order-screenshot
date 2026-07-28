from __future__ import annotations

from datetime import date, datetime, timedelta


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def resolve_time_range(preset: str, custom_start: str = "", custom_end: str = "", today: date | None = None) -> tuple[date, date]:
    current = today or date.today()
    if preset == "today":
        return current, current
    if preset == "yesterday":
        target = current - timedelta(days=1)
        return target, target
    if preset == "last3":
        return current - timedelta(days=2), current
    if preset == "last7":
        return current - timedelta(days=6), current
    if preset == "month":
        return current.replace(day=1), current
    if preset == "custom":
        if not custom_start or not custom_end:
            raise ValueError("自定义时间范围需要填写开始日期和结束日期")
        start = parse_date(custom_start)
        end = parse_date(custom_end)
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        return start, end
    raise ValueError(f"不支持的时间范围: {preset}")
