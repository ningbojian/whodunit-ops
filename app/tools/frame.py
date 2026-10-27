"""新业务表的通用整形与 Excel sidecar 读写。

资源、草稿、渠道单、阶段模板和场控表都需要同一套规则：
缺列补齐、多余列丢弃、数值/时间/布尔类型归一、空表返回完整列。
把这套规则集中在这里，避免每张表各写一份、类型语义慢慢漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.tools.storage import is_missing_value


@dataclass(frozen=True)
class FrameSpec:
    name: str
    columns: list[str]
    numerics: tuple[str, ...] = ()
    integers: tuple[str, ...] = ()
    datetimes: tuple[str, ...] = ()
    booleans: tuple[str, ...] = ()
    strings: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)


def _as_bool(value: Any) -> Any:
    if is_missing_value(value):
        return pd.NA
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "y", "是", "启用", "有效"}:
        return True
    if text in {"0", "false", "no", "n", "否", "停用", "无效"}:
        return False
    return pd.NA


def coerce_frame(df: pd.DataFrame | None, spec: FrameSpec) -> pd.DataFrame:
    if df is None:
        df = pd.DataFrame(columns=spec.columns)
    out = df.copy().reset_index(drop=True)
    for column in spec.columns:
        if column not in out.columns:
            out[column] = pd.NA
    out = out[spec.columns]

    for column in spec.numerics:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in spec.integers:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in spec.datetimes:
        # SQL 的 UTCDateTime 读回来是带时区的 UTC，Excel sidecar 是 naive；
        # 两者混在一列里时，pd.to_datetime 会把 naive 值静默变成 NaT。
        # 统一成 naive UTC，保证 Excel / SQLite / Postgres 三端一致。
        out[column] = (
            pd.to_datetime(out[column], errors="coerce", utc=True)
            .dt.tz_localize(None)
        )
    for column in spec.booleans:
        out[column] = out[column].map(_as_bool).astype("boolean")
    for column in spec.strings:
        out[column] = out[column].astype("string")

    for column, default in spec.defaults.items():
        if column not in out.columns:
            continue
        # 空表时 map 在 pandas 的 StringDtype 上可能仍返回 string dtype，
        # 直接 .any() 会抛 “Cannot perform reduction 'any' with string dtype”。
        # 先显式转成可空布尔，再判断。
        missing = (
            out[column]
            .map(is_missing_value)
            .astype("boolean")
            .fillna(False)
        )
        if bool(missing.any()):
            out.loc[missing.astype(bool), column] = default
    return out


def empty_frame(spec: FrameSpec) -> pd.DataFrame:
    return coerce_frame(pd.DataFrame(columns=spec.columns), spec)


def load_frame(
    path: Path | None,
    default_path: Path,
    spec: FrameSpec,
) -> pd.DataFrame:
    target = Path(path) if path is not None else default_path
    if not target.exists():
        return empty_frame(spec)
    return coerce_frame(pd.read_excel(target, dtype=object), spec)


def save_frame(
    df: pd.DataFrame,
    path: Path | None,
    default_path: Path,
    spec: FrameSpec,
) -> None:
    target = Path(path) if path is not None else default_path
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = coerce_frame(df, spec)
    tmp = target.with_name(target.name + ".tmp")
    clean.to_excel(tmp, index=False)
    tmp.replace(target)


def json_safe(value: Any) -> Any:
    """把 pandas / numpy 值转成可 JSON 序列化的原生值。"""
    if value is None:
        return None
    if is_missing_value(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError, AttributeError):
            pass
    return value


__all__ = [
    "FrameSpec",
    "coerce_frame",
    "empty_frame",
    "json_safe",
    "load_frame",
    "save_frame",
]
