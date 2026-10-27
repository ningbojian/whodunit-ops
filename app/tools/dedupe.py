"""工具 2：场次去重（识别重复转发，合并成一场）。

========================= 一句话 =========================

同一场拼场信息会被门店反复转发：先是完整海报文案，几小时后变成「还差 2 人」，
改价了再发一条。本工具回答三个问题：

    这批记录里，哪几条其实是同一场？   -> 联合键 + 合并
    合并后应该长什么样？               -> 字段级「最后一个非空值胜出」
    跟现有表比，是新增还是更新？       -> dedupe_action / changed_fields

它**不写文件**：分析结果交给 storage.upsert_sessions 落盘。

========================= 「同一场次」的定义 =========================

    门店 + 剧本 + 开场时间（归一化到分钟）

三个维度归一后完全一致，就是同一场。开场时间不同就是两场（19:30 和 21:30
是两车），门店不同就是两家店。

这是**定义**，不是阈值 —— 本模块没有需要标定的经验参数，
这一点和工具 4（到客速率）、工具 5（成本与折扣档位）不同。

========================= 合并规则（本轮的核心） =========================

合并以**字段**为单位，不以**行**为单位：

    三条转发：  [完整: 8人本 168 情感本]  [残缺: 只有身份字段]  [残缺+改价: 158]
    结果：       8人本  158  情感本

**最后一个非空值胜出，空值永不覆盖。**

旧实现（storage 里的 drop_duplicates(keep="last")）是按整行去重，会出两个事故：
后一条残缺的转发把前一条完整的 total_seats / list_price 整行丢掉；
跨批次写入时，未提供的列被填成 NA，把旧表的价格、总位抹空。
转发得越多，数据丢得越干净 —— 这两个其实是同一个 bug 的两种表现。

冲突（两个**非空**值打架）时取后者，但**必须把字段名报出来**。
取后者是因为改房、改价都会重发，后来的消息更接近当前状态；
但工具不假装自己知道哪个对，所以冲突要对 LLM 可见。

冲突报告排除 source_text：同一场转发三次，原文必然三份不同，
算进去会让 conflict_fields 永远非空，报告就失去意义。

========================= 与工具 6 的分工 =========================

    工具 2（本模块）  分析层：分组、身份裁决、冲突报告、与现有表比对
    工具 6（storage） 落盘层：upsert_sessions 写 Excel

合并策略是**共用的一份实现**（storage 第四节），因为 upsert 落盘时必须用
同一套规则，否则项目里会有两份互相漂移的合并语义。策略文档写在本文档，
物理实现放在 storage —— 依赖方向只有 dedupe -> storage，不会循环导入。

错误策略刻意不同：

    本模块       算不出主键 -> 放行并标注 dedupe_note，让 LLM 追问店长
    upsert       算不出主键 -> 抛 ValueError，主键为空的行不能进 Excel

分析层要能容忍坏数据，落盘层要守住主键底线。

========================= 显式 session_id 的裁决 =========================

    显式 ID  联合键      行为
    空       可算        用联合键补，正常合并
    空       不可算      放行，标注原因，每行独立成组
    有       可算且一致  按显式 ID 合并，无备注
    有       可算但不一致 保留显式 ID，标注「session_id与联合键不一致」
    同批两行 ID 不同、联合键相同 -> 不合并，标注「联合键相同但session_id不同」

最后一条的理由：场次表**没有房间字段**，同一门店 + 同一剧本 + 同一时间
完全可能是两个房间同时开。显式 ID 是调用方唯一能表达「这是两场」的逃生舱。
工具不替它改判，但必须把联合键冲突报出来。

========================= 已知边界 =========================

一、区分不了「同店同剧本同时间的两间房」—— 没有房间字段，靠显式 ID。
二、名称只做 strip，不做全角/半角、别名、模糊匹配：
    「年轮」和「年轮（情感本）」是两场，「迷雾剧场」和「迷雾剧场旗舰店」是两店。
三、冲突取「最后一个非空值」是启发式，不保证是对的 —— 所以要报出来。
四、已有重复数据不会被清理：本模块是写前分析层，不是表清洗器。
五、status / booked_seats 的批内冲突会被报告，但落盘时 storage 仍保留旧值。

本模块**只放纯函数，不出现 @tool 装饰器**（理由见 storage.py 顶部说明）。
"""

from __future__ import annotations

import pandas as pd

from app.tools.storage import (
    NOTE_KEY_COLLISION,
    SESSIONS_COLUMNS,
    _coerce_types,
    assign_session_keys,
    is_missing_value,
    merge_session_batch,
    session_identity,
    values_equal,
)

# ===========================================================================
# 一、常量
# ===========================================================================

ACTION_NEW = "新增"
ACTION_UPDATED = "更新"
ACTION_UNCHANGED = "无变化"
ACTION_UNCOMPARED = "未比对"

# 与现有表比对时，哪些字段的变化会让动作变成「更新」。
# 排除：
#   session_id      主键，不覆盖
#   booked_seats    只能来自预约明细，storage 无条件保留旧值
#   status          必须保留店长的人工决定
#   remaining_seats 派生字段
#   updated_at      每次写入都变，算进去就永远是「更新」
COMPARE_FIELDS: list[str] = [
    "store_name", "script_name", "script_type", "start_time",
    "total_seats", "list_price", "source_text",
]

# 本模块新增的列。永不落盘 —— 喂给 upsert_sessions 时会被 _coerce_types 裁掉。
DEDUPE_COLUMNS: list[str] = [
    "dedupe_action",    # 新增 / 更新 / 无变化 / 未比对
    "merged_count",     # 这一行由几条原始记录合并而来
    "conflict_fields",  # 同批内值打架的字段名 tuple
    "changed_fields",   # 相对现有表会被改写的字段名 tuple
    "dedupe_note",      # 身份缺失 / 冲突的放行原因，正常为空字符串
]


# ===========================================================================
# 二、与现有表比对
# ===========================================================================

def compare_with_existing(
    records: pd.DataFrame,
    existing: pd.DataFrame | None,
) -> pd.DataFrame:
    """标注 dedupe_action 与 changed_fields。

    existing=None   -> 全部「未比对」（调用方没给现有表，谈不上新增还是更新）
    existing 空表   -> 有 session_id 的全部「新增」

    两者语义不同，不能混为一谈。

    changed_fields 只统计「incoming 非空且与旧值不同」的字段 ——
    空值不会覆盖旧值，所以不算变化。这也是为什么 status / booked_seats
    的差异永远不会出现在这里：storage 本来就会无条件保留它们。
    """
    out = records.copy()

    if existing is None:
        out["dedupe_action"] = ACTION_UNCOMPARED
        out["changed_fields"] = pd.Series([()] * len(out), index=out.index, dtype="object")
        return out

    old = existing.drop_duplicates(subset="session_id", keep="last").set_index("session_id")

    actions: list[str] = []
    changes: list[tuple[str, ...]] = []

    for _, row in out.iterrows():
        sid = row.get("session_id")

        if is_missing_value(sid):
            # 没有身份，无法与现有表匹配
            actions.append(ACTION_UNCOMPARED)
            changes.append(())
            continue

        if sid not in old.index:
            actions.append(ACTION_NEW)
            changes.append(())
            continue

        old_row = old.loc[sid]
        changed = tuple(
            field for field in COMPARE_FIELDS
            if not is_missing_value(row.get(field))
            and not values_equal(row.get(field), old_row.get(field), field)
        )
        actions.append(ACTION_UPDATED if changed else ACTION_UNCHANGED)
        changes.append(changed)

    out["dedupe_action"] = actions
    out["changed_fields"] = pd.Series(changes, index=out.index, dtype="object")
    return out


# ===========================================================================
# 三、内部辅助
# ===========================================================================

def _empty_result() -> pd.DataFrame:
    """空输入时的返回：列齐全的空表，而不是一个裸的 None。"""
    out = _coerce_types(pd.DataFrame(columns=SESSIONS_COLUMNS))
    out["dedupe_action"] = pd.Series(dtype="object")
    out["merged_count"] = pd.Series(dtype="Int64")
    out["conflict_fields"] = pd.Series(dtype="object")
    out["changed_fields"] = pd.Series(dtype="object")
    out["dedupe_note"] = pd.Series(dtype="string")
    return out[SESSIONS_COLUMNS + DEDUPE_COLUMNS]


def _mark_key_collisions(
    keyed: pd.DataFrame,
    identities: list[tuple[str | None, str]],
) -> pd.DataFrame:
    """标注「联合键相同、但调用方给了不同的显式 session_id」的行。

    这些行不会被合并 —— 显式 ID 不同意味着调用方认为它们是两场
    （比如同一时间的两个房间）。但联合键撞了，必须让人看见。
    """
    natural = pd.Series(
        [sid for sid, _ in identities], index=keyed.index, dtype="object"
    )
    explicit = keyed["session_id"].astype("string").fillna("")

    both = natural.notna() & (explicit != "")
    if not both.any():
        return keyed

    probe = keyed.loc[both, ["session_id"]].assign(_natural=natural[both])
    distinct = probe.groupby("_natural")["session_id"].nunique()
    collided = set(distinct[distinct > 1].index)
    if not collided:
        return keyed

    mask = both & natural.isin(collided)
    out = keyed.copy()
    out.loc[mask, "dedupe_note"] = NOTE_KEY_COLLISION
    return out


def _sort_result(records: pd.DataFrame) -> pd.DataFrame:
    """按开场时间排序；时间无法解析的行保持输入顺序排在最后。"""
    timed = records[records["start_time"].notna()].sort_values(
        ["start_time", "session_id"], ignore_index=True
    )
    untimed = records[records["start_time"].isna()].reset_index(drop=True)
    return pd.concat([timed, untimed], ignore_index=True)


# ===========================================================================
# 四、主入口
# ===========================================================================

def dedupe_sessions(
    records: pd.DataFrame | list[dict],
    *,
    existing: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """**工具 2 主入口**：批内合并 + 身份放行 + 与现有表比对。

    records   一批待处理的场次记录（通常是工具 1 从文案里抽出来的）
    existing  现有场次表；传 None 表示不比对（全部标「未比对」）

    纯函数，不读写文件。现有表由调用方用 storage.load_sessions() 读好后传入。

    返回 SESSIONS_COLUMNS + DEDUPE_COLUMNS，按开场时间、session_id 排序。
    把结果直接喂给 upsert_sessions 是安全的：多出来的分析列会被自动裁掉。
    """
    raw = pd.DataFrame(records)
    if raw.empty:
        return _empty_result()

    # 身份必须在整形**之前**算：_coerce_types 会把「下周三晚上」静默变成 NaT，
    # 之后就分不清「没填时间」和「填了一句解析不了的话」。
    identities = [session_identity(row) for _, row in raw.iterrows()]
    coerced = _coerce_types(raw)

    # 分配最终 session_id / 分组键 / 放行备注（strict=False：坏行放行不报错）
    keyed = assign_session_keys(coerced, identities, strict=False)
    keyed = _mark_key_collisions(keyed, identities)

    # 批内合并。dedupe_note 不是业务列，合并后被裁掉，下面按组键贴回。
    merged = merge_session_batch(keyed, key="_group_key")

    note_by_group = keyed.groupby("_group_key", sort=False)["dedupe_note"].first()
    merged["dedupe_note"] = (
        merged["_group_key"].map(note_by_group).astype("string").fillna("")
    )
    merged = merged.drop(columns="_group_key")

    merged = compare_with_existing(merged, existing)
    merged = merged[SESSIONS_COLUMNS + DEDUPE_COLUMNS]
    return _sort_result(merged)
