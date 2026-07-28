from __future__ import annotations

import re
from pathlib import Path

from .models import ReceiverInfo


def sanitize_folder_name(value: str, fallback: str = "未识别") -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (value or "").strip())
    value = value.rstrip(". ")
    return value[:80] or fallback


def category_output_dir(base_dir: str | Path, receiver: ReceiverInfo, classify_by: str = "none") -> Path:
    base = Path(base_dir).expanduser()
    if classify_by not in {"none", "receiver", "phone"}:
        raise ValueError(f"不支持的分类方式: {classify_by}")
    if classify_by == "none":
        base.mkdir(parents=True, exist_ok=True)
        return base
    if classify_by == "phone":
        category = sanitize_folder_name(receiver.phone, "未识别手机号")
    else:
        category = sanitize_folder_name(receiver.receiver, "未识别收货人")
    path = base / category
    path.mkdir(parents=True, exist_ok=True)
    return path
