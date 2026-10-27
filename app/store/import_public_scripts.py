"""公共剧本库导入通道 CLI。

用法：
    python -m app.store.import_public_scripts <file> [--dry-run] [--data-dir data]

接受 .json（对象数组）或 .csv；字段映射兼容采集脚本输出（vshang66 列表页
格式）与手工整理表。每行必须有 script_name 与 origin（来源标注）；缺任一
进 errors 不导入。按 name_key 幂等合并：重复导入第二次 added=0。

存储模式与 Web 侧同源：WEB_STORAGE_BACKEND=database（或设置了 DATABASE_URL
且未显式指定 excel）走数据库公共库；否则走 data/public_scripts.xlsx。
输出绝不包含 DATABASE_URL 或任何密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import DATA_DIR, PUBLIC_SCRIPTS_XLSX
from app.tools.public_library import PUBLIC_COLUMNS, coerce_public_scripts
from app.tools.scripts import _parse_int  # 复用官方校验，不重写解析
from app.tools.storage import is_missing_value

# 采集格式里常见的一行多值文本 → 规范字段的容错解析。
_DURATION_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~—–]\s*(\d+(?:\.\d+)?)")
_SEATS_RE = re.compile(r"(\d+)\s*[人位本]")
_GENDER_SPLIT_RE = re.compile(r"[（(]\s*\d+\s*[男女]\s*[/、,，]?\s*\d+\s*[男女]\s*[)）]")


def parse_duration_text(value: Any) -> tuple[Any, str]:
    """"7-8小时" → 区间中值分钟；"6小时"/"240分钟"/"4小时（以内）" 直接解析。"""
    if is_missing_value(value):
        return None, ""
    text = str(value).strip()
    if not text:
        return None, ""
    match = _DURATION_RANGE_RE.search(text)
    if match:
        low = float(match.group(1))
        high = float(match.group(2))
        midpoint = (low + high) / 2
        hours = "小时" in text or "h" in text.casefold()
        minutes = midpoint * 60 if hours else midpoint
        parsed, error = _parse_int(
            round(minutes), field="duration_minutes", minimum=15, maximum=720, duration=True
        )
        if error:
            return None, f"时长无法识别：{text!r}"
        return parsed, f"时长原文：{text}"
    # 站点偶带括号补充（"4小时（以内）"）：截到括号前再解析。
    cut = re.split(r"[（(]", text, maxsplit=1)[0].strip() or text
    parsed, error = _parse_int(cut, field="duration_minutes", minimum=15, maximum=720, duration=True)
    if error:
        return None, f"时长无法识别：{text!r}"
    if cut != text:
        return parsed, f"时长原文：{text}"
    return parsed, ""


def parse_seats_text(value: Any) -> tuple[Any, str]:
    """"6人（2男4女）" / "8人本" → 人数 6/8；保留性别比到备注。"""
    if is_missing_value(value):
        return None, ""
    text = str(value).strip()
    if not text:
        return None, ""
    gender_note = ""
    gender = _GENDER_SPLIT_RE.search(text)
    if gender:
        gender_note = f"性别比：{gender.group(0).strip('（）()')}"
    match = _SEATS_RE.search(text)
    raw = match.group(1) if match else text
    parsed, error = _parse_int(raw, field="total_seats", minimum=1, maximum=50)
    if error:
        return None, f"人数无法识别：{text!r}"
    return parsed, gender_note


# 宽松列名 → 规范列名。
_COLUMN_ALIASES: dict[str, str] = {
    "script_name": "script_name",
    "name": "script_name",
    "剧本名": "script_name",
    "剧本名称": "script_name",
    "书名": "script_name",
    "title": "script_name",
    "aliases": "aliases",
    "别名": "aliases",
    "script_type": "script_type",
    "type": "script_type",
    "类型": "script_type",
    "状态": "script_type",
    "total_seats": "total_seats",
    "seats": "total_seats",
    "人数": "total_seats",
    "角色": "total_seats",
    "duration_minutes": "duration_minutes",
    "duration": "duration_minutes",
    "时长": "duration_minutes",
    "list_price": "list_price",
    "price": "list_price",
    "价格": "list_price",
    "notes": "notes",
    "备注": "notes",
    "作者": "notes",
    "author": "notes",
    "origin": "origin",
    "来源": "origin",
    "source": "origin",
    "收录时间": "collected_at",
    "collected_at": "collected_at",
}


def load_input(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit("JSON 输入必须是对象数组")
        return [dict(item) for item in raw if isinstance(item, dict)]
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=object)
        return df.to_dict("records")
    raise SystemExit(f"不支持的输入格式：{path.suffix}（只接受 .json / .csv）")


def normalize_rows(rows: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """宽松行 → 规范 PUBLIC_COLUMNS 行；无法解析的进 errors。"""
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for pos, raw in enumerate(rows, start=1):
        mapped: dict[str, Any] = {}
        for key, value in raw.items():
            target = _COLUMN_ALIASES.get(str(key).strip().lower()) or _COLUMN_ALIASES.get(str(key).strip())
            if target:
                mapped.setdefault(target, value)

        name = "" if is_missing_value(mapped.get("script_name")) else str(mapped.get("script_name")).strip()
        origin = "" if is_missing_value(mapped.get("origin")) else str(mapped.get("origin")).strip()
        if not name:
            errors.append({"row": pos, "script_name": "", "reason": "缺 script_name"})
            continue
        if not origin:
            errors.append({"row": pos, "script_name": name, "reason": "缺 origin（来源标注必填）"})
            continue

        record: dict[str, Any] = {col: pd.NA for col in PUBLIC_COLUMNS}
        record["script_name"] = name
        record["origin"] = origin

        for col in ("aliases", "script_type"):
            value = mapped.get(col)
            if not is_missing_value(value) and str(value).strip():
                record[col] = str(value).strip()

        notes_bits: list[str] = []
        seats_value, seats_note = parse_seats_text(mapped.get("total_seats"))
        if seats_value is not None:
            record["total_seats"] = seats_value
        elif seats_note:
            errors.append({"row": pos, "script_name": name, "reason": seats_note})
        if seats_note:
            notes_bits.append(seats_note)

        duration_value, duration_note = parse_duration_text(mapped.get("duration_minutes"))
        if duration_value is not None:
            record["duration_minutes"] = duration_value
        elif duration_note:
            errors.append({"row": pos, "script_name": name, "reason": duration_note})
        if duration_note and duration_value is not None:
            notes_bits.append(duration_note)

        price_raw = mapped.get("list_price")
        if not is_missing_value(price_raw) and str(price_raw).strip():
            price_value, price_error = _parse_int(
                price_raw, field="list_price", minimum=0, maximum=100_000
            )
            if price_error:
                errors.append({"row": pos, "script_name": name, "reason": price_error})
            else:
                record["list_price"] = price_value

        notes = [] if is_missing_value(mapped.get("notes")) else [str(mapped.get("notes")).strip()]
        collected = mapped.get("collected_at")
        if not is_missing_value(collected) and str(collected).strip():
            notes_bits.append(f"收录时间：{str(collected).strip()}")
        all_notes = "；".join(bit for bit in [*notes, *notes_bits] if bit)
        if all_notes:
            record["notes"] = all_notes[:500]

        records.append(record)

    frame = coerce_public_scripts(pd.DataFrame(records, columns=PUBLIC_COLUMNS)) if records else coerce_public_scripts(pd.DataFrame(columns=PUBLIC_COLUMNS))
    return frame, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入公共剧本库（JSON/CSV，按名称幂等合并）")
    parser.add_argument("file", help="待导入的 .json 或 .csv 文件")
    parser.add_argument("--dry-run", action="store_true", help="只预览逐行决定，不写库")
    parser.add_argument("--data-dir", default=None, help=f"Excel 模式数据目录（默认 {DATA_DIR}）")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"输入文件不存在：{path}")
        return 2

    rows = load_input(path)
    frame, normalize_errors = normalize_rows(rows)
    print(f"读取 {len(rows)} 行；可导入 {len(frame)} 行，格式错误 {len(normalize_errors)} 行。")
    for err in normalize_errors[:20]:
        print(f"  ! 第 {err['row']} 行《{err.get('script_name') or '?'}》：{err['reason']}")
    if len(normalize_errors) > 20:
        print(f"  … 其余 {len(normalize_errors) - 20} 条格式错误略")

    backend = (os.getenv("WEB_STORAGE_BACKEND", "").strip() or "").lower()
    database_url = os.getenv("DATABASE_URL", "").strip()
    use_database = backend == "database" or (backend != "excel" and bool(database_url))

    if use_database:
        from sqlalchemy import create_engine

        from app.store.engine import ensure_schema
        from app.store.public_sql import SqlPublicLibrary

        engine = create_engine(database_url)
        ensure_schema(engine)
        library = SqlPublicLibrary(engine)
    else:
        from app.store.public_sql import ExcelPublicLibrary

        data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR
        target = data_dir / PUBLIC_SCRIPTS_XLSX.name if data_dir != DATA_DIR else PUBLIC_SCRIPTS_XLSX
        library = ExcelPublicLibrary(target)

    if args.dry_run:
        existing = library.load()
        existing_keys = {
            str(row.get("script_name") or "").strip().casefold()
            for _, row in existing.iterrows()
        }
        print(f"[dry-run] 目标公共库现有 {len(existing)} 条；预估：")
        added = updated_or_skip = 0
        for _, row in frame.iterrows():
            key = str(row.get("script_name") or "").strip().casefold()
            if key in existing_keys:
                updated_or_skip += 1
            else:
                added += 1
        print(f"  新增 {added} 条；已存在（更新/跳过）{updated_or_skip} 条。未写库。")
        return 0

    stats, merge_errors = library.merge(frame)
    print(
        f"导入完成：新增 {stats['added']}，更新 {stats['updated']}，"
        f"跳过（无变化）{stats['skipped']}，错误 {len(normalize_errors) + len(merge_errors)}。"
    )
    for err in merge_errors[:20]:
        print(f"  ! 第 {err.get('row')} 行《{err.get('script_name') or '?'}》：{err['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
