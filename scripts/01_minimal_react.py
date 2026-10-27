"""第一步烟测：最小 ReAct Agent。

一个文件同时验证四件事：
  1. 依赖装好了   → 几个 import 不报错
  2. API key 通了 → 模型能返回内容
  3. 工具调用通了 → Agent 自己决定去调 get_current_time
  4. 记忆通了     → 第二轮能复述第一轮的问题（checkpointer 生效）

运行：
  conda run -n base python scripts/01_minimal_react.py

注意：这是**一次性烟测脚本**，不是最终架构的一部分。
里面的 get_current_time 不会进 app/tools/，后续「过期过滤工具」会另写自己的时间获取逻辑。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认 stdout / stderr 编码是 GBK，中文会全部乱码。
# 显式切成 UTF-8，保证终端里能正常看到中文输出与报错信息。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 让脚本能 import 到项目根目录下的 app 包（scripts/ 与 app/ 平级）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain.agents import create_agent  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.config import get_model  # noqa: E402


# ===========================================================================
# ① 玩具工具 —— 只为验证链路，非业务工具
# ===========================================================================
@tool
def get_current_time() -> str:
    """获取当前日期和时间。

    当用户询问"现在几点""今天几号"，或需要判断某个场次是否已经开场时使用。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# ② 组装 Agent
# ===========================================================================
def build_agent():
    """把 model、tools、prompt、checkpointer 四样东西拼成一个 Agent。

    create_agent 内部生成一张 LangGraph 状态图：
      model 节点 ←→ tools 节点，循环直到模型不再要求调用工具为止。
    这就是"ReAct 循环"在 1.x 里的形态 —— 它由框架托管，我们不再手写调度。
    """
    return create_agent(
        model=get_model(temperature=0),
        tools=[get_current_time],
        system_prompt="你是一个剧本杀门店运营助手，回答简洁。",
        # checkpointer 负责保存每一轮的对话状态；
        # 不传它，thread_id 就毫无意义，多轮之间也不会有任何记忆。
        checkpointer=InMemorySaver(),
    )


# ===========================================================================
# ③ 可观测性 —— 把这一轮真正发生的工具调用打印出来
# ===========================================================================
def show_trace(new_messages) -> None:
    """只打印**本轮新增**消息里的工具调用。

    为什么需要自己还原：
      0.x 的 AgentExecutor 会把 Thought / Action / Observation 作为纯文本打印出来，
      肉眼可见；1.x 的 create_agent 把它们变成了结构化的 messages
      （AIMessage.tool_calls / ToolMessage），框架内部循环不再对外吐文本。
      可观测性没丢，只是从"读日志"变成了"读消息对象"。

    ⚠️ 这里必须传"本轮新增"的消息，不能直接把 invoke 的返回值丢进来：
      invoke 返回的 messages 是该 thread **累积的全部历史**，
      直接遍历会把前面每一轮的工具调用重复打印一遍。
    """
    print("  [轨迹]")
    called = False

    for msg in new_messages:
        # 模型决定调用工具时，会在 AIMessage 上挂 tool_calls
        for call in getattr(msg, "tool_calls", None) or []:
            called = True
            print(f"    -> 模型决定调用 {call['name']}({call['args']})")
        # 工具执行完的结果，以 ToolMessage 形式回填给模型
        if msg.__class__.__name__ == "ToolMessage":
            print(f"    <- 工具返回 {msg.content!r}")

    if not called:
        print("    （本轮未调用任何工具，模型直接作答）")


def ask(agent, config, question: str) -> str:
    """问一句，打印本轮轨迹和回答，返回回答文本。"""
    print(f"\n用户：{question}")

    # 先向 checkpointer 查一下这个 thread 现在存了多少条消息，
    # 用于把本轮新增的部分切出来。顺便演示 get_state() 怎么读记忆。
    before = len(agent.get_state(config).values.get("messages", []))

    # invoke 的入参是一个 dict，messages 是必需的 key；
    # config 里的 thread_id 决定这次对话属于哪一段"记忆"。
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config,
    )

    # 切片：只要 before 之后新增的消息
    show_trace(result["messages"][before:])

    # messages 列表的最后一条，就是模型最终给用户的答复
    answer = result["messages"][-1].content
    print(f"Agent：{answer}")
    return answer


# ===========================================================================
# ④ 三个场景，逐一验证
# ===========================================================================
def main() -> int:
    agent = build_agent()

    # 同一个 thread_id 代表同一段会话记忆
    config = {"configurable": {"thread_id": "demo-session-1"}}

    print("=" * 62)
    print("第 1 轮：应该触发工具调用（模型自己判断需要看时间）")
    print("=" * 62)
    ask(agent, config, "现在几点了？")

    print()
    print("=" * 62)
    print("第 2 轮：不该调用工具（时间上轮已经查过），但必须答对")
    print("=" * 62)
    ask(agent, config, "我刚才问你的第一个问题是什么？")

    print()
    print("=" * 62)
    print("对照实验：换一个全新的 thread_id，应该完全不记得上面两轮")
    print("=" * 62)
    fresh_config = {"configurable": {"thread_id": "demo-session-2"}}
    ask(agent, fresh_config, "我们之前聊过什么吗？")

    # ------------------------------------------------------------------
    # 客观证据：直接问 checkpointer 要两个 thread 各自的消息条数。
    # 这一段不依赖模型怎么说，是硬证据。
    # ------------------------------------------------------------------
    print()
    print("=" * 62)
    print("客观证据：两个 thread 在 checkpointer 里各存了多少条消息")
    print("=" * 62)
    counts = {}
    for tid in ("demo-session-1", "demo-session-2"):
        state = agent.get_state({"configurable": {"thread_id": tid}})
        counts[tid] = len(state.values.get("messages", []))
        print(f"  thread {tid}  ->  {counts[tid]} 条消息")

    a, b = counts["demo-session-1"], counts["demo-session-2"]
    print()
    print(f"解读：两个 thread 相差 {a - b} 条消息。同一份代码、同一个 agent 实例，")
    print("      仅靠 thread_id 就隔离出了两段互不可见的记忆 —— 这就是 1.x 里")
    print("      checkpointer + thread_id 替代 ConversationBufferMemory 的方式。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
