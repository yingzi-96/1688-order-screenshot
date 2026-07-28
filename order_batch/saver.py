from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd


RESULT_COLUMNS = [
    "订单号", "订单状态", "下单时间", "收货人", "手机号", "地址", "实付款金额",
    "金额读取状态", "读取时间", "分类方式", "分类目录", "截图文件", "截图方式",
    "截图结果", "失败原因", "提取参考文本",
]

PAYMENT_COLUMNS = [
    "订单号", "订单状态", "下单时间", "收货人", "手机号", "地址", "实付款金额",
    "金额读取状态", "截图结果", "截图文件", "截图方式", "失败原因", "读取时间",
    "提取参考文本",
]


def _frame_with_columns(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column in columns:
        if column not in frame.columns:
            frame[column] = ""
    return frame[columns]


def save_payment_tables(rows: list[dict], output_dir: str | Path) -> list[Path]:
    output = Path(output_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    groups: dict[Path, list[dict]] = {}
    for row in rows:
        category_value = str(row.get("分类目录", "")).strip()
        category_dir = Path(category_value).expanduser() if category_value else output
        try:
            category_dir.resolve().relative_to(output.resolve())
        except ValueError:
            category_dir = output
        groups.setdefault(category_dir, []).append(row)

    saved: list[Path] = []
    for category_dir, category_rows in groups.items():
        category_dir.mkdir(parents=True, exist_ok=True)
        table_path = category_dir / "订单实付款汇总.xlsx"
        _frame_with_columns(category_rows, PAYMENT_COLUMNS).to_excel(table_path, index=False)
        saved.append(table_path)
    return saved


def save_batch_results(rows: list[dict], output_dir: str | Path, stats: dict, date_range: dict) -> tuple[Path, Path]:
    output = Path(output_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = output / f"已支付订单截图结果_{stamp}.xlsx"
    json_path = output / f"已支付订单截图结果_{stamp}.json"

    _frame_with_columns(rows, RESULT_COLUMNS).to_excel(excel_path, index=False)
    json_path.write_text(
        json.dumps(
            {"统计": stats, "时间范围": date_range, "结果": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return excel_path, json_path