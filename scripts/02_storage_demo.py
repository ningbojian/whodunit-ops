"""工具 6 演示：把「双表模型」完整跑一遍，生成真实的 Excel 文件。

这个脚本不是测试（测试在 tests/test_storage.py），它的作用是让你**亲眼看到**
两个关键机制确实按设计工作，并且真的在磁盘上落下两个 Excel 文件：

  机制一「幂等」：同一条预约消息被转发两次，人数不会从 1 变成 2
  机制二「软删除」：跳车不删行，只改状态，事后查得出谁跳过车

运行：
  PYTHONUTF8=1 "C:/Users/Lenovo/anaconda3/python.exe" scripts/02_storage_demo.py

⚠️ 它会**清空并重写** data/ 下的 sessions.xlsx 与 bookings.xlsx。
   这两个文件是演示数据，随时可以重新生成，不需要备份。

跑完请用 Excel 打开 data/sessions.xlsx 与 data/bookings.xlsx 看一眼 ——
pandas 写出来的表在 Excel 里显示是否正常（中文、时间、数字），
是单测查不出来、必须肉眼确认的事。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
# 显式切成 UTF-8，保证终端里能正常看到中文输出与报错信息。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 让脚本能 import 到项目根目录下的 app 包（scripts/ 与 app/ 平级）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.config import BOOKINGS_XLSX, SESSIONS_XLSX  # noqa: E402
from app.tools.storage import (  # noqa: E402
    STATUS_PENDING,
    cancel_booking,
    list_bookings,
    load_bookings,
    load_sessions,
    load_sessions_with_counts,
    save_sessions,
    upsert_booking,
    upsert_sessions,
)

# 打印用的列选择（人数是重点，所以排在前面）
SESSION_COLS = [
    "script_name", "script_type", "start_time",
    "total_seats", "booked_seats", "remaining_seats", "status",
]
BOOKING_COLS = ["player_name", "seats", "status", "source_text"]


def title(text: str) -> None:
    print()
    print("=" * 68)
    print(text)
    print("=" * 68)


def note(text: str) -> None:
    """打印一句解释，和普通输出区分开。"""
    print(f"  >> {text}")


def show(df: pd.DataFrame, cols: list[str], empty_hint: str = "（空）") -> None:
    if df.empty:
        print(f"  {empty_hint}")
        return
    print(df[cols].to_string(index=False))


def read_nianlun() -> pd.Series:
    """每次都重新从磁盘读「年轮」那一行，而不是复用内存里的旧对象。

    这一条很重要：如果读的是内存里那份，就永远验证不了
    「派生字段是读的时候重算的」这件事。
    """
    df = load_sessions_with_counts()
    return df[df["script_name"] == "年轮"].iloc[0]


def main() -> int:
    # ------------------------------------------------------------------
    # 第 0 步：清空演示数据，保证这个脚本可以反复运行、结果完全一样
    # ------------------------------------------------------------------
    title("第 0 步：清空旧的演示数据")
    for path in (SESSIONS_XLSX, BOOKINGS_XLSX):
        if path.exists():
            path.unlink()
            print(f"  已删除 {path.name}")
        else:
            print(f"  {path.name} 本来就不存在，跳过")

    # ------------------------------------------------------------------
    # 第 1 步：门店发了三条拼场文案
    # ------------------------------------------------------------------
    title("第 1 步：写入场次（session_id 由「门店+剧本+时间」自动算出）")
    upsert_sessions([
        {
            "store_name": "迷雾剧场", "script_name": "年轮", "script_type": "情感",
            "start_time": "2026-10-20 19:30", "total_seats": 8, "list_price": 168,
            "status": STATUS_PENDING,
            "source_text": "【年轮】20号19:30 情感本 8人 还差5位",
        },
        {
            "store_name": "迷雾剧场", "script_name": "蛊魂铃", "script_type": "恐怖",
            "start_time": "2026-10-21 20:00", "total_seats": 6, "list_price": 198,
            "status": STATUS_PENDING,
            "source_text": "【蛊魂铃】21号20:00 恐怖本 6人 差3",
        },
        {
            "store_name": "迷雾剧场", "script_name": "漓川怪谈簿", "script_type": "推理",
            "start_time": "2026-10-22 19:00", "total_seats": 7, "list_price": 188,
            "status": STATUS_PENDING,
            "source_text": "【漓川怪谈簿】22号19:00 推理本 7人 差4",
        },
    ])

    sessions = load_sessions_with_counts()
    show(sessions, SESSION_COLS)
    note("booked_seats 全是 0 —— 目前还没有任何人预约。")

    # 后面所有预约都挂到「年轮」这一场上
    sid = sessions[sessions["script_name"] == "年轮"].iloc[0]["session_id"]
    print(f"\n  「年轮」的 session_id = {sid}")

    # ------------------------------------------------------------------
    # 第 2 步：机制一 —— 幂等，防重复转发
    # ------------------------------------------------------------------
    title("第 2 步：机制一「幂等」—— 同一条预约消息被转发两次")

    upsert_booking(sid, "张三", source_text="张三 约一个")
    print(f"  张三预约           ->  已约 {read_nianlun()['booked_seats']} 人")

    upsert_booking(sid, "李四", source_text="李四 约一个")
    print(f"  李四预约           ->  已约 {read_nianlun()['booked_seats']} 人")

    upsert_booking(sid, "张三", source_text="张三 约一个")
    print(f"  「张三」那条被转发了一遍，代码又收到一次同样的预约")
    row = read_nianlun()
    print(f"                     ->  已约 {row['booked_seats']} 人   ← 仍是 2，没有变成 3")

    assert row["booked_seats"] == 2, "幂等性被破坏了"
    note("同一玩家 + 同一场次 => booking_id 相同 => 落到同一行，人数不变。")

    # ------------------------------------------------------------------
    # 第 3 步：机制二 —— 软删除，跳车留痕
    # ------------------------------------------------------------------
    title("第 3 步：机制二「软删除」—— 张三跳车")

    cancel_booking(sid, "张三", source_text="张三 跳车了")
    row = read_nianlun()
    print(f"  取消后：已约 {row['booked_seats']} 人，剩 {row['remaining_seats']} 位")

    print("\n  预约明细表（注意张三那行**还在**，只是状态变了）：")
    show(load_bookings(), BOOKING_COLS)
    note("跳车不删行：删了就查不出「这人原本约了、后来跳的」，出纠纷时没有痕迹。")

    # ------------------------------------------------------------------
    # 第 4 步：回答「这场的位子都是谁占的」
    # ------------------------------------------------------------------
    title("第 4 步：谁约的？")
    print("  当前在座：")
    show(list_bookings(sid), ["player_name", "seats"])
    print("\n  连跳车的一起看：")
    show(list_bookings(sid, include_cancelled=True), ["player_name", "seats", "status"])

    # ------------------------------------------------------------------
    # 第 5 步：派生字段是「读的时候重算的」
    # ------------------------------------------------------------------
    title("第 5 步：派生字段每次读取都重算 —— 文件里的数字不作数")
    note("booked_seats 和 remaining_seats 都不是真相来源，真相来源是预约明细表。")
    note("下面手工往文件里写一个错值，来证明这一点。")

    dirty = load_sessions()
    mask = dirty["script_name"] == "年轮"
    dirty.loc[mask, "booked_seats"] = 99
    save_sessions(dirty)

    print(f"\n  已把「年轮」的 booked_seats 手工改成 99")
    print(f"    直接读场次表      ->  {load_sessions()[mask].iloc[0]['booked_seats']}  （脏值还躺在文件里）")
    print(f"    接上明细重算后读  ->  {read_nianlun()['booked_seats']}   （被覆盖回真实值）")
    note("所以店长在 Excel 里手动改数字，污染不了系统。")

    # 演示完了把脏值修回去，否则你等下打开 Excel 看到的是 99 而不是真实值。
    # 顺带说明一件事：重算 + 写回，就是"修复被手工改坏的表"这个操作的实现。
    save_sessions(load_sessions_with_counts())
    print(f"\n  已把重算后的正确值写回文件（现在文件里是 1）")

    # ------------------------------------------------------------------
    # 收尾：两张表的全貌 + 文件位置
    # ------------------------------------------------------------------
    title("最终结果：两张表")
    print("【sessions.xlsx 场次表】")
    show(load_sessions_with_counts(), SESSION_COLS)
    print("\n【bookings.xlsx 预约明细表】")
    show(load_bookings(), ["session_id", "player_name", "seats", "status", "booked_at"])

    title("磁盘上的文件")
    for path in (SESSIONS_XLSX, BOOKINGS_XLSX):
        size = path.stat().st_size if path.exists() else 0
        print(f"  {path}")
        print(f"    存在={path.exists()}  大小={size} 字节")
    print("\n  现在用 Excel 打开这两个文件，确认中文、时间、数字显示正常。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
