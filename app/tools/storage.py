"""工具 6：表格存储与读取（pandas + openpyxl 读写 Excel）。

========================= 本文件管两张表 =========================

  data/sessions.xlsx   场次表     —— 有哪些场、什么时候开、几位、多少钱
  data/bookings.xlsx   预约明细表 —— 谁约了哪一场

为什么要拆成两张表，而不是在场次表里直接写一个「已约人数」：
  因为人数是个会变、且需要追溯来源的量。写成单个数字，
  「同一条预约消息被转发两次」就会把 1 个人算成 2 个人，而且事后查不出是谁。
  拆成明细后：同一玩家在同一场次只有一条有效记录（靠 booking_id 保证），
  转发几次都是 1 个人；并且随时能回答「这场都有谁」。

========================= 本文件的分层原则 =========================

本模块**只放纯函数，不出现任何 LangChain 的 @tool 装饰器**。

为什么这样分层：
  业务逻辑与「暴露给 LLM 的工具接口」是两件事。
    - 纯函数可以用 pytest 直接断言：不需要 API key、不花钱、毫秒级、结果稳定；
    - @tool 包装层只是薄薄一层适配，把函数签名翻译成模型能看懂的 schema。

  如果把逻辑直接写进 @tool 函数体，单测就必须绕过 LangChain 的对象才能跑，
  又慢又脆。所以正确顺序是：**先把纯函数写对、测透，第 9 步组装时再套 @tool。**

======================= 贯穿全文件的一条不变量 =======================

  booked_seats 和 remaining_seats 都是**派生字段**：
  它们唯一的真相来源是 bookings 表，文件里存的数字一律不信任、每次读出来重算。

        bookings 中 status=有效 的行
              │  sum(seats) group by session_id
              ▼
        sessions.booked_seats
              │  total_seats - booked_seats
              ▼
        sessions.remaining_seats

  好处：店长手工在 Excel 里改了数字也污染不了系统 —— 下次读出来会被覆盖回正确值。
  注意两步有先后依赖：必须先算 booked，再算 remaining，顺序反了剩余空位就是错的。
"""

from __future__ import annotations

import hashlib
import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from app.config import BOOKINGS_XLSX, SESSIONS_XLSX

# ===========================================================================
# 一、两张表的「形状」定义
# ===========================================================================

SESSIONS_COLUMNS: list[str] = [
    "session_id",       # 主键：门店+剧本+开场时间 的哈希，见 make_session_id()
    "store_name",       # 门店名
    "script_name",      # 剧本名
    "script_type",      # 剧本类型：情感 / 推理 / 恐怖 / 欢乐 / 机制 / 阵营 …
    "start_time",       # 开场时间
    "total_seats",      # 总位
    "booked_seats",     # 已约人数（派生字段，读取时由预约明细重算）
    "remaining_seats",  # 剩余空位（派生字段，由 total - booked 重算）
    "list_price",       # 原价
    "status",           # 待开场 / 已开场 / 已取消
    "source_text",      # 原始拼场文案，用于溯源
    "updated_at",       # 最近更新时间
]

BOOKINGS_COLUMNS: list[str] = [
    "booking_id",    # 主键：场次ID + 玩家昵称 的哈希，见 make_booking_id()
    "session_id",    # 外键，指向 sessions.session_id
    "player_name",   # 玩家昵称
    "seats",         # 占位人数，默认 1（代朋友一起约时 > 1）
    "status",        # 有效 / 已取消
    "booked_at",     # 首次预约时间（改约不覆盖，保留第一次的时间）
    "updated_at",    # 最近变更时间
    "source_text",   # 原始预约 / 跳车消息，溯源用
]

# --- 场次状态 ---
# 工具 3（过期过滤）和工具 4（高危识别）都会用到，所以定义在这里统一出口。
STATUS_PENDING = "待开场"
STATUS_STARTED = "已开场"
STATUS_CANCELLED = "已取消"
VALID_STATUS: set[str] = {STATUS_PENDING, STATUS_STARTED, STATUS_CANCELLED}

# --- 预约状态 ---
# 用「软删除」而不是真删行：跳车只把状态改成已取消。
# 删掉就查不出「这人原本约了、后来跳的」，出纠纷时没有痕迹可查。
BOOKING_ACTIVE = "有效"
BOOKING_CANCELLED = "已取消"
VALID_BOOKING_STATUS: set[str] = {BOOKING_ACTIVE, BOOKING_CANCELLED}

# 剧本类型只是「建议值」，不做强制校验：
# 不同门店的品类叫法不一样（有的叫"硬核推理"、有的叫"还原"），
# 强行白名单校验会把真实数据挡在门外。
COMMON_SCRIPT_TYPES: list[str] = ["情感", "推理", "恐怖", "欢乐", "机制", "阵营", "还原"]


def _derive_session_counts(df: pd.DataFrame) -> pd.DataFrame:
    """场次表的派生字段：由 total 与 booked 算出剩余空位。

    remaining_seats 本可以现算现用，但存下来让「查询剩余空位」更直接。
    代价是存在不一致风险 —— 所以任何写入路径都必须经过这里重算，
    调用方传入的 remaining_seats 会被无条件覆盖。
    """
    df["remaining_seats"] = (df["total_seats"] - df["booked_seats"]).astype("Int64")
    return df


@dataclass(frozen=True)
class _TableSpec:
    """一张 Excel 表的完整「形状」：列顺序 + 每列类型 + 派生字段规则。

    把这三样收在一处，读表和写表就走同一套逻辑。
    两张表共用这套机制，而不是各写一遍 —— 原子写入那套代码如果出现两份，
    将来只改一处就是隐患。
    """

    name: str
    columns: list[str]
    numeric: list[str] = field(default_factory=list)     # 数值列
    integers: list[str] = field(default_factory=list)    # 可空整数列
    datetimes: list[str] = field(default_factory=list)   # 时间列
    strings: list[str] = field(default_factory=list)     # 文本列
    derive: Callable[[pd.DataFrame], pd.DataFrame] | None = None


SESSIONS_SPEC = _TableSpec(
    name="场次表",
    columns=SESSIONS_COLUMNS,
    numeric=["total_seats", "booked_seats", "list_price"],
    integers=["total_seats", "booked_seats"],
    datetimes=["start_time", "updated_at"],
    strings=["session_id", "store_name", "script_name", "script_type", "status", "source_text"],
    derive=_derive_session_counts,
)

BOOKINGS_SPEC = _TableSpec(
    name="预约明细表",
    columns=BOOKINGS_COLUMNS,
    numeric=["seats"],
    integers=["seats"],
    datetimes=["booked_at", "updated_at"],
    strings=["booking_id", "session_id", "player_name", "status", "source_text"],
    derive=None,  # 预约表没有派生字段，它自己就是真相来源
)


# ===========================================================================
# 二、主键生成（工具 2「场次去重」会复用同一个函数）
# ===========================================================================

def normalize_start_time(value: Any) -> str:
    """把各种写法的开场时间归一成 'YYYY-MM-DD HH:MM'。

    为什么必须归一：
      同一场次，门店可能写成 "2026-10-20 19:30"、"2026/10/20 19:30"，
      Excel 单元格里还可能是 datetime 对象。若不归一，
      make_session_id 会为同一个场次算出不同的哈希，**去重直接失效**。

    只保留到分钟：门店排场次不会精确到秒，秒级差异通常来自解析噪音。
    """
    if isinstance(value, str):
        value = value.strip()

    # errors="raise"：解析不了就明确报错，不要静默变成 NaT。
    # 静默失败会让一个格式错误的场次悄悄变成"永不重复"的新记录。
    ts = pd.to_datetime(value, errors="raise")
    return ts.strftime("%Y-%m-%d %H:%M")


def make_session_id(store_name: str, script_name: str, start_time: Any) -> str:
    """用「门店 + 剧本 + 开场时间」生成稳定的 session_id。

    为什么用哈希而不是直接拼接原文：
      1. 联合键含中文，拼出来又长又可能含 Excel 不友好的字符；
      2. 固定长度（12 位）便于作为列展示；
      3. sha1 前 12 位十六进制 = 48 bit，本项目万级场次规模下碰撞概率可忽略。

    注意：只要三个输入归一化后完全一致，无论调用多少次、在哪台机器上，
    结果都相同 —— 这是去重能成立的前提。
    """
    canonical = (
        f"{str(store_name).strip()}"
        f"|{str(script_name).strip()}"
        f"|{normalize_start_time(start_time)}"
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def make_session_id_from_record(record: dict) -> str:
    """从一条场次记录（dict / Series）生成 session_id 的便捷入口。"""
    return make_session_id(
        record["store_name"],
        record["script_name"],
        record["start_time"],
    )


def make_booking_id(session_id: str, player_name: str) -> str:
    """用「场次ID + 玩家昵称」生成稳定的 booking_id —— 幂等性的地基。

    这个函数就是「防重复转发」机制本身：
      同一个人在同一场次反复预约，算出来的 booking_id 永远相同，
      于是 upsert 到同一行，人数不会从 1 变 2。

    为什么昵称也要 strip：微信昵称常带前后空格或特殊空白字符，
      不 strip 的话「张三」和「张三 」会被当成两个人，各占一个位子。
    """
    canonical = f"{str(session_id).strip()}|{str(player_name).strip()}"
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _now() -> pd.Timestamp:
    """当前时间，截断到秒。

    为什么截秒：Excel 的时间精度只到秒（毫秒/微秒会被丢掉），
    不截的话「写入前的值 == 读回来的值」这类断言会莫名其妙地失败。
    提前截断，内存里的值和文件里的值就永远一致。
    """
    return pd.Timestamp.now().floor("s")


# ===========================================================================
# 三、类型强制转换（两张表共用）
# ===========================================================================

def _coerce_table(df: pd.DataFrame, spec: _TableSpec) -> pd.DataFrame:
    """把 DataFrame 整形成 spec 描述的规范形态：列齐全、类型正确、派生字段重算。

    Excel 没有类型系统 —— 时间列读回来可能是 str，数字列可能是 str，
    空单元格读回来可能是 NaN / None / NaT。全项目所有读表结果都过这一道，
    下游工具就不必各自防御。

    副作用（有意为之）：多出来的列会被丢弃，缺失的列会被补上空值。
    """
    df = df.copy()

    # 补齐缺失列、裁掉多余列，保证列集合与顺序恒定
    for col in spec.columns:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[spec.columns]

    # --- 数值列 ---
    for col in spec.numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Int64（大写 I）是 pandas 的可空整数类型，能容纳 NaN；
    # 普通 int64 遇到空值会直接报错或把整列变成 float（8 位显示成 8.0）。
    for col in spec.integers:
        df[col] = df[col].astype("Int64")

    # --- 时间列 ---
    for col in spec.datetimes:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    # --- 文本列 ---
    for col in spec.strings:
        df[col] = df[col].astype("string")

    # --- 派生字段：一律重算，不信任传入值 ---
    if spec.derive is not None:
        df = spec.derive(df)

    return df


def _coerce_types(df: pd.DataFrame, spec: _TableSpec = SESSIONS_SPEC) -> pd.DataFrame:
    """_coerce_table 的别名，默认处理场次表（保留旧调用方式）。"""
    return _coerce_table(df, spec)


def _empty_table(spec: _TableSpec) -> pd.DataFrame:
    """返回一张空的、列齐全的表。"""
    return _coerce_table(pd.DataFrame(columns=spec.columns), spec)


# 存储层适配器也走同一套整形规则，避免 SQL / Excel 两套类型语义。
def coerce_sessions(df: pd.DataFrame) -> pd.DataFrame:
    return _coerce_table(df, SESSIONS_SPEC)


def coerce_bookings(df: pd.DataFrame) -> pd.DataFrame:
    return _coerce_table(df, BOOKINGS_SPEC)


def empty_sessions() -> pd.DataFrame:
    return _empty_table(SESSIONS_SPEC)


def empty_bookings() -> pd.DataFrame:
    return _empty_table(BOOKINGS_SPEC)


# ===========================================================================
# 四、身份判定与字段级合并（工具 2 与工具 6 共用，全项目只此一份）
# ===========================================================================
#
# 这里是「同一场次怎么合并」的唯一实现。工具 2（dedupe.py）负责策略语义
# ——分组、冲突报告、与现有表比对；本模块负责物理合并，因为 upsert 落盘时
# 必须调用同一份代码。策略文档写在 dedupe.py 顶部，实现放在这里，
# 两边不会漂移。
#
# 核心规则只有一条：**合并以字段为单位，最后一个非空值胜出**。
#
#   三条转发： [完整: 8人本 168 情感本]  [残缺: 只有身份字段]  [残缺+改价: 158]
#   结果：      8人本  158  情感本
#
# 空值永远不参与覆盖。这条规则同时解决两个看起来无关的问题：
#   1. 同一批里后一条残缺 —— 旧实现 drop_duplicates(keep="last") 会整行丢掉
#      前一条完整的 total_seats / list_price / script_type / source_text；
#   2. 跨批次更新 —— _coerce_types 给未提供的列填 NA，旧实现只保护
#      booked_seats 和 status，其余字段会被 NA 抹空。
# 两者其实是同一个 bug 的两种表现。

NOTE_STORE_MISSING = "门店缺失"
NOTE_SCRIPT_MISSING = "剧本缺失"
NOTE_TIME_MISSING = "开场时间缺失"
NOTE_TIME_UNPARSABLE = "开场时间无法解析"
NOTE_ID_MISMATCH = "session_id与联合键不一致"
NOTE_KEY_COLLISION = "联合键相同但session_id不同"

# 参与合并的字段：除主键和派生字段外的全部业务字段。
MERGE_FIELDS: list[str] = [c for c in SESSIONS_COLUMNS if c not in ("session_id", "remaining_seats")]

# 参与「冲突报告」的字段。刻意排除：
#   source_text —— 同一场转发三次，原文必然三份不同，算进去报告就永远非空；
#   updated_at  —— 每次写入都变；
#   remaining_seats —— 派生字段。
CONFLICT_FIELDS: list[str] = [
    "store_name", "script_name", "script_type", "start_time",
    "total_seats", "booked_seats", "list_price", "status",
]


def is_missing_value(value: Any) -> bool:
    """统一的「空值」判定：None / pd.NA / NaT / NaN / 空白字符串。

    **数值 0 是有效值**，不算空 —— 0 元的体验场、0 个空位都是真实数据。

    为什么必须统一：pd.NA == pd.NA 得到的是 pd.NA 而不是 True，
    裸用 == 比较会产出含 NA 的布尔掩码，拿去索引直接抛
    「Cannot mask with non-boolean array containing NA / NaN values」。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def session_identity(record: Any) -> tuple[str | None, str]:
    """算出一场次的联合键；算不出来时返回 (None, 原因)。

    必须在 _coerce_types **之前**调用：整形会把「时间无法解析」静默变成 NaT，
    之后就分不清「没填时间」和「填了句『下周三晚上』」了。

    也不能直接把空值丢给 make_session_id：
      str(pd.NA) 是字符串 "<NA>"，会让所有缺门店的记录撞成同一个键；
      pd.NaT.strftime() 直接抛 ValueError。
    """
    getter = record.get if hasattr(record, "get") else (lambda k: None)
    store = getter("store_name")
    script = getter("script_name")
    start = getter("start_time")

    if is_missing_value(store):
        return None, NOTE_STORE_MISSING
    if is_missing_value(script):
        return None, NOTE_SCRIPT_MISSING
    if is_missing_value(start):
        return None, NOTE_TIME_MISSING

    try:
        return make_session_id(store, script, start), ""
    except (ValueError, TypeError, OverflowError):
        return None, NOTE_TIME_UNPARSABLE


def _canonical_merge_value(field: str, value: Any) -> Any:
    """合并前把值归一，避免「格式不同但事实相同」被当成冲突。"""
    if field in ("start_time", "updated_at"):
        return pd.to_datetime(value, errors="coerce")
    if field in ("total_seats", "booked_seats", "list_price"):
        number = pd.to_numeric(value, errors="coerce")
        return None if pd.isna(number) else number
    return str(value).strip()


def values_equal(left: Any, right: Any, field: str) -> bool:
    """按字段类型比较两个值；两边都空算相等。"""
    left_missing = is_missing_value(left)
    right_missing = is_missing_value(right)
    if left_missing or right_missing:
        return left_missing and right_missing

    if field in ("start_time", "updated_at"):
        try:
            return normalize_start_time(left) == normalize_start_time(right)
        except (ValueError, TypeError, OverflowError):
            return False
    if field in ("total_seats", "booked_seats", "list_price"):
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return str(left).strip() == str(right).strip()


def merge_session_records(group: pd.DataFrame) -> tuple[dict[str, Any], tuple[str, ...]]:
    """把同一场次的多行合成一行：字段级，最后一个非空值胜出。

    返回 (合并后的字段 dict, 冲突字段名 tuple)。
    前置条件：group 已经过 _coerce_types（或至少列名齐全）。
    """
    merged: dict[str, Any] = {}
    observed: dict[str, list[Any]] = {f: [] for f in CONFLICT_FIELDS}

    for _, row in group.iterrows():
        for column in MERGE_FIELDS:
            value = row.get(column)
            if is_missing_value(value):
                continue
            value = _canonical_merge_value(column, value)
            if is_missing_value(value):
                continue
            merged[column] = value
            if column in observed and not any(
                values_equal(value, seen, column) for seen in observed[column]
            ):
                observed[column].append(value)

    # session_id 不参与 MERGE_FIELDS，单独取最后一个非空值。
    sids = [
        str(v).strip() for v in group.get("session_id", [])
        if not is_missing_value(v)
    ]
    merged["session_id"] = sids[-1] if sids else ""

    conflicts = tuple(f for f in CONFLICT_FIELDS if len(observed[f]) > 1)
    return merged, conflicts


def merge_session_batch(records: pd.DataFrame, *, key: str = "session_id") -> pd.DataFrame:
    """按 key 分组，组内用 merge_session_records 合并。

    输出 = SESSIONS_COLUMNS + merged_count + conflict_fields（key 若不是
    SESSIONS_COLUMNS 中的列，也会原样带回）。

    实现刻意用显式循环而不是 groupby.apply —— 后者返回的形状会随组数、
    列类型变化，还容易带 MultiIndex。
    """
    if records.empty:
        out = _empty_table(SESSIONS_SPEC)
        out["merged_count"] = pd.Series(dtype="Int64")
        out["conflict_fields"] = pd.Series(dtype="object")
        if key not in out.columns:
            out[key] = pd.Series(dtype="object")
        return out

    rows: list[dict[str, Any]] = []
    counts: list[int] = []
    conflicts: list[tuple[str, ...]] = []
    keys: list[Any] = []

    for group_key, group in records.groupby(key, sort=False, dropna=False):
        merged, conflict = merge_session_records(group)
        rows.append(merged)
        counts.append(len(group))
        conflicts.append(conflict)
        keys.append(group_key)

    # 注意顺序：_coerce_types 会按 SESSIONS_COLUMNS 裁列，
    # 所以分析列必须在整形之后再加。
    out = _coerce_types(pd.DataFrame(rows))
    out["merged_count"] = pd.array(counts, dtype="Int64")
    out["conflict_fields"] = pd.Series(conflicts, dtype="object")
    if key not in out.columns:
        out[key] = keys
    return out


def assign_session_keys(
    records: pd.DataFrame,
    identities: list[tuple[str | None, str]],
    *,
    strict: bool,
) -> pd.DataFrame:
    """给一批记录分配最终 session_id、分组键与放行备注。

    strict=True  —— 算不出联合键又没有显式 session_id 时抛 ValueError。
                    这是 storage 落盘前的最后一道闸：主键为空的行不能进
                    Excel，否则身份永久丢失，之后再也去不了重。
    strict=False —— 分配唯一哨兵键、放行并标注。这是 dedupe 分析层的行为：
                    让 LLM 看见「这行缺门店」并追问店长，而不是让整批崩掉。

    合并策略两边共用，**错误策略按层分开** —— 这个不对称是刻意的。
    """
    explicit = records["session_id"].fillna("").astype("string").str.strip().fillna("")
    natural = pd.Series([sid for sid, _ in identities], index=records.index, dtype="object")
    notes = pd.Series([note for _, note in identities], index=records.index, dtype="object")

    # 显式 ID 只填空、不覆盖；显式与联合键打架时保留显式值，但要标注。
    final = explicit.astype("object").copy()
    fill = (explicit == "") & natural.notna()
    final.loc[fill] = natural.loc[fill]

    mismatch = (explicit != "") & natural.notna() & (
        explicit.astype("object") != natural
    )
    notes = notes.mask(mismatch, NOTE_ID_MISMATCH)

    blank = final.fillna("").astype("string").str.strip() == ""
    if strict and blank.any():
        offenders = records.loc[blank, "script_name"].fillna("（未命名剧本）")
        raise ValueError(
            "有场次算不出 session_id，且没有显式提供："
            + "、".join(str(v) for v in offenders)
            + "。请先调用 dedupe_sessions 查看 dedupe_note，补齐门店 / 剧本 / 开场时间。"
        )

    # 空 key 各自分到唯一的哨兵组，绝不按空值互相撞键。
    group_key = final.fillna("").astype("string").str.strip()
    group_key.loc[blank] = [f"__pass__{i}" for i in records.index[blank]]

    out = records.copy()
    out["session_id"] = final.astype("string").fillna("")
    out["dedupe_note"] = notes.fillna("")
    out["_group_key"] = group_key
    return out


# ===========================================================================
# 五、整表读取 / 写入（两张表共用同一套原子写入机制）
# ===========================================================================

def _load_table(path: Path | None, spec: _TableSpec, default: Path) -> pd.DataFrame:
    """读取整张表。

    文件不存在时返回**空表**（列齐全），而不是抛异常 ——
    首次使用时表还没建是正常状态，调用方不必为此写特判。

    参数 path 可覆盖默认路径，测试用它指向临时文件，避免碰真实数据。
    """
    target = Path(path) if path is not None else default

    if not target.exists():
        return _empty_table(spec)

    # dtype=object 让 pandas 不要自作主张推断类型，
    # 后续统一由 _coerce_table 处理，避免"两套类型规则打架"。
    df = pd.read_excel(target, dtype=object)
    return _coerce_table(df, spec)


def _save_table(df: pd.DataFrame, path: Path | None, spec: _TableSpec, default: Path) -> None:
    """把整张表写回 Excel（原子写入）。

    原子写入的必要性：
      直接 df.to_excel(目标路径) 覆盖原文件，如果写到一半进程崩了 / 断电，
      会留下一个半截的损坏文件，**原来那批数据一起没了**。
      所以先写临时文件，写完整了再用 os.replace 一步换过去 ——
      这个操作在操作系统层面是原子的，要么全成功要么完全没动过。
    """
    target = Path(path) if path is not None else default
    target.parent.mkdir(parents=True, exist_ok=True)

    clean = _coerce_table(df, spec)  # 落盘前再整形一次，脏数据不许进文件

    tmp = target.with_name(target.name + ".tmp")
    clean.to_excel(tmp, index=False)
    os.replace(tmp, target)  # 原子替换


def store_lock(store: Any):
    if store is None:
        return nullcontext()
    lock = getattr(store, "write_lock", None)
    return lock() if callable(lock) else nullcontext()


def load_sessions(
    path: Path | None = None,
    *,
    store: Any = None,
) -> pd.DataFrame:
    """读取整张场次表（未接预约明细，booked_seats 是文件里存的值）。"""
    if store is not None:
        return store.load_sessions()
    return _load_table(path, SESSIONS_SPEC, SESSIONS_XLSX)


def save_sessions(
    df: pd.DataFrame,
    path: Path | None = None,
    *,
    store: Any = None,
) -> None:
    """把整张场次表写回存储（Excel 原子写入 / SQL 整表替换）。"""
    if store is not None:
        store.save_sessions(df)
        return
    _save_table(df, path, SESSIONS_SPEC, SESSIONS_XLSX)


def load_bookings(
    path: Path | None = None,
    *,
    store: Any = None,
) -> pd.DataFrame:
    """读取整张预约明细表。"""
    if store is not None:
        return store.load_bookings()
    return _load_table(path, BOOKINGS_SPEC, BOOKINGS_XLSX)


def save_bookings(
    df: pd.DataFrame,
    path: Path | None = None,
    *,
    store: Any = None,
) -> None:
    """把整张预约明细表写回存储（Excel 原子写入 / SQL 整表替换）。"""
    if store is not None:
        store.save_bookings(df)
        return
    _save_table(df, path, BOOKINGS_SPEC, BOOKINGS_XLSX)


# ===========================================================================
# 六、场次表操作
# ===========================================================================

def _coalesce_with_existing(
    incoming: pd.DataFrame,
    existing: pd.DataFrame,
    *,
    trust_status: bool = False,
) -> pd.DataFrame:
    """把旧表的值回填进 incoming，实现「空值不覆盖」。

    两类字段，两种处理：
      booked_seats / status —— 无条件保留旧值（两条硬不变量）
      其余字段             —— incoming 非空才覆盖，空则回落到旧值

    为什么不能只保护这两个字段：_coerce_types 会给调用方没提供的列填 NA。
    一份只写了「年轮 20号19:30 还差2人」的复转文案，旧实现会把旧表里的
    8 人本、168 元、情感本全部抹空 —— 转发得越多，数据丢得越干净。

    status / booked_seats 若被覆盖，会出两种事故：
      1. 人数被抹成空 —— 重新抽一遍文案，真实人数就没了；
      2. 已取消的场次被"复活" —— 店长手工取消了一场，
         之后那份文案又被转发一次，场次回到「待开场」，
         接着进了营销文案，推给已经被告知取消的玩家。

    trust_status=True 是店长显式改状态的唯一入口（update_session 汇聚点）：
    这时 incoming 的 status 按普通字段规则合并——非空才覆盖、空回落旧值。
    文案抽取等路径必须保持默认 False，防复活不变量不能松。
    """
    old = existing.drop_duplicates(subset="session_id", keep="last").set_index("session_id")
    sid = incoming["session_id"].astype("string").fillna("")
    out = incoming.copy()

    for col in SESSIONS_COLUMNS:
        # session_id 是匹配键本身；remaining_seats 最后会重算；
        # updated_at 已在本轮统一打过时间戳，不需要回填。
        if col in ("session_id", "remaining_seats", "updated_at"):
            continue

        old_values = sid.map(old[col]).astype("object")
        new_values = out[col].astype("object")

        if col in ("booked_seats", "status") and not (trust_status and col == "status"):
            keep_old = ~old_values.map(is_missing_value)
            out[col] = new_values.where(~keep_old, old_values)
        else:
            keep_new = ~new_values.map(is_missing_value)
            out[col] = new_values.where(keep_new, old_values)

    return _coerce_types(out)


def _upsert_sessions_impl(
    rows: pd.DataFrame | list[dict],
    path: Path | None = None,
    *,
    store: Any = None,
    trust_status: bool = False,
) -> pd.DataFrame:
    """upsert_sessions 的实现；调用方必须先持有写锁。

    「upsert」= update + insert。
    为什么不让调用方自己决定新增还是更新：
      工具 1 从门店群里抽文案时，根本不知道这个场次之前有没有出现过 ——
      同一场可能被反复转发，抽出多条。调用方逐个查表再分支，既啰嗦又容易漏。
      合并逻辑收在这里一处，调用方只管把抽到的东西丢进来。

    四条关键规则：
      1. session_id 为空时自动按「门店+剧本+时间」补算，不覆盖已有值；
      2. **合并以字段为单位，最后一个非空值胜出** —— 同一批里后一条残缺的
         转发不会再把前一条的完整信息整行丢掉（旧实现是 keep="last"，会丢）；
      3. **已存在场次的 booked_seats 一律保留**，不接受新行传入的数字 ——
         它是从预约明细算出来的，场次批次写入时（比如重新抽取一遍文案）
         手上根本没有这个数，传个空值覆盖进去就把真实人数抹掉了；
      4. **已存在场次的 status 默认同样保留** —— 它记录的是店长的手工决定
         （取消一场），文案里读不出来。详见 _coalesce_with_existing 的注释。
         trust_status=True 是店长显式改状态的专用入口（update_session），
         此时 incoming 的 status 非空即可覆盖。

    ⚠️ 与工具 2 的分工：合并策略与 dedupe.py 共用同一份实现（本模块第四节）。
    dedupe 对「算不出主键」的行放行并标注，交给人 / LLM 处理；
    本函数是落盘前的最后一道闸，**直接抛 ValueError** —— 主键为空的行
    不能进 Excel，否则身份永久丢失，之后再也去不了重。
    """
    raw = pd.DataFrame(rows)
    # 身份必须在 _coerce_types 之前算：整形会把「无法解析」的时间静默变成 NaT。
    identities = [session_identity(row) for _, row in raw.iterrows()]
    incoming = _coerce_types(raw)

    if incoming.empty:
        return load_sessions(path, store=store)

    # --- ① 补主键（只填空、不覆盖）+ ② 同一批内按字段合并 ---
    incoming = assign_session_keys(incoming, identities, strict=True)
    incoming = merge_session_batch(incoming, key="_group_key")

    # --- ③ 打时间戳 ---
    incoming["updated_at"] = _now()

    existing = load_sessions(path, store=store)

    # --- ④ 与旧表合并：空值不覆盖，两条硬不变量无条件保留 ---
    if not existing.empty:
        incoming = _coalesce_with_existing(incoming, existing, trust_status=trust_status)

    # --- ⑤ 合并：没被这次写入提到的旧场次原样保留 ---
    untouched = existing[~existing["session_id"].isin(incoming["session_id"])]

    # 分开处理"没有旧场次"这种情况：拿一张空表去 concat，
    # 会触发 pandas 的 FutureWarning（空表参与拼接时结果类型的行为要变），
    # 而且本来就没什么可拼的，直接返回新数据更清楚。
    if untouched.empty:
        merged = incoming[SESSIONS_COLUMNS].copy()
    else:
        merged = pd.concat([untouched, incoming[SESSIONS_COLUMNS]], ignore_index=True)

    # 全新场次的默认值：
    #   人数 0 而不是空 —— 空值会让后面的 total - booked 一起变成空；
    #   状态「待开场」而不是空 —— 新场次按定义就是还没开场、也没被取消，
    #   留空的话 Excel 里显示一个空格子，下游还得各自判断空值算什么。
    merged["booked_seats"] = merged["booked_seats"].fillna(0).astype("Int64")
    merged["status"] = merged["status"].fillna(STATUS_PENDING)

    # 按开场时间排序：Excel 打开就是时间顺序，便于店长肉眼核对；
    # 同时让「同样的输入产出同样的文件」，测试才能稳定断言。
    merged = _coerce_types(merged).sort_values(["start_time", "session_id"], ignore_index=True)

    save_sessions(merged, path, store=store)
    return merged


def upsert_sessions(
    rows: pd.DataFrame | list[dict],
    path: Path | None = None,
    *,
    store: Any = None,
) -> pd.DataFrame:
    """把一批场次按 session_id 合并进表里（有则更新，无则新增）。

    传入 store 时走数据库；不传时保持原 Excel 语义。
    """
    with store_lock(store):
        return _upsert_sessions_impl(rows, path, store=store)


def _keep(series: pd.Series, wanted: Any) -> pd.Series:
    """构造一个筛选掩码。

    wanted 支持三种形态：
      None        -> 不过滤（全 True）
      单个字符串   -> 精确匹配
      一组值       -> 属于其中之一（工具 3 要「状态不是已取消的」这种多选）
    """
    if wanted is None:
        return pd.Series(True, index=series.index)
    if isinstance(wanted, str):
        return series == wanted
    return series.isin(list(wanted))


def query_sessions(
    *,
    store_name: Any = None,
    script_name: Any = None,
    script_type: Any = None,
    status: Any = None,
    start_from: Any = None,
    start_to: Any = None,
    with_counts: bool = True,
    path: Path | None = None,
    bookings_path: Path | None = None,
    store: Any = None,
) -> pd.DataFrame:
    """按条件查场次。所有参数都可选，不传就是不按这个条件过滤。

    with_counts 默认 True：
      凡是「给人看的」查询都应该带上真实人数，而真实人数只能从预约明细算。
      默认 False 的话，每个调用方都得记得手动接一次明细表 —— 忘一次就报错人数。
    """
    df = load_sessions(path, store=store)

    if with_counts:
        df = apply_booking_counts(df, load_bookings(bookings_path, store=store))

    df = df[_keep(df["store_name"], store_name)]
    df = df[_keep(df["script_name"], script_name)]
    df = df[_keep(df["script_type"], script_type)]
    df = df[_keep(df["status"], status)]

    # 时间区间用的是 >= / <= 闭区间：店长说"20 号到 22 号的场"，
    # 心里想的是两头都算，不是数学上的半开区间。
    if start_from is not None:
        df = df[df["start_time"] >= pd.to_datetime(start_from)]
    if start_to is not None:
        df = df[df["start_time"] <= pd.to_datetime(start_to)]

    return df.reset_index(drop=True)


def get_session(
    session_id: str,
    *,
    with_counts: bool = True,
    path: Path | None = None,
    bookings_path: Path | None = None,
    store: Any = None,
) -> dict | None:
    """按 session_id 取单个场次，返回 dict；找不到返回 None。

    返回 None 而不是抛异常：查不到是正常结果（比如明细里的场次已被删），
    调用方用 `if row is None` 判断即可。
    """
    df = query_sessions(
        with_counts=with_counts,
        path=path,
        bookings_path=bookings_path,
        store=store,
    )
    hit = df[df["session_id"] == str(session_id).strip()]
    if hit.empty:
        return None
    return hit.iloc[0].to_dict()


# ===========================================================================
# 六、预约明细表操作
# ===========================================================================

def count_booked(session_id: str, bookings: pd.DataFrame) -> int:
    """统计某个场次的**有效**预约人数（已取消的不算）。

    纯函数形式（接 DataFrame 而不是路径），方便单测直接构造数据断言。
    """
    mask = (bookings["session_id"] == session_id) & (bookings["status"] == BOOKING_ACTIVE)
    return int(bookings.loc[mask, "seats"].fillna(0).sum())


def list_bookings(
    session_id: str,
    *,
    include_cancelled: bool = False,
    bookings: pd.DataFrame | None = None,
    path: Path | None = None,
    store: Any = None,
) -> pd.DataFrame:
    """列出某个场次的预约人 —— 回答「这场的位子都是谁占的」。

    默认只看有效的：店长问"都有谁"时想知道的是当前在座的人，
    跳车的不该混在里面。
    """
    df = (
        load_bookings(path, store=store)
        if bookings is None
        else _coerce_table(bookings, BOOKINGS_SPEC)
    )
    df = df[df["session_id"] == session_id]
    if not include_cancelled:
        df = df[df["status"] == BOOKING_ACTIVE]
    return df.reset_index(drop=True)


def _upsert_booking_impl(
    session_id: str,
    player_name: str,
    *,
    seats: int = 1,
    source_text: str | None = None,
    path: Path | None = None,
    now: pd.Timestamp | None = None,
    store: Any = None,
) -> pd.DataFrame:
    """upsert_booking 的实现；调用方必须先持有写锁。

    幂等是怎么来的：booking_id = hash(场次ID + 昵称)，
      同一个人重复预约 => 同一个 id => 落到同一行 => 人数不变。
      这就是「同一条预约消息被转发两次」不会多算一个人的原因。

    三种情况都走这一个入口：
      - 新预约        -> 新增一行
      - 重复预约      -> 更新（人数、来源文案刷新）
      - 跳车后又约回来 -> 把「已取消」改回「有效」（玩家反悔是真实场景）

    不校验 session_id 是否存在于场次表：
      预约可能先于场次记录到达（群里先有人喊"约"，门店后发排期）。
      宁可不拦，也不要把真实预约挡在门外；对不上的由 find_orphan_bookings() 报出来。
    """
    session_id = str(session_id).strip()
    player_name = str(player_name).strip()

    if not session_id or not player_name:
        raise ValueError("session_id 与 player_name 都不能为空")
    if int(seats) < 1:
        raise ValueError(f"占位人数至少为 1，收到 {seats}")

    ts = now if now is not None else _now()
    booking_id = make_booking_id(session_id, player_name)
    df = load_bookings(path, store=store)

    hit = df["booking_id"] == booking_id
    if hit.any():
        idx = df.index[hit][0]
        df.loc[idx, "seats"] = int(seats)
        df.loc[idx, "status"] = BOOKING_ACTIVE      # 反悔又约回来
        df.loc[idx, "updated_at"] = ts
        if source_text is not None:
            df.loc[idx, "source_text"] = source_text
        # 注意 booked_at 不动：那是"第一次约"的时间，改约不该把它冲掉
    else:
        fresh = pd.DataFrame([{
            "booking_id": booking_id,
            "session_id": session_id,
            "player_name": player_name,
            "seats": int(seats),
            "status": BOOKING_ACTIVE,
            "booked_at": ts,
            "updated_at": ts,
            "source_text": source_text,
        }])
        fresh = _coerce_table(fresh, BOOKINGS_SPEC)
        # 空表不参与 concat，理由同 upsert_sessions
        df = fresh if df.empty else pd.concat([df, fresh], ignore_index=True)

    df = _coerce_table(df, BOOKINGS_SPEC)
    save_bookings(df, path, store=store)
    return df


def upsert_booking(
    session_id: str,
    player_name: str,
    *,
    seats: int = 1,
    source_text: str | None = None,
    path: Path | None = None,
    now: pd.Timestamp | None = None,
    store: Any = None,
) -> pd.DataFrame:
    """记录一条预约（幂等 —— 同一玩家同一场次反复调用只算一个人）。"""
    with store_lock(store):
        return _upsert_booking_impl(
            session_id,
            player_name,
            seats=seats,
            source_text=source_text,
            path=path,
            now=now,
            store=store,
        )


@dataclass(frozen=True)
class BookingCheck:
    ok: bool
    booking_id: str | None
    error_code: str | None
    error: str | None
    session: dict | None
    warnings: tuple[str, ...]


def _record_booking_checked_impl(
    session_id: str,
    player_name: str,
    *,
    seats: int = 1,
    source_text: str | None = None,
    path: Path | None = None,
    now: pd.Timestamp | None = None,
    store: Any = None,
) -> BookingCheck:
    """带容量守卫的预约写入；调用方必须已持有写锁。

    低层 upsert_booking 故意不检查容量，供迁移/特殊修复使用。这里多一层：
      1. 场次必须存在；
      2. 总人数已知时，净增座位不能超过剩余容量；
      3. 同一玩家改人数按差额计算，重复提交同一请求不会误判超卖。
    """
    session_id = str(session_id).strip()
    player_name = str(player_name).strip()
    if not session_id or not player_name:
        return BookingCheck(
            ok=False,
            booking_id=None,
            error_code="bad_request",
            error="session_id 与 player_name 都不能为空。",
            session=None,
            warnings=(),
        )
    try:
        seat_count = int(seats)
    except (TypeError, ValueError):
        seat_count = 0
    if seat_count < 1:
        return BookingCheck(
            ok=False,
            booking_id=None,
            error_code="bad_request",
            error="占位人数至少为 1。",
            session=None,
            warnings=(),
        )

    # 这里的 path 与 upsert_booking 一致，指预约表；场次表取同目录的 sessions.xlsx。
    sessions_path = (
        Path(path).with_name("sessions.xlsx") if path is not None else None
    )
    sessions = load_sessions_with_counts(sessions_path, path, store=store)
    hit = sessions[
        sessions["session_id"].astype("string").fillna("") == session_id
    ]
    if hit.empty:
        return BookingCheck(
            ok=False,
            booking_id=None,
            error_code="session_not_found",
            error="场次不存在，请先建场次再记预约。",
            session=None,
            warnings=(),
        )
    session = hit.iloc[0].to_dict()
    status = str(session.get("status") or "").strip()
    if status == STATUS_CANCELLED:
        return BookingCheck(
            ok=False,
            booking_id=None,
            error_code="session_cancelled",
            error="场次已取消，不能继续加预约。",
            session=session,
            warnings=(),
        )

    bookings = load_bookings(path=path, store=store)
    booking_id = make_booking_id(session_id, player_name)
    existing = bookings[
        (bookings["booking_id"].astype("string").fillna("") == booking_id)
        & (bookings["status"] == BOOKING_ACTIVE)
    ]
    previous_seats = int(existing.iloc[0]["seats"]) if not existing.empty else 0
    delta = max(0, seat_count - previous_seats)

    booked = int(session.get("booked_seats") or 0)
    total = session.get("total_seats")
    warnings: list[str] = []
    if is_missing_value(total):
        warnings.append("场次总人数未填写，已写入但无法校验容量。")
    else:
        total_seats = int(total)
        if booked + delta > total_seats:
            return BookingCheck(
                ok=False,
                booking_id=None,
                error_code="capacity_exceeded",
                error=(
                    f"容量不足：已约 {booked}/{total_seats}，"
                    f"本次净增 {delta} 人。建议候补或改场。"
                ),
                session=session,
                warnings=tuple(warnings),
            )

    _upsert_booking_impl(
        session_id,
        player_name,
        seats=seat_count,
        source_text=source_text,
        path=path,
        now=now,
        store=store,
    )
    return BookingCheck(
        ok=True,
        booking_id=booking_id,
        error_code=None,
        error=None,
        session=session,
        warnings=tuple(warnings),
    )


def record_booking_checked(
    session_id: str,
    player_name: str,
    *,
    seats: int = 1,
    source_text: str | None = None,
    path: Path | None = None,
    now: pd.Timestamp | None = None,
    store: Any = None,
) -> BookingCheck:
    """Web/Agent 的主入口：容量检查与写入在同一个 store.write_lock() 内。"""
    with store_lock(store):
        return _record_booking_checked_impl(
            session_id,
            player_name,
            seats=seats,
            source_text=source_text,
            path=path,
            now=now,
            store=store,
        )


def _cancel_booking_impl(
    session_id: str,
    player_name: str,
    *,
    source_text: str | None = None,
    path: Path | None = None,
    now: pd.Timestamp | None = None,
    store: Any = None,
) -> bool:
    """cancel_booking 的实现；调用方必须先持有写锁。

    返回 False 的两种情况：这条预约不存在，或它本来就是已取消。
    为什么不静默返回成功：如果工具 2/工具 4 依赖这个结果去更新状态，
      一个"假装成功"会掩盖掉上游传错参数的问题，等到发现时已经错了很远。

    不删行：跳车记录本身是有价值的信息（谁老跳车、某场为什么垮了）。
    """
    session_id = str(session_id).strip()
    player_name = str(player_name).strip()
    booking_id = make_booking_id(session_id, player_name)
    df = load_bookings(path, store=store)

    hit = df["booking_id"] == booking_id
    if not hit.any():
        return False

    idx = df.index[hit][0]
    if df.loc[idx, "status"] == BOOKING_CANCELLED:
        return False  # 已经是取消状态，无事发生

    df.loc[idx, "status"] = BOOKING_CANCELLED
    df.loc[idx, "updated_at"] = now if now is not None else _now()
    if source_text is not None:
        df.loc[idx, "source_text"] = source_text

    save_bookings(_coerce_table(df, BOOKINGS_SPEC), path, store=store)
    return True


def cancel_booking(
    session_id: str,
    player_name: str,
    *,
    source_text: str | None = None,
    path: Path | None = None,
    now: pd.Timestamp | None = None,
    store: Any = None,
) -> bool:
    """把一条预约标记为已取消（软删除），返回是否真的改动了。"""
    with store_lock(store):
        return _cancel_booking_impl(
            session_id,
            player_name,
            source_text=source_text,
            path=path,
            now=now,
            store=store,
        )


# ===========================================================================
# 七、把两张表接起来
# ===========================================================================

def apply_booking_counts(sessions: pd.DataFrame, bookings: pd.DataFrame) -> pd.DataFrame:
    """用预约明细重算 booked_seats，再据此重算 remaining_seats。

    这一步是全文件的核心：**文件里存的 booked_seats 一律不作为依据**。
    店长手工在 Excel 里把 3 改成 8，下次读出来还是真实值 3。

    顺序不能反：remaining 依赖 booked，先算 booked 再算 remaining。
    这里靠"先赋值 booked_seats、再调 _coerce_types 触发 derive"保证顺序。
    """
    out = sessions.copy()

    active = bookings[bookings["status"] == BOOKING_ACTIVE]
    # to_dict() 而不是直接 map 一个 Series：绕开 pandas 的索引类型匹配问题，
    # 字典查表的行为最直观也最不容易出意外。
    counts = active.groupby("session_id")["seats"].sum().to_dict()

    # 没有任何预约的场次算 0 人，不是"未知"
    out["booked_seats"] = out["session_id"].map(counts).fillna(0).astype("Int64")

    # 这一步顺带把 remaining_seats 重算掉
    return _coerce_types(out)


def load_sessions_with_counts(
    sessions_path: Path | None = None,
    bookings_path: Path | None = None,
    *,
    store: Any = None,
) -> pd.DataFrame:
    """读两张表并合成 —— 下游工具（3 过期过滤 / 2 去重 / 4 高危识别 / 5 定价）
    应该优先用这个入口，而不是裸的 load_sessions()。

    裸的 load_sessions() 拿到的人数可能是过期的；这个函数拿到的永远是
    按预约明细重算过的当前值。
    """
    return apply_booking_counts(
        load_sessions(sessions_path, store=store),
        load_bookings(bookings_path, store=store),
    )


def find_orphan_bookings(
    *,
    sessions: pd.DataFrame | None = None,
    bookings: pd.DataFrame | None = None,
    sessions_path: Path | None = None,
    bookings_path: Path | None = None,
    store: Any = None,
) -> pd.DataFrame:
    """找出「指向不存在场次」的预约明细。

    为什么会有孤儿：场次被删除、或 session_id 拼错。
    为什么不当场报错：计数时直接忽略它们就够了（不能让一条脏数据把整张表读崩），
      但也不能当没看见 —— 有孤儿说明数据有问题，要能被查出来给人看。
    """
    if sessions is None:
        sessions = load_sessions(sessions_path, store=store)
    if bookings is None:
        bookings = load_bookings(bookings_path, store=store)

    known = set(sessions["session_id"].dropna())
    return bookings[~bookings["session_id"].isin(known)].reset_index(drop=True)
