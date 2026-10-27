"""工具 3 演示：过期过滤。

演示四件事：
  1. 时间轴切分 —— 昨天 / 恰好现在 / 一小时后 / 明天，哪些被判为过期
  2. now 可注入 —— 同一个场次，随着"现在"推移，从有效变成过期
  3. 时间说了算 —— 文件里写着「待开场」但时间已过，照样算过期
  4. 两条规则分开 ——「已取消」不参与时间判定，但被 filter_active 排除

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/03_expiry_demo.py

本脚本**不写 data/ 目录**：演示数据建在临时目录里，结束自动清理。
只在最后一步读一下 data/sessions.xlsx（由 02_storage_demo.py 生成）做真实对照，
如果那个文件不存在就跳过。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 让脚本能 import 到项目根目录下的 app 包（scripts/ 与 app/ 平级）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import SESSIONS_XLSX  # noqa: E402
from app.tools.expiry import (  # noqa: E402
    filter_active,
    split_expired,
    with_derived_status,
)
from app.tools.storage import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_PENDING,
    load_sessions_with_counts,
    upsert_sessions,
)

# 演示用的"现在"：写死，这样每次运行结果完全一样。
NOW = pd.Timestamp("2026-10-20 19:30:00")

SHOW_COLS = ["script_name", "start_time", "status", "derived_status"]


def title(text: str) -> None:
    print()
    print("=" * 68)
    print(text)
    print("=" * 68)


def note(text: str) -> None:
    print(f"  >> {text}")


def show(df: pd.DataFrame, cols: list[str] = SHOW_COLS) -> None:
    if df.empty:
        print("  （空）")
        return
    print(df[cols].to_string(index=False))


def build_demo_sessions(path: Path) -> pd.DataFrame:
    """建一批时间横跨"昨天 → 明天"的场次放在临时目录里。"""
    upsert_sessions([
        {"store_name": "迷雾剧场", "script_name": "昨天的场", "script_type": "情感",
         "start_time": "2026-10-19 19:00", "total_seats": 8},
        {"store_name": "迷雾剧场", "script_name": "恰好现在的场", "script_type": "推理",
         "start_time": "2026-10-20 19:30", "total_seats": 6},
        {"store_name": "迷雾剧场", "script_name": "一小时后", "script_type": "恐怖",
         "start_time": "2026-10-20 20:30", "total_seats": 7},
        {"store_name": "迷雾剧场", "script_name": "明天的场", "script_type": "欢乐",
         "start_time": "2026-10-21 19:00", "total_seats": 8},
        {"store_name": "迷雾剧场", "script_name": "取消了的未来场", "script_type": "机制",
         "start_time": "2026-10-25 19:00", "total_seats": 6,
         "status": STATUS_CANCELLED},
        {"store_name": "迷雾剧场", "script_name": "忘了改状态的场", "script_type": "阵营",
         "start_time": "2026-10-18 19:00", "total_seats": 6,
         "status": STATUS_PENDING},   # 时间已过，但 status 还写着待开场
    ], path)
    return with_derived_status(load_sessions_with_counts(path), now=NOW)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sessions_path = tmp / "sessions.xlsx"

        raw = build_demo_sessions(sessions_path)

        # ------------------------------------------------------------------
        title(f"演示数据（推导状态一列是按 now = {NOW} 算出来的）")
        show(raw)
        note("注意「忘了改状态的场」：start_time 是 10-18，已经过去两天，")
        note("但文件里 status 那列还写着「待开场」—— 这正是现实中会发生的情况。")

        # ------------------------------------------------------------------
        title(f"第 1 步：按 now = {NOW} 切分")
        有效, 过期 = split_expired(raw, now=NOW)
        print("【未过期】")
        show(有效)
        print("\n【已过期】")
        show(过期)

        # ------------------------------------------------------------------
        title("第 2 步：三条边界规则")
        note("① 「恰好现在的场」start_time == now -> 算过期")
        note("   用 <= 而不是 <：已经开场就不能再卖票了，差一秒也不行。")
        print(f"      在未过期里吗？{'恰好现在的场' in set(有效['script_name'])}")
        print(f"      在已过期里吗？{'恰好现在的场' in set(过期['script_name'])}")

        note("② 「忘了改状态的场」status 写着待开场，但时间已过 -> 仍然算过期")
        print(f"      在已过期里吗？{'忘了改状态的场' in set(过期['script_name'])}")
        note("   时间说了算，不信任 status 字段 —— 和 storage 里 booked_seats 一个道理。")

        note("③ 「取消了的未来场」时间还没到 -> 不在已过期里")
        print(f"      在已过期里吗？{'取消了的未来场' in set(过期['script_name'])}")
        note("   过期是时间规则，取消是人工规则，两条规则分开算，不混在一个函数里。")

        # ------------------------------------------------------------------
        title("第 3 步：filter_active —— 下游工具真正该用的入口")
        print("  未过期 且 未取消的场次：")
        show(filter_active(raw, now=NOW))
        note("「取消了的未来场」虽然没过期，但不该进运营分析，所以被排除了。")
        note("工具 4（高危识别）、工具 5（定价）、工具 7（文案）都应该从这里拿数据。")

        # ------------------------------------------------------------------
        title("第 4 步：now 是可注入的 —— 同一批数据，换个'现在'结果就变")

        for moment, label in [
            ("2026-10-20 19:00", "开场前 30 分钟"),
            ("2026-10-20 19:30", "正点开场"),
            ("2026-10-20 20:30", "一小时后那场也开了"),
            ("2026-10-22 00:00", "到了后天"),
        ]:
            moment_ts = pd.Timestamp(moment)
            ok, bad = split_expired(raw, now=moment_ts)
            names = "、".join(ok["script_name"]) or "（无）"
            print(f"  now = {moment}  （{label}）")
            print(f"    未过期 {len(ok)} 场，已过期 {len(bad)} 场   剩：{names}")

        note("如果 now 不可注入，这个演示就写不出来 —— 测试里也一样，")
        note("写不出「开场时间恰好等于现在」这种用例，还会随运行时刻时红时绿。")

    # ----------------------------------------------------------------------
    # 对照：真实 data/ 目录下的场次表
    # ----------------------------------------------------------------------
    title("对照：data/sessions.xlsx 用真实的「现在」切分")
    if not SESSIONS_XLSX.exists():
        print("  文件不存在，跳过。先运行 scripts/02_storage_demo.py 生成它。")
    else:
        real = with_derived_status(load_sessions_with_counts())
        print(f"  真实现在时间：{pd.Timestamp.now().floor('s')}\n")
        show(real)
        ok, bad = split_expired(real)
        print(f"\n  未过期 {len(ok)} 场，已过期 {len(bad)} 场")
        note("02_storage_demo.py 造的场次都在 10-20 之后，所以现在全都还没过期。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
