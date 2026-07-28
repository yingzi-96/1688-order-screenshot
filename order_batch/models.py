from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class OrderSummary:
    order_id: str
    status: str = ""
    created_at: str = ""
    detail_url: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReceiverInfo:
    receiver: str = ""
    phone: str = ""
    address: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchStats:
    scanned_total: int = 0
    total: int = 0
    paid: int = 0
    ignored_unpaid: int = 0
    ignored_cancelled: int = 0
    ignored_other: int = 0
    ignored_out_of_range: int = 0
    ignored_date_unknown: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
