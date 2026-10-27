"""工具 3：过期过滤（剔除已经开场、失效的场次）。

========================= 三条设计原则 =========================

一、只读不写
   本模块不修改任何数据，只回答「哪些场次现在还有意义」。
   已开场的场次**不需要**有人去把 status 改成「已开场」——
   那是由 start_time 推导出来的事实，不是需要谁去维护的状态。

二、时间说了算，不信任 status 字段
   文件里写着「待开场」，但 start_time 已经过去 —— 仍然是过期。
   这与 storage.py 里 booked_seats 的处理是同一个原则：
   **存储的值会过期，重算的值才是真的。**
   推论：status 字段里只有「已取消」必须持久化（那是人的决定，推不出来），
   「已开场」任何时刻都能算出来，不必写回文件。

三、now 一律可注入
   不可注入的话，测试就只能依赖系统时钟：写不出「开场时间恰好等于现在」
   这种边界用例，而且测试会随运行时刻不同而时红时绿。

========================= 与工具 6 的分工 =========================

  storage.py  负责「数据从哪来、长什么样」—— 读写、类型、派生计数
  本模块       负责「在时间维度上，哪些还有意义」

本模块**只放纯函数，不出现 @tool 装饰器**（理由见 storage.py 顶部说明）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.tools.storage import STATUS_CANCELLED, STATUS_PENDING, STATUS_STARTED


# ===========================================================================
# 一、时间基准
# ===========================================================================

def resolve_now(now: Any = None) -> pd.Timestamp:
    """把 now 参数归一成一个 Timestamp；不传则取当前时间。

    截断到秒，与 storage._now() 保持一致 —— 两边精度不同的话，
    「刚写入的场次」和「过期判定」会差那么几微秒，边界上很难解释。

    公开（无下划线）：工具 4「高危识别」也要按同一套规则取"现在"。
    两处各写一份的话，同一批数据在两个工具里可能算出不同的时间基准。
    """
    return pd.Timestamp.now().floor("s") if now is None else pd.Timestamp(now)


def to_timestamp(value: Any) -> pd.Timestamp | None:
    """把任意写法的时间转成 Timestamp；转不了返回 None。

    返回 None 而不是抛异常：读表阶段宽容（不炸），
    把"这条时间看不懂"作为数据问题留给调用方，而不是当成崩溃。

    公开（无下划线）：工具 4「高危识别」要算剩余小时数，同样需要这层容错。
    """
    if value is None or pd.isna(value):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts


# ===========================================================================
# 二、单值判定
# ===========================================================================

def is_expired(start_time: Any, *, now: Any = None) -> bool:
    """判断一个开场时间是否已过期。

    两条边界规则：

      start_time == now  ->  **算过期**（用 <= 而不是 <）
        已经开场就不能再卖票了，差一秒也不行。

      start_time 为空    ->  **不算过期**（返回 False）
        "无法判定"不等于"已失效"。把它当过期剔掉，
        等于静默吞掉一条真实场次 —— 宁可留着让人看见，也不要悄悄丢掉。
    """
    ts = to_timestamp(start_time)
    if ts is None:
        return False
    return bool(ts <= resolve_now(now))


# ===========================================================================
# 三、整表切分
# ===========================================================================

def split_expired(
    sessions: pd.DataFrame,
    *,
    now: Any = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把场次表按时间切成两张：(未过期, 已过期)。

    为什么两张都返回，而不是只给过滤后的一张：
      调用方常常要报一句「另有 N 场已过期」，只给过滤结果是说不出这句话的。
      过期场次本身也不该被静默丢弃 —— 它是一份「哪些场没发起来」的记录。

    注意这里**只按时间切**，不看 status 字段。已取消的场次由谁负责排除，
    是 filter_active() 的事（见下），两个规则不混在一个函数里。
    """
    moment = resolve_now(now)
    starts = pd.to_datetime(sessions["start_time"], errors="coerce")

    # 先 notna() 再比较，顺序不能反：
    # NaT 参与 <= 得到的是 False，看着"不会误判成过期"，
    # 但把 NA 显式排除掉，意图才写在代码表面，而不是靠读者知道 pandas 的默认行为。
    expired_mask = starts.notna() & (starts <= moment)

    return (
        sessions[~expired_mask].reset_index(drop=True),
        sessions[expired_mask].reset_index(drop=True),
    )


def filter_active(sessions: pd.DataFrame, *, now: Any = None) -> pd.DataFrame:
    """**下游工具的直接入口**：未过期 且 未取消的场次。

    工具 4（高危识别）、工具 5（定价）、工具 7（文案生成）都应该从这里拿数据，
    而不是自己写一遍过滤条件 —— 各写一遍，早晚会有一处忘记排除已取消的场次，
    然后给一个开不了的场子生成营销文案。

    前置条件：入参应来自 storage.load_sessions_with_counts()，
    保证有 status / start_time 两列且类型已整形。
    """
    usable, _ = split_expired(sessions, now=now)

    # fillna("") 是必需的：status 为空时，pd.NA != "已取消" 得到的是 pd.NA，
    # 拿这种"含 NA 的布尔数组"去索引会直接抛错。空状态视为"未取消"。
    return usable[usable["status"].fillna("") != STATUS_CANCELLED].reset_index(drop=True)


# ===========================================================================
# 四、状态推导
# ===========================================================================

def is_cancelled(stored_status: Any) -> bool:
    """status 字段是否表示"人为取消"。空值不算取消。"""
    if stored_status is None or pd.isna(stored_status):
        return False
    return str(stored_status).strip() == STATUS_CANCELLED


def derive_status(start_time: Any, stored_status: Any, *, now: Any = None) -> str:
    """推导场次的真实状态。

    优先级：
      1. 已取消    —— 人工意图优先。取消了就是取消了，跟时间无关；
                      而且取消的场次时间可能还没到，不能靠时间判断。
      2. 已开场    —— 时间已过。
      3. 待开场    —— 其余。

    为什么需要这个函数：
      status 字段里真正需要"存"的只有「已取消」这一种
      —— 它是人的决定，无论怎么算都算不出来。
      「已开场」纯粹是时间的函数，就不该有谁去把它写回文件：
      一旦写回，就多出一个会跟时间对不上的状态需要维护。
    """
    if is_cancelled(stored_status):
        return STATUS_CANCELLED
    if is_expired(start_time, now=now):
        return STATUS_STARTED
    return STATUS_PENDING


def with_derived_status(sessions: pd.DataFrame, *, now: Any = None) -> pd.DataFrame:
    """在表上增加一列 derived_status（推导结果），不改动原 status 列。

    为什么不直接覆盖 status：
      原 status 里的「已取消」是人工输入的事实，属于原始数据；
      推导结果是计算结果。两者混在一列里，就再也分不清
      "这场是被谁取消的"和"这场是因为到点才显示为已开场"。
    """
    out = sessions.copy()
    moment = resolve_now(now)
    starts = pd.to_datetime(out["start_time"], errors="coerce")

    expired = starts.notna() & (starts <= moment)
    cancelled = out["status"].apply(is_cancelled)

    # 顺序即优先级：先按时间定，再用「已取消」覆盖
    derived = pd.Series(STATUS_PENDING, index=out.index, dtype="object")
    derived[expired] = STATUS_STARTED
    derived[cancelled] = STATUS_CANCELLED

    out["derived_status"] = derived.astype("string")
    return out
