"""公共剧本库：所有商家只读共享的剧本元数据层。

分层规则：
  - 公共库是**全局**资产（无 tenant_id）：Excel 模式一个共享文件，
    数据库模式一张全局表；所有商家可见、可检索，但**没有写权**。
  - 商家私有库（scripts 表）保持原语义不变；同 name_key 时**私有遮蔽公共**
    （merge_with_shadow），商家用独家本/桌游覆盖公共条目的显示。
  - 公共行的 script_id 带 `pub-` 前缀（make_public_script_id）。
    前缀本身就是公共/私有判别标记：coerce 往返不会丢额外列，因此
    合并帧只需标准 SCRIPTS_COLUMNS。草稿/场次**永不**引用 pub- 行
    （session_intake 负责不落 script_id）。

数据来源与写入：
  - 商家永远写不进公共库（upsert_script 只写租户 store）。
  - 公共库的写入只有两条路：运营方通过导入通道
    （app/store/import_public_scripts.py）合并，和空库时的策展种子
    （ensure_public_seed）。种子没有标记文件——守卫就是库本身的
    空/非空：清库后重灌是期望行为。

非目标（本轮明确不做）：
  - assignment.py / stages.py / dm_control.py 的名字索引仍只覆盖私有库——
    它们索引的是场次行上的剧名快照，不是库元数据查询，接入公共库
    没有收益只有回归风险。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import PUBLIC_SCRIPTS_XLSX, PUBLIC_SEED_CSV
from app.tools.scripts import (
    SCRIPTS_COLUMNS,
    coerce_scripts,
    make_script_id,
    normalize_script_name,
)
from app.tools.storage import is_missing_value

PUBLIC_COLUMNS: list[str] = SCRIPTS_COLUMNS + ["origin", "imported_at"]

# 每轮种子守卫按解析后的路径去重；库为空时允许重灌（无独立标记文件）。
_SEED_GUARD: set[str] = set()
_SEED_LOCK = threading.Lock()

_ORIGIN_SEP = "; "


def make_public_script_id(script_name: Any) -> str:
    """公共库行的稳定 ID：`pub-` 前缀 + 名称哈希。"""
    return "pub-" + make_script_id(script_name)


def is_public_script_id(script_id: Any) -> bool:
    if is_missing_value(script_id):
        return False
    return str(script_id).startswith("pub-")


def _name_key(value: Any) -> str:
    return normalize_script_name(value).casefold()


def _empty_public() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in PUBLIC_COLUMNS})


def coerce_public_scripts(df: pd.DataFrame) -> pd.DataFrame:
    """整形公共库表：标准列 + origin/imported_at 两列补齐。"""
    base = coerce_scripts(df)
    out = pd.DataFrame(index=base.index)
    for col in SCRIPTS_COLUMNS:
        out[col] = base[col]
    extra = df.copy()
    for col in ("origin", "imported_at"):
        if col not in extra.columns:
            extra[col] = pd.NA
    out["origin"] = extra["origin"].astype("string").fillna("")
    out["imported_at"] = pd.to_datetime(extra["imported_at"], errors="coerce")
    return out


def empty_public_scripts() -> pd.DataFrame:
    return coerce_public_scripts(_empty_public())


def load_public_scripts(path: Path | None = None) -> pd.DataFrame:
    """读公共库；文件不存在/损坏时返回空表（不阻塞登录）。"""
    target = Path(path) if path is not None else PUBLIC_SCRIPTS_XLSX
    if not target.exists():
        return empty_public_scripts()
    try:
        df = pd.read_excel(target, dtype=object)
    except (OSError, ValueError):
        return empty_public_scripts()
    return coerce_public_scripts(df)


def save_public_scripts(df: pd.DataFrame, path: Path | None = None) -> None:
    """原子写入公共库（tmp + os.replace），读方永远看到旧或新、无撕裂。"""
    target = Path(path) if path is not None else PUBLIC_SCRIPTS_XLSX
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = coerce_public_scripts(df)
    tmp = target.with_name(target.name + ".tmp")
    clean.to_excel(tmp, index=False)
    os.replace(tmp, target)


def _is_blank(value: Any) -> bool:
    if is_missing_value(value):
        return True
    return not str(value).strip()


def _merge_origin(stored: Any, incoming: Any) -> str:
    parts: list[str] = []
    for value in (stored, incoming):
        if _is_blank(value):
            continue
        for piece in str(value).split(_ORIGIN_SEP):
            piece = piece.strip()
            if piece and piece not in parts:
                parts.append(piece)
    return _ORIGIN_SEP.join(parts)


def apply_merge(
    existing_df: pd.DataFrame,
    incoming_df: pd.DataFrame,
    *,
    now: Any = None,
) -> tuple[pd.DataFrame, dict[str, int], list[dict[str, Any]]]:
    """按 name_key 合并入库：新增/更新/跳过；空值不覆盖已有值。

    Excel 与 SQL 后端共用这一份实现。缺 script_name 或 origin 的行进
    errors，不静默丢弃。origin 变更时保留全部来源（"; " 连接）。
    """
    existing = coerce_public_scripts(existing_df)
    incoming = coerce_public_scripts(incoming_df)
    moment = pd.Timestamp(now) if now is not None else pd.Timestamp.now().floor("s")
    if moment.tzinfo is not None:
        moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
    moment = moment.floor("s")

    stats = {"added": 0, "updated": 0, "skipped": 0}
    errors: list[dict[str, Any]] = []

    merged = existing.copy()
    index_by_key: dict[str, int] = {}
    for idx, row in merged.iterrows():
        key = _name_key(row.get("script_name"))
        if key:
            index_by_key[key] = int(idx)

    for pos, (_, row) in enumerate(incoming.iterrows(), start=1):
        name = normalize_script_name(row.get("script_name"))
        origin = "" if _is_blank(row.get("origin")) else str(row.get("origin")).strip()
        if not name:
            errors.append({"row": pos, "script_name": "", "reason": "缺 script_name"})
            continue
        if not origin:
            errors.append({"row": pos, "script_name": name, "reason": "缺 origin（来源标注必填）"})
            continue
        key = _name_key(name)

        updates: dict[str, Any] = {}
        for col in ("aliases", "script_type", "notes", "source_text"):
            value = row.get(col)
            if not _is_blank(value):
                updates[col] = str(value).strip()
        for col in ("total_seats", "duration_minutes", "list_price"):
            value = row.get(col)
            if not _is_blank(value):
                updates[col] = value

        if key in index_by_key:
            idx = index_by_key[key]
            changed = False
            for col, value in updates.items():
                if _is_blank(merged.at[idx, col]):
                    merged.at[idx, col] = value
                    changed = True
            new_origin = _merge_origin(merged.at[idx, "origin"], origin)
            if new_origin != ("" if _is_blank(merged.at[idx, "origin"]) else str(merged.at[idx, "origin"]).strip()):
                merged.at[idx, "origin"] = new_origin
                changed = True
            # 名称补全：已有行缺名而 incoming 有名（理论上 name_key 命中即有名，保底）
            if _is_blank(merged.at[idx, "script_name"]):
                merged.at[idx, "script_name"] = name
                changed = True
            if changed:
                merged.at[idx, "updated_at"] = moment
                merged.at[idx, "imported_at"] = moment
                stats["updated"] += 1
            else:
                stats["skipped"] += 1
            continue

        record: dict[str, Any] = {col: pd.NA for col in PUBLIC_COLUMNS}
        record["script_id"] = make_public_script_id(name)
        record["script_name"] = name
        record["origin"] = origin
        record["created_at"] = moment
        record["updated_at"] = moment
        record["imported_at"] = moment
        for col, value in updates.items():
            record[col] = value
        index_by_key[key] = len(merged)
        merged.loc[len(merged)] = record
        stats["added"] += 1

    return coerce_public_scripts(merged), stats, errors


def merge_public_scripts(
    incoming_df: pd.DataFrame,
    *,
    path: Path | None = None,
    now: Any = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Excel 后端：读-合并-原子写。返回 (stats, errors)。"""
    existing = load_public_scripts(path)
    merged, stats, errors = apply_merge(existing, incoming_df, now=now)
    if stats["added"] or stats["updated"]:
        save_public_scripts(merged, path)
    return stats, errors


def merge_with_shadow(
    private_df: pd.DataFrame,
    public_df: pd.DataFrame | None,
) -> pd.DataFrame:
    """私有 + 公共合并帧：同 name_key 时私有遮蔽公共；输出仅标准列。

    公共行保留 `pub-` script_id，消费者用 is_public_script_id 区分来源。
    """
    private = coerce_scripts(private_df)
    if public_df is None or (hasattr(public_df, "empty") and public_df.empty):
        return private
    public = coerce_public_scripts(public_df)

    private_keys: set[str] = set()
    for _, row in private.iterrows():
        key = _name_key(row.get("script_name"))
        if key:
            private_keys.add(key)

    rows = private.to_dict("records")
    for record in public.to_dict("records"):
        key = _name_key(record.get("script_name"))
        if key and key in private_keys:
            continue
        rows.append({col: record.get(col) for col in SCRIPTS_COLUMNS})
    out = pd.DataFrame(rows, columns=SCRIPTS_COLUMNS)
    return coerce_scripts(out)


def _read_seed_frame(seed_path: Path) -> pd.DataFrame:
    """读种子 CSV；列名与 PUBLIC_COLUMNS 对齐，缺列补空。"""
    if seed_path.suffix.lower() == ".csv":
        df = pd.read_csv(seed_path, dtype=object)
    else:
        df = pd.read_excel(seed_path, dtype=object)
    for col in PUBLIC_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[PUBLIC_COLUMNS]


def ensure_public_seed(
    *,
    path: Path | None = None,
    seed_path: Path | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """公共库为空时灌入策展种子；非空时 no-op。

    进程级守卫按路径去重：同一进程只检查一次；跨进程并发下种子合并
    本身按 name_key 幂等，重复灌入无害。
    """
    target = Path(path) if path is not None else PUBLIC_SCRIPTS_XLSX
    seed = Path(seed_path) if seed_path is not None else PUBLIC_SEED_CSV
    guard_key = str(target.resolve()) if target.parent.exists() else str(target)
    with _SEED_LOCK:
        if guard_key in _SEED_GUARD:
            return {"seeded": False, "reason": "already_checked", "added": 0, "updated": 0, "skipped": 0}
    existing = load_public_scripts(target)
    if not existing.empty:
        with _SEED_LOCK:
            _SEED_GUARD.add(guard_key)
        return {"seeded": False, "reason": "not_empty", "added": 0, "updated": 0, "skipped": 0}
    if not seed.exists():
        with _SEED_LOCK:
            _SEED_GUARD.add(guard_key)
        return {"seeded": False, "reason": "seed_file_missing", "added": 0, "updated": 0, "skipped": 0}

    seed_frame = _read_seed_frame(seed)
    stats, errors = merge_public_scripts(seed_frame, path=target, now=now)
    with _SEED_LOCK:
        _SEED_GUARD.add(guard_key)
    return {
        "seeded": True,
        "added": stats["added"],
        "updated": stats["updated"],
        "skipped": stats["skipped"],
        "errors": errors,
    }


def reset_seed_guard() -> None:
    """测试用：清空进程级种子守卫。"""
    with _SEED_LOCK:
        _SEED_GUARD.clear()
