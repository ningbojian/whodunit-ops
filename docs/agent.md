# Agent 组装与控制台

实现见 `app/agent.py` 与 `app/cli.py`。

---

## Agent 组装

- **工具注册**：`build_store_tools()` 用 `@tool` 把 12 个业务函数包装成 12 个 Agent 工具，统一返回 JSON 信封 `{"ok", "data", "warnings", "error_code", "error"}`。包装层只做参数适配、依赖注入（Store 或 Excel 路径、当前时间、可注入的 LLM / 搜索 Provider）与结果序列化，**不重新实现业务规则**；确定性逻辑仍在 `app/tools/`。
- **图与记忆**：`build_agent()` 调用 `langchain.agents.create_agent`，接入 `InMemorySaver` checkpointer。`run_turn()` 以 `thread_id` 区分会话：同一 thread 保留多轮工具调用上下文，不同 thread 相互隔离。会话记忆只保存对话，**不作为场次数据源**。
- **路由**：`SYSTEM_PROMPT` 写明了 A–N 路由的推荐工具链、写操作触发条件与失败转述规则。其中 F–K 覆盖资源初始化、场次增量录入与排班、资源不足、点评导入和 DM 场控查询；N 覆盖网络查询（本地库未命中 → `web_search` 一次 → 标注非官方 → 店长确认全名后才写库）。**实际是否按推荐链执行由模型决定**——离线脚本模型不能证明真实模型的调度能力。
- **写操作边界**：只有 `store_data` 的写 action（`upsert_sessions` / `record_booking` / `cancel_booking` / `create_session` / `update_session`）、`script_library` 的 `upsert`、`manage_resources` 的资源写 action，以及 `schedule_sessions` 的 `auto_assign` / `apply` / `resolve` 会写存储，且只在店长明确要求录入、预约、跳车、加剧本、补资源或确认排期时执行。`dm_control`、`web_search` 及另外 6 个工具只读。
- **异常边界**：业务失败返回 `ok=false`，未预期异常兜底为 `internal_error`，均不向 ReAct 循环抛异常（`KeyboardInterrupt` / `SystemExit` 除外）。`--read-only` 在工具层硬拦截上述全部写动作，是代码边界而非提示词约定。
- **事务边界**：Excel 模式下各表文件各自原子替换，跨文件写入之间没有事务；数据库模式下每次表写入在事务内完成，跨表操作仍不是单一事务。真实数据请保留备份。

**运行方式**：`scripts/09_agent_demo.py` 默认离线，使用临时目录中的合成数据依次跑 A–E（E 为两轮：缩写查询、剧本档案补全），不写真实 `data/`；`--live` 才调用真实 DeepSeek，先跑 A/B/C/D，再跑新增路由四轮，且只打印「检测到 DEEPSEEK_API_KEY：是/否」。

---

## 店长交互控制台

入口 `python -m app.cli` 或 `python -m app`。Agent 只构建一次，同一进程内连续对话；`scripts/01–09` 仍是单向演示。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--data-dir PATH` | 项目 `data/` | 包含三张 Excel 的目录，目录必须已存在 |
| `--store NAME` | 未指定 | 抽取缺门店文本时的默认门店，并作为「当前门店」追加进系统提示词 |
| `--thread ID` | `store-manager` | 初始会话线程 |
| `--read-only` | 关 | 工具层拒绝场次 / 预约 / 资源 / 排期写入，返回 `error_code=read_only`；`dm_control` 仍可只读查询 |
| `--trace` | 关 | 每轮打印工具名、截断参数、`ok`、`error_code` |

**命令**：`/help`、`/reset`（换新线程，不改数据）、`/status`（路径、门店、线程、模式、消息数）、`/quit` / `/exit`。

**行为与边界**

- 同一 `thread_id` 保留多轮上下文；`/reset` 切到 `store-manager-1`、`-2`……旧线程留在内存直到进程退出。`InMemorySaver` 是进程内存储，退出即丢失。
- 单轮模型异常会被捕获并用 `sk-***` 脱敏，循环继续；空输入忽略；EOF / Ctrl+C 干净退出。
- 默认**可写**：启动 banner 写出三张表的完整路径，并明确提示没有逐次确认。试用真实数据前建议加 `--read-only`，或用 `--data-dir` 指向副本 / 临时目录。
- `--store` 是默认值提示，不是硬过滤；店长明确指定其他门店时以指令为准。
- 这是终端控制台，不是网页版运营台，也不是 Flutter 客户端；不做玩家直连、不自动发送微信、不实现支付履约。
- 控制台命令、会话隔离、错误恢复、轨迹渲染与只读闸门由 `tests/test_cli.py` 离线覆盖。
