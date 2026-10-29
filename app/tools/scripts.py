"""工具 8：剧本库（剧本档案、别名、缺失字段与缩写候选）。

为什么单独一张表：
  场次表回答「哪一天、几点、谁约了」，剧本库回答「这个剧本本身是什么」。
  同一个剧本可以有很多场次，玩家人数、时长、类型不应该在场次表里重复维护；
  店长用缩写沟通时，也需要一个稳定的地方保存「缩写 → 全名」。

设计边界：
  - 本工具**不调用模型，也不上网**。模型可以给店长联想候选，但必须由店长确认
    全名后再调用 upsert；本工具只负责存取和本地匹配，不替模型猜。
  - 允许先保存残缺档案：只要知道全名或至少一个别名，就能先落一条草稿；
    必填字段缺失时返回 missing_fields，下一次带上 script_id 继续补。
  - 缩写候选会合并「剧本库」与「场次历史」两类来源；场次历史只读，不写回。
  - 剧本名称只做空白折叠和大小写归一，不做拼音匹配。正常匹配（精确/前缀/
    互相包含）**命中就返回**；一条都没有时才走兜底：先查「确认过的全局
    剧名映射」，再给字符 bigram 近似候选。兜底结果一律带
    ``match_quality``，模型据此知道该不该先请店长确认，避免把「年轮」和
    「年轮（情感本）」当成同一个本。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from pathlib import Path
from warnings import catch_warnings, simplefilter
from typing import Any, Iterable

import pandas as pd

from app.config import SCRIPTS_XLSX
from app.tools.storage import is_missing_value, load_sessions, store_lock

SCRIPTS_COLUMNS: list[str] = [
    "script_id",          # 稳定 ID：完整名称的哈希，或草稿的 draft-xxxxxxxx
    "script_name",        # 店长确认过的剧本全名；草稿可以为空
    "aliases",            # 别名/缩写，多个用 " / " 连接
    "script_type",        # 情感 / 推理 / 恐怖 / 欢乐 / 机制 / 阵营 / 还原
    "total_seats",        # 适合人数（几人本）
    "duration_minutes",   # 时长（分钟）
    "list_price",         # 参考原价；实际场次价格以 sessions 为准
    "notes",              # 店长备注
    "source_text",        # 原始消息，便于溯源
    "created_at",
    "updated_at",
]

# 「完整档案」的必填字段。aliases / list_price / notes 是可选补充。
REQUIRED_FIELDS: tuple[str, ...] = (
    "script_name",
    "script_type",
    "total_seats",
    "duration_minutes",
)

FIELD_LABELS: dict[str, str] = {
    "script_name": "剧本全名",
    "script_type": "剧本类型",
    "total_seats": "适合人数（几人本）",
    "duration_minutes": "时长（分钟）",
    "aliases": "别名/缩写",
    "list_price": "参考原价",
    "notes": "备注",
}

PROFILE_COMPLETE = "已完整"
PROFILE_DRAFT = "待补全"
PROFILE_NAME_PENDING = "待确认名称"

_ALIAS_SPLIT_RE = re.compile(r"[/、,，;；|\n]+")
_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([^\d\s]*)\s*$")
_MAX_NAME = 80
_MAX_ALIAS = 40
_MAX_ALIASES = 12
_MAX_TYPE = 40
_MAX_NOTES = 500
_MAX_SOURCE = 4000

# 兜底候选最多给几条：再多多半是噪声，模型也没法逐条找店长确认。
_FALLBACK_LIMIT = 3
# 字符 bigram 重叠系数的下限。低于它基本只是「碰巧共用一个字」。
_SUGGEST_THRESHOLD = 0.5
# 至少共享这么多个 bigram 才考虑。只共用一个字的短名靠 1/2 = 0.5 就能
# 混进候选列表，而它和查询没有任何连续两字的相似片段。
_SUGGEST_MIN_OVERLAP = 2
# 查询短于 3 个字时 bigram 不可靠（《青搂》与《青楼》零交集），
# 宁可返回空也不给一串假候选。
_SUGGEST_MIN_QUERY = 3

logger = logging.getLogger(__name__)

# 正常匹配的 score → 给模型看的匹配强度。兜底的两种另有取值：
# learned（命中全局记忆）、suggested（bigram 猜的）、list（空查询）。
_QUALITY_BY_SCORE = ("exact", "prefix", "partial", "loose")


def _empty_scripts() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in SCRIPTS_COLUMNS})


def _coerce_scripts(df: pd.DataFrame) -> pd.DataFrame:
    """把剧本表整形成规范形态；缺失列补空、多余列丢弃。"""
    df = df.copy().reset_index(drop=True)
    for col in SCRIPTS_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[SCRIPTS_COLUMNS]

    for col in ("total_seats", "duration_minutes", "list_price"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("total_seats", "duration_minutes"):
        df[col] = df[col].astype("Int64")
    for col in ("created_at", "updated_at"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in (
        "script_id",
        "script_name",
        "aliases",
        "script_type",
        "notes",
        "source_text",
    ):
        df[col] = df[col].astype("string")
    return df


def coerce_scripts(df: pd.DataFrame) -> pd.DataFrame:
    """公开的剧本表整形入口，SQL 存储层复用同一套列与类型规则。"""
    return _coerce_scripts(df)


def empty_scripts() -> pd.DataFrame:
    return _coerce_scripts(_empty_scripts())


def load_scripts(
    path: Path | None = None,
    *,
    store: Any = None,
) -> pd.DataFrame:
    """读取剧本库；文件不存在时返回空表。"""
    if store is not None:
        return store.load_scripts()
    target = Path(path) if path is not None else SCRIPTS_XLSX
    if not target.exists():
        return _empty_scripts()
    try:
        df = pd.read_excel(target, dtype=object)
    except (OSError, ValueError):
        raise
    return _coerce_scripts(df)


def save_scripts(
    df: pd.DataFrame,
    path: Path | None = None,
    *,
    store: Any = None,
) -> None:
    """原子写入剧本库，避免写一半留下损坏文件。"""
    if store is not None:
        store.save_scripts(df)
        return
    target = Path(path) if path is not None else SCRIPTS_XLSX
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = _coerce_scripts(df)
    tmp = target.with_name(target.name + ".tmp")
    clean.to_excel(tmp, index=False)
    os.replace(tmp, target)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now().floor("s")


def _business_now(value: Any = None) -> pd.Timestamp:
    moment = pd.Timestamp(value) if value is not None else _now()
    if moment.tzinfo is not None:
        moment = moment.tz_convert("Asia/Shanghai").tz_localize(None)
    return moment.floor("s")


def normalize_script_name(value: Any) -> str:
    """剧本名只做空白折叠和长度限制，不改写括号、序号等原始写法。"""
    if is_missing_value(value):
        return ""
    text = str(value).strip()
    text = " ".join(text.split())
    return text[:_MAX_NAME]


def _name_key(value: Any) -> str:
    return normalize_script_name(value).casefold()


def make_script_id(script_name: Any) -> str:
    """完整名称的稳定 ID。没有名称时不要调用本函数。"""
    canonical = _name_key(script_name)
    if not canonical:
        raise ValueError("剧本名为空，无法生成 script_id")
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _new_draft_id(existing: Iterable[Any]) -> str:
    used = {str(v) for v in existing if not is_missing_value(v)}
    for _ in range(20):
        candidate = f"draft-{secrets.token_hex(4)}"
        if candidate not in used:
            return candidate
    raise RuntimeError("无法生成草稿 ID")


def split_aliases(value: Any) -> list[str]:
    """把别名文本拆成去重后的列表，支持 /、逗号、分号和竖线。"""
    if is_missing_value(value):
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = _ALIAS_SPLIT_RE.split(str(value))

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        alias = " ".join(str(item).strip().split())[:_MAX_ALIAS]
        if not alias:
            continue
        key = alias.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(alias)
        if len(out) >= _MAX_ALIASES:
            break
    return out


def _aliases_text(value: Any) -> str:
    return " / ".join(split_aliases(value))


def _merge_aliases(*values: Any) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for alias in split_aliases(value):
            key = alias.casefold()
            if key not in seen:
                seen.add(key)
                merged.append(alias)
    return " / ".join(merged[:_MAX_ALIASES])


def _clean_text(value: Any, *, limit: int) -> str:
    if is_missing_value(value):
        return ""
    text = str(value).strip()
    text = " ".join(text.split())
    return text[:limit]


def _parse_int(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
    duration: bool = False,
) -> tuple[int | None, str]:
    """解析人数/时长/价格，接受「8人」「4小时」「168元」这类常见写法。"""
    if is_missing_value(value):
        return None, ""
    if isinstance(value, bool):
        return None, f"{FIELD_LABELS[field]}不是有效数字"

    label = FIELD_LABELS[field]
    if isinstance(value, (int, float)):
        number = float(value)
        suffix = ""
    else:
        text = str(value).strip()
        match = _NUMBER_RE.fullmatch(text)
        if not match:
            return None, f"{label}无法识别：{text!r}"
        number = float(match.group(1))
        suffix = match.group(2).casefold()

    if duration:
        if suffix in {"小时", "h", "hr", "hrs", "hour", "hours"}:
            number *= 60
        elif suffix not in {"", "分钟", "分", "min", "mins", "minute", "minutes"}:
            return None, f"{label}单位无法识别：{suffix!r}"
    elif field == "total_seats":
        if suffix not in {"", "人", "位", "人本", "本"}:
            return None, f"{label}单位无法识别：{suffix!r}"
    elif field == "list_price":
        if suffix not in {"", "元", "块", "rmb", "cny"}:
            return None, f"{label}单位无法识别：{suffix!r}"

    parsed = int(round(number))
    if not minimum <= parsed <= maximum:
        return None, f"{label}需在 {minimum}..{maximum} 之间，当前为 {parsed}"
    return parsed, ""


def missing_script_fields(record: Any) -> list[str]:
    """返回仍缺的必填字段名；数值 0 不算有效人数/时长。"""
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    missing: list[str] = []
    if is_missing_value(getter("script_name")):
        missing.append("script_name")
    if is_missing_value(getter("script_type")):
        missing.append("script_type")
    for field in ("total_seats", "duration_minutes"):
        value = getter(field)
        if is_missing_value(value):
            missing.append(field)
            continue
        try:
            if float(value) <= 0:
                missing.append(field)
        except (TypeError, ValueError):
            missing.append(field)
    return missing


def profile_status(record: Any) -> str:
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    missing = missing_script_fields(record)
    if not missing:
        return PROFILE_COMPLETE
    if is_missing_value(getter("script_name")) and not is_missing_value(getter("aliases")):
        return PROFILE_NAME_PENDING
    return PROFILE_DRAFT


def next_question(missing: list[str], invalid: list[str] | None = None) -> str:
    bits: list[str] = []
    if missing:
        bits.append("还需要补：" + "、".join(FIELD_LABELS[field] for field in missing))
    if invalid:
        bits.append("这些值我没识别：" + "；".join(invalid))
    if not bits:
        return ""
    return "。".join(bits) + "。你直接按任意顺序告诉我就行，已经保存的字段不用重复。"


def _json_value(value: Any) -> Any:
    if is_missing_value(value):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat(sep=" ", timespec="minutes")
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


def script_to_dict(record: Any) -> dict[str, Any]:
    getter = record.get if hasattr(record, "get") else (lambda _: None)
    aliases = split_aliases(getter("aliases"))
    missing = missing_script_fields(record)
    out = {
        "script_id": _json_value(getter("script_id")) or "",
        "script_name": _json_value(getter("script_name")) or "",
        "aliases": aliases,
        "script_type": _json_value(getter("script_type")) or "",
        "total_seats": _json_value(getter("total_seats")),
        "duration_minutes": _json_value(getter("duration_minutes")),
        "list_price": _json_value(getter("list_price")),
        "notes": _json_value(getter("notes")) or "",
        "profile_status": profile_status(record),
        "missing_fields": missing,
        "updated_at": _json_value(getter("updated_at")) or "",
    }
    return out


def _session_history(sessions: pd.DataFrame | None) -> list[dict[str, Any]]:
    """从场次历史归纳剧本候选；只读，不写成剧本档案。"""
    if sessions is None or sessions.empty:
        return []
    frame = sessions.copy()
    if "script_name" not in frame.columns:
        return []
    frame = frame[~frame["script_name"].map(is_missing_value)]
    if frame.empty:
        return []
    frame["_name_key"] = frame["script_name"].map(_name_key)
    frame = frame[frame["_name_key"] != ""]

    out: list[dict[str, Any]] = []
    for key, group in frame.groupby("_name_key", sort=False):
        name = str(group["script_name"].iloc[0]).strip()
        if not name:
            continue
        script_type = ""
        if "script_type" in group.columns:
            types = [
                str(value).strip()
                for value in group["script_type"]
                if not is_missing_value(value) and str(value).strip()
            ]
            script_type = types[0] if types else ""
        total_seats = None
        if "total_seats" in group.columns:
            values = pd.to_numeric(group["total_seats"], errors="coerce").dropna()
            if not values.empty:
                total_seats = int(values.max())
        list_price = None
        if "list_price" in group.columns:
            values = pd.to_numeric(group["list_price"], errors="coerce").dropna()
            if not values.empty:
                list_price = float(values.max())

        record = {
            "script_id": "",
            "script_name": name,
            "aliases": [],
            "script_type": script_type,
            "total_seats": total_seats,
            "duration_minutes": None,
            "list_price": list_price,
            "notes": "",
            "profile_status": PROFILE_DRAFT,
            "missing_fields": [],
            "updated_at": "",
            "source": "场次历史",
        }
        record["missing_fields"] = missing_script_fields(record)
        record["profile_status"] = profile_status(record)
        out.append(record)
    return out


def _score_keys(query_key: str, keys: list[str]) -> int | None:
    """按已归一化的名字/别名键打分。

    ``_match_score`` 的对偶版本：调用方已经算好 keys 时用它，避免重复
    归一化。四档口径（0 精确 / 1 名字以查询开头 / 2 查询是名字的一段 /
    3 名字是查询的一段）必须与 ``_match_score`` 保持逐条一致，改动时
    两处一起改。
    """
    if not query_key:
        return 0
    if not keys:
        return None
    if query_key in keys:
        return 0
    if any(key.startswith(query_key) for key in keys):
        return 1
    if any(query_key in key for key in keys):
        return 2
    # 单字名不参与「名字 ⊂ 查询」这一档：库里有 49 个单字本名，任何长查询
    # 只要含那个字就会被判成 loose 命中（实测「病娇男孩的精分曰记」被
    # 「病」抢走，真名反而轮不到兜底）。单字查询仍可用第 0/1 档精确命中。
    if any(len(key) >= 2 and key in query_key for key in keys):
        return 3
    return None


def _match_score(query_key: str, candidate: dict[str, Any]) -> int | None:
    if not query_key:
        return 0
    name_key = _name_key(candidate.get("script_name"))
    alias_keys = [_name_key(alias) for alias in candidate.get("aliases") or []]
    keys = [key for key in [name_key, *alias_keys] if key]
    if not keys:
        return None
    if query_key in keys:
        return 0
    if any(key.startswith(query_key) for key in keys):
        return 1
    if any(query_key in key for key in keys):
        return 2
    # 与 _score_keys 保持一致：单字名不参与「名字 ⊂ 查询」这一档。
    if any(len(key) >= 2 and key in query_key for key in keys):
        return 3
    return None


def _bigrams(text: str) -> set[str]:
    """相邻两字组成的集合；单字文本没有 bigram，返回空集。"""
    if len(text) < 2:
        return set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _suggest_scripts(
    query_key: str,
    *,
    frame: pd.DataFrame,
    candidates: list[tuple[int, str, list[str], int]],
    limit: int,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """字符 bigram 近似候选；只用于「一条都没匹配上」的情况。

    相似度用重叠系数 ``|交集| / min(|A|,|B|)``，另加一条「至少共享 2 个
    bigram」。这两条一起用是量出来的，不是推出来的：拿真实库 5,809 行里
    的 429 个剧名各改一个字，只靠比例的话，只共用一个 bigram 的短名会把
    错条目塞进候选（135 → 192 条）；加上「至少 2 个」后噪声回到 135 条，
    召回不变。

    分母**不能**换成 max。max 更干净（93 条错条目），但它把「店长把 8 字
    全名缩成 4 字、还打错一个字」整类挡在门外——254 个「简称 + 错字」的
    查询里一个都兜不住（0/254），而下面这套兜住 177 个。简称本来就是店长
    最常用的口径，丢了它兜底就没有意义了。

    排序在分数之后比「与查询的字数差」，而不是按名字字母序：同分时应当
    先看更像同一条目的那个，字母序只是噪声。

    查询短于 3 个字直接放弃。实测《青搂》与《青楼》的 bigram 零交集，
    退化成单字比对会产生几十条误报，真名还排不进前四——给一串假候选
    比说「没找到」更糟。这类短名的错拼交给全局剧名记忆。

    候选池只含剧本库（私有 + 公共帧），**不含场次历史**：那部分条目在
    正常匹配阶段已经参与打分了，而且没有 ``frame.iloc[position]`` 可定位。
    只出现在某场历史里的名字打错字时，这里给不出候选。

    ``exclude`` 是已经出现在结果里的 name_key（loose 行）。必须在**截断
    之前**排除：名字是查询子串的条目重叠系数恒为 1.0，排序上永远压过真正
    打错字的目标；3 个名额一旦被它们占满，去重阶段再删掉，候选就一条不剩。
    """
    if len(query_key) < _SUGGEST_MIN_QUERY:
        return []
    query_grams = _bigrams(query_key)
    if not query_grams:
        return []

    scored: list[tuple[float, int, str, int]] = []
    for position, name_key, keys, name_len in candidates:
        if exclude and name_key in exclude:
            continue
        best = 0.0
        for key in keys:
            grams = _bigrams(key)
            if not grams:
                continue
            overlap = len(query_grams & grams)
            if overlap < _SUGGEST_MIN_OVERLAP:
                continue
            best = max(best, overlap / min(len(query_grams), len(grams)))
        if best < _SUGGEST_THRESHOLD:
            continue
        scored.append((best, abs(name_len - len(query_key)), name_key, position))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    out: list[dict[str, Any]] = []
    for _, _, _, position in scored[: min(_FALLBACK_LIMIT, limit)]:
        item = _annotate_source(script_to_dict(frame.iloc[position]))
        item["match_quality"] = "suggested"
        out.append(item)
    return out


def _learned_script(
    query_key: str,
    *,
    frame: pd.DataFrame,
    public: pd.DataFrame | None,
    learned_aliases: Any,
) -> dict[str, Any] | None:
    """查全局学到的剧名映射；查不到或任何异常都返回 None。

    映射只可能指向公共行，但本店可能用同名私有档案遮蔽了那条公共行。
    所以不能直接按 ``script_id`` 取行（遮蔽后就取不到了），而是先拿到
    公共行的名字，再按 name_key 在**合并后的帧**里解析——这样私有档案
    才会按既有规则赢。
    """
    if learned_aliases is None:
        return None
    try:
        script_id = learned_aliases.lookup(query_key)
    except Exception as exc:  # noqa: BLE001 - 记忆不可用不该让搜索失败
        logger.warning("learned alias lookup failed: %s", type(exc).__name__)
        return None
    if not script_id:
        return None
    if public is None or getattr(public, "empty", True):
        return None
    if "script_id" not in public.columns or "script_name" not in public.columns:
        return None
    hit = public[public["script_id"].astype("string").fillna("") == script_id]
    if hit.empty:
        return None
    key = _name_key(hit.iloc[0].get("script_name"))
    if not key or frame.empty or "script_name" not in frame.columns:
        return None
    for position, name in enumerate(frame["script_name"].tolist()):
        if _name_key(name) == key:
            item = _annotate_source(script_to_dict(frame.iloc[position]))
            item["match_quality"] = "learned"
            return item
    return None


def _fallback_scripts(
    query_key: str,
    *,
    frame: pd.DataFrame,
    candidates: list[tuple[int, str, list[str], int]],
    limit: int,
    public: pd.DataFrame | None,
    learned_aliases: Any,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """正常匹配为空时的兜底：先查全局记忆，再给 bigram 近似候选。

    整个兜底是一次「额外线索」，不是搜索本身：内部吞掉异常并记一行
    warning。记忆或公共库出问题应当表现为少一条线索，而不是搜索报错。

    ``exclude`` 只影响 bigram 候选——它们是猜的，不该重复已经在结果里的
    条目，也不该为了给它们腾位置去挤掉真实命中。
    """
    if not query_key:
        return []
    try:
        learned = _learned_script(
            query_key,
            frame=frame,
            public=public,
            learned_aliases=learned_aliases,
        )
        if learned is not None:
            return [learned]
        return _suggest_scripts(
            query_key,
            frame=frame,
            candidates=candidates,
            limit=limit,
            exclude=exclude,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底不该让搜索失败
        logger.warning("script fallback failed: %s", type(exc).__name__)
        return []


def _merged_with_public(
    frame: pd.DataFrame,
    public: pd.DataFrame | None,
) -> pd.DataFrame:
    """私有帧 + 公共库遮蔽合并；public 为空时原样返回。

    懒导入：public_library 依赖本模块的列定义，顶层互导会成环。
    """
    if public is None or getattr(public, "empty", True):
        return frame
    from app.tools.public_library import merge_with_shadow

    return merge_with_shadow(frame, public)


def _annotate_source(item: dict[str, Any]) -> dict[str, Any]:
    """给候选条目打来源标记：公共行 read_only，私有行保持原语义。"""
    from app.tools.public_library import is_public_script_id

    if is_public_script_id(item.get("script_id")):
        item["source"] = "公共库"
        item["read_only"] = True
    else:
        item["source"] = "剧本库"
    return item


def find_scripts(
    query: Any = "",
    *,
    scripts: pd.DataFrame | None = None,
    sessions: pd.DataFrame | None = None,
    path: Path | None = None,
    limit: int = 10,
    store: Any = None,
    public: pd.DataFrame | None = None,
    learned_aliases: Any = None,
) -> list[dict[str, Any]]:
    """在剧本库和场次历史里找候选；不联网、不猜全名。

    public 提供时与私有库合并（同 name_key 私有遮蔽公共）；
    公共行 source="公共库"、read_only=True。upsert 永远只写私有库。

    ``learned_aliases`` 是全局剧名记忆（有 ``lookup(key) -> script_id | None``
    的任意对象，见 app/store/script_aliases.py）。它**只在正常匹配为空时**
    被查询一次，绝不并进候选的别名列表——否则学到的键可能让某个本在
    ``(score, name_len, name_key)`` 排序里顶掉真正的精确命中。

    每条结果都带 ``match_quality``：``exact`` / ``prefix`` / ``partial`` /
    ``loose`` 表示正常匹配的强弱，``learned`` 表示命中全局记忆，
    ``suggested`` 表示 bigram 猜的候选，``list`` 表示空查询的列表输出。
    """
    frame = (
        load_scripts(path, store=store)
        if scripts is None
        else _coerce_scripts(scripts)
    )
    frame = _merged_with_public(frame, public)
    limit = max(1, min(int(limit), 50))
    query_key = _name_key(query)

    candidates: list[tuple[int, str, list[str], int]] = []
    library_names: set[str] = set()
    # 打分和排序只需要「名字 + 别名 + 名字长度」，完整档案等条目进了
    # 结果集再造。原实现给全部 5809 行都跑 script_to_dict（每行十几次
    # 字段归一化），实测 309ms，而最终最多只返回 limit（≤50）条。
    #
    # 按列取值而不是 to_dict("records")：后者要把 13 列全部转成 Python
    # 原生对象（每格一次 maybe_box_native，含 Timestamp 拆箱），实测
    # 96.7ms；这里只碰 2 列，1.4ms。
    # 两个列表保持与 frame 同长，缺列时也能逐行对齐。
    names = (
        frame["script_name"].tolist()
        if "script_name" in frame.columns
        else [None] * len(frame)
    )
    aliases_col = (
        frame["aliases"].tolist()
        if "aliases" in frame.columns
        else [None] * len(frame)
    )
    for position, (name, raw_aliases) in enumerate(zip(names, aliases_col)):
        key = _name_key(name)
        if key:
            library_names.add(key)
        keys = [key] if key else []
        for alias in split_aliases(raw_aliases):
            alias_key = _name_key(alias)
            if alias_key:
                keys.append(alias_key)
        name_len = len(name) if isinstance(name, str) else 0
        candidates.append((position, key, keys, name_len))

    matched: list[tuple[int, int, str, Any, bool]] = []
    for position, name_key, keys, name_len in candidates:
        score = _score_keys(query_key, keys)
        if score is None:
            continue
        matched.append((score, name_len, name_key, position, False))
    for item in _session_history(
        sessions if sessions is not None else load_sessions(store=store)
    ):
        key = _name_key(item.get("script_name"))
        if key and key not in library_names:
            score = _match_score(query_key, item)
            if score is None:
                continue
            name = item.get("script_name") or ""
            # 场次历史条目已是完整记录（带 source="场次历史"），不再过
            # script_to_dict / _annotate_source，否则来源会被改成「剧本库」。
            matched.append((score, len(name), key, item, True))

    matched.sort(key=lambda item: (item[0], item[1], item[2]))
    # 只有进入结果集的条目才需要完整档案：逐行取 Series（script_to_dict
    # 本来就支持 Series），最多 limit 次，而不是给全表建 dict。
    out: list[dict[str, Any]] = []
    for score, _, _, row_index, is_history in matched[:limit]:
        # 空查询的 score 恒为 0，不能据此标成 exact，否则模型会误以为
        # 「这就是精确命中」。
        quality = "list" if not query_key else _QUALITY_BY_SCORE[score]
        if is_history:
            # 复制而不是原地改：这些 dict 来自调用方传入的场次记录。
            out.append(dict(row_index, match_quality=quality))
        else:
            item = _annotate_source(script_to_dict(frame.iloc[row_index]))
            item["match_quality"] = quality
            out.append(item)
    if out:
        # 全是 loose 的时候补一次兜底。score 3 只说明「查询里夹着这个短剧名」，
        # 长查询里恰好含一个 2–3 字本名时，真正的目标（很可能打错一个字）
        # 永远轮不到候选——库里有《病》和《病娇男孩的精分日记》时，「病娇
        # 男孩的精分曰记」会先被《病》按 loose 命中。单字名已排除在这档外，
        # 2–3 字名没有。只在「一条像样的匹配都没有」时才跑，仍是低频路径。
        #
        # 不按 ``len(out) < limit`` 设门：loose 行凑满名额时，恰恰是最需要
        # 兜底的场合（5 个两字本被一段长查询同时 loose 命中）。学到的命中
        # 排在 loose 行前面——它是整串查询的精确键；bigram 候选仍排在后面，
        # 它是猜的，不该顶掉一个真实存在的短名命中。
        if query_key and all(
            item.get("match_quality") == "loose" for item in out
        ):
            extra = _fallback_scripts(
                query_key,
                frame=frame,
                candidates=candidates,
                limit=limit,
                public=public,
                learned_aliases=learned_aliases,
                # 已经在结果里的名字不参与候选打分：它们是「查询里夹着的
                # 短剧名」，bigram 重叠恒为 1.0，会把 3 个名额全占满。
                exclude={
                    key
                    for key in (
                        _name_key(item.get("script_name")) for item in out
                    )
                    if key
                },
            )
            if extra:
                learned_rows = [
                    item
                    for item in extra
                    if item.get("match_quality") == "learned"
                ]
                guessed_rows = [
                    item
                    for item in extra
                    if item.get("match_quality") != "learned"
                ]
                seen = set()
                merged: list[dict[str, Any]] = []
                for item in [*learned_rows, *out, *guessed_rows]:
                    name_key = _name_key(item.get("script_name"))
                    if name_key and name_key in seen:
                        continue
                    seen.add(name_key)
                    merged.append(item)
                out = merged[:limit]
        return out
    # 一条都没匹配上才走兜底。兜底只读，且内部吞掉异常：记忆或公共库
    # 出问题应当是「搜不到额外线索」，而不是搜索报错。
    return _fallback_scripts(
        query_key,
        frame=frame,
        candidates=candidates,
        limit=limit,
        public=public,
        learned_aliases=learned_aliases,
    )


def get_script(
    script_id: str,
    *,
    path: Path | None = None,
    store: Any = None,
    public: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """按 script_id 或完整名称取档案；私有未命中时回落公共库。

    公共行返回 source="公共库"、read_only=True；该结果只读展示，
    写入仍走 upsert_script（只写私有库）。
    """
    target = str(script_id or "").strip()
    if not target:
        return None
    frame = load_scripts(path, store=store)
    hit = frame[frame["script_id"].astype("string").fillna("") == target]
    if hit.empty:
        # 也允许用完整名称当查询键，方便模型在只拿到名字时更新。
        key = _name_key(target)
        hit = frame[frame["script_name"].map(_name_key) == key]
    if not hit.empty:
        return script_to_dict(hit.iloc[0])

    if public is None or getattr(public, "empty", True):
        return None
    from app.tools.public_library import coerce_public_scripts

    public_frame = coerce_public_scripts(public)
    pub_hit = public_frame[
        public_frame["script_id"].astype("string").fillna("") == target
    ]
    if pub_hit.empty:
        key = _name_key(target)
        pub_hit = public_frame[public_frame["script_name"].map(_name_key) == key]
    if pub_hit.empty:
        return None
    item = script_to_dict(pub_hit.iloc[0])
    item["source"] = "公共库"
    item["read_only"] = True
    return item


def _find_row_index(frame: pd.DataFrame, script_id: str) -> int | None:
    if not script_id or frame.empty:
        return None
    matches = frame.index[
        frame["script_id"].astype("string").fillna("") == script_id
    ].tolist()
    return int(matches[0]) if matches else None


def _find_name_index(frame: pd.DataFrame, name: str) -> int | None:
    key = _name_key(name)
    if not key or frame.empty:
        return None
    matches = frame.index[frame["script_name"].map(_name_key) == key].tolist()
    return int(matches[0]) if matches else None


def _infer_from_sessions(
    name: str,
    sessions: pd.DataFrame | None,
) -> dict[str, tuple[Any, str]]:
    """返回 {字段: (值, 中文名)}；只补能从历史确定的事实，不猜时长。"""
    if sessions is None or sessions.empty or "script_name" not in sessions.columns:
        return {}
    key = _name_key(name)
    matched = sessions[sessions["script_name"].map(_name_key) == key]
    if matched.empty:
        return {}

    inferred: dict[str, tuple[Any, str]] = {}
    if "script_type" in matched.columns:
        types = [
            str(value).strip()
            for value in matched["script_type"]
            if not is_missing_value(value) and str(value).strip()
        ]
        if types:
            inferred["script_type"] = (types[0], "剧本类型")
    if "total_seats" in matched.columns:
        values = pd.to_numeric(matched["total_seats"], errors="coerce").dropna()
        if not values.empty:
            inferred["total_seats"] = (int(values.max()), "适合人数")
    if "list_price" in matched.columns:
        values = pd.to_numeric(matched["list_price"], errors="coerce").dropna()
        if not values.empty:
            inferred["list_price"] = (float(values.max()), "参考原价")
    return inferred


def _upsert_script_impl(
    *,
    script_id: str | None = None,
    script_name: Any = None,
    aliases: Any = None,
    script_type: Any = None,
    total_seats: Any = None,
    duration_minutes: Any = None,
    list_price: Any = None,
    notes: Any = None,
    source_text: Any = None,
    path: Path | None = None,
    now: Any = None,
    sessions: pd.DataFrame | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """upsert_script 的实现；调用方必须先持有写锁。"""
    frame = load_scripts(path, store=store)
    warnings: list[str] = []
    invalid: list[str] = []

    name = normalize_script_name(script_name)
    aliases_text = _aliases_text(aliases)
    type_text = _clean_text(script_type, limit=_MAX_TYPE)
    notes_text = _clean_text(notes, limit=_MAX_NOTES)
    source_text_value = _clean_text(source_text, limit=_MAX_SOURCE)

    seats_value, seats_error = _parse_int(
        total_seats, field="total_seats", minimum=1, maximum=50
    )
    duration_value, duration_error = _parse_int(
        duration_minutes,
        field="duration_minutes",
        minimum=15,
        maximum=720,
        duration=True,
    )
    price_value, price_error = _parse_int(
        list_price, field="list_price", minimum=0, maximum=100_000
    )
    for error in (seats_error, duration_error, price_error):
        if error:
            invalid.append(error)

    requested_id = str(script_id or "").strip()
    if not name and not aliases_text and not requested_id:
        raise ValueError(
            "请先告诉我剧本全名，或至少一个缩写/别名；"
            "如果是在补已有草稿，请带上 script_id。"
        )

    # --- 定位目标行：优先显式 script_id，其次同名档案 ---
    target_index: int | None = None
    if requested_id:
        target_index = _find_row_index(frame, requested_id)
        if target_index is None and name:
            same_name_index = _find_name_index(frame, name)
            if same_name_index is not None:
                # 显式 ID 与同名档案冲突：让 Agent 先确认用哪一个，避免重复。
                existing_id = str(frame.at[same_name_index, "script_id"])
                raise ValueError(
                    f"剧本库里已有《{name}》（script_id={existing_id}），"
                    "请直接用这个 script_id 更新，避免创建重复档案。"
                )
    elif name:
        target_index = _find_name_index(frame, name)

    # 显式 ID 已存在时，如果新名称属于另一条档案，不允许把两条合并/重名。
    if target_index is not None and name:
        same_name_index = _find_name_index(frame, name)
        if same_name_index is not None and same_name_index != target_index:
            existing_id = str(frame.at[same_name_index, "script_id"])
            raise ValueError(
                f"剧本库里已有《{name}》（script_id={existing_id}），"
                "请直接用这个 script_id 更新，避免创建重复档案。"
            )

    created = target_index is None
    if created:
        if requested_id:
            new_id = requested_id
        elif name:
            new_id = make_script_id(name)
        else:
            new_id = _new_draft_id(frame["script_id"].tolist())
        record = {col: pd.NA for col in SCRIPTS_COLUMNS}
        record["script_id"] = new_id
        record["created_at"] = _business_now(now)
        # 不用 pd.concat：新增行大多为空，pandas 会发 FutureWarning。
        # .loc 在部分 pandas 版本内部同样走 concat，这里只屏蔽该已知告警。
        with catch_warnings():
            simplefilter("ignore", FutureWarning)
            frame.loc[len(frame)] = record
        target_index = len(frame) - 1
        warnings.append("已新建剧本档案。")
    else:
        assert target_index is not None
        existing_name = frame.at[target_index, "script_name"]
        if name and not is_missing_value(existing_name):
            if _name_key(existing_name) != _name_key(name):
                warnings.append(
                    f"已把剧本名从《{existing_name}》更新为《{name}》。"
                )
        elif name:
            warnings.append("已补全剧本全名，别名会继续保留。")

    assert target_index is not None
    row = frame.loc[target_index]

    # --- 先填名称/别名/文本字段；已有的非空值不被空值覆盖 ---
    if name:
        frame.at[target_index, "script_name"] = name
    if aliases_text:
        frame.at[target_index, "aliases"] = _merge_aliases(
            row.get("aliases"), aliases_text
        )
    if type_text:
        frame.at[target_index, "script_type"] = type_text
    if seats_value is not None:
        frame.at[target_index, "total_seats"] = seats_value
    if duration_value is not None:
        frame.at[target_index, "duration_minutes"] = duration_value
    if price_value is not None:
        frame.at[target_index, "list_price"] = price_value
    if notes_text:
        frame.at[target_index, "notes"] = notes_text
    if source_text_value:
        frame.at[target_index, "source_text"] = source_text_value

    # --- 从场次历史补齐能确定的字段；只补空缺，不覆盖已有值 ---
    used_history: list[str] = []
    current_name = frame.at[target_index, "script_name"]
    if not is_missing_value(current_name):
        inferred = _infer_from_sessions(str(current_name), sessions)
        for field, (value, label) in inferred.items():
            if is_missing_value(frame.at[target_index, field]):
                frame.at[target_index, field] = value
                used_history.append(label)
        if used_history:
            warnings.append("已从场次历史补齐：" + "、".join(used_history) + "。")

    frame.at[target_index, "updated_at"] = _business_now(now)
    frame = _coerce_scripts(frame)
    save_scripts(frame, path, store=store)

    record = frame.loc[target_index]
    missing = missing_script_fields(record)
    status = profile_status(record)
    question = next_question(missing, invalid)
    if status == PROFILE_COMPLETE:
        warnings.append("剧本档案已完整，可以用于排期。")
    elif status == PROFILE_NAME_PENDING:
        warnings.append("已保存缩写草稿；全名还需要店长确认。")

    data = script_to_dict(record)
    data["created"] = created
    data["warnings"] = warnings
    data["invalid_fields"] = invalid
    data["next_question"] = question
    data["used_session_history"] = used_history
    return data


def upsert_script(
    *,
    script_id: str | None = None,
    script_name: Any = None,
    aliases: Any = None,
    script_type: Any = None,
    total_seats: Any = None,
    duration_minutes: Any = None,
    list_price: Any = None,
    notes: Any = None,
    source_text: Any = None,
    path: Path | None = None,
    now: Any = None,
    sessions: pd.DataFrame | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """新增/补齐一条剧本档案。

    允许残缺：只要 script_name 或 aliases 至少有一个，就能先保存；
    缺失字段会原样返回，不会为了「完整」而编造值。
    传入 store 时走数据库；不传时保持原 Excel 语义。
    """
    with store_lock(store):
        return _upsert_script_impl(
            script_id=script_id,
            script_name=script_name,
            aliases=aliases,
            script_type=script_type,
            total_seats=total_seats,
            duration_minutes=duration_minutes,
            list_price=list_price,
            notes=notes,
            source_text=source_text,
            path=path,
            now=now,
            sessions=sessions,
            store=store,
        )


def list_scripts(
    *,
    path: Path | None = None,
    limit: int = 50,
    store: Any = None,
    public: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    frame = _merged_with_public(load_scripts(path, store=store), public)
    if frame.empty:
        return []
    limit = max(1, min(int(limit), 100))
    frame = frame.sort_values(["script_name", "script_id"], na_position="last")
    # 同 find_scripts：整批转换，避免 iterrows 逐行构造 Series。
    return [
        _annotate_source(script_to_dict(record))
        for record in frame.head(limit).to_dict("records")
    ]
