# 开发说明

## 演示脚本

| 脚本 | 内容 | 需要 key |
|---|---|---|
| 01 | 最小 ReAct 循环，玩具工具，直接调用真实模型 | 是 |
| 02 | ⚠️ 表格读写演示，**删除并重写真实 `data/` 的两张表且不备份** | 否 |
| 03–06 | 过期 / 风险 / 定价 / 去重演示，临时目录，真实 `data/` 仅只读对照 | 否 |
| 07 | 文本抽取，默认 FakeLLM；`--live` 调真实模型 | 否 / 是 |
| 08 | 营销文案，默认 FakeLLM；`--live` 调真实模型 | 否 / 是 |
| 09 | Agent A–E 离线演示；`--live` 用真实模型跑 A/B/C/D + 新增路由 | 否 / 是 |

运行 `02` 前确认没有需要保留的数据。`03` 以后的脚本都不写真实 `data/`。

## 项目结构

```text
app/
  agent.py      12 个 @tool 包装、SYSTEM_PROMPT、build_agent / run_turn
  cli.py        店长端交互控制台（python -m app.cli）
  config.py     .env、模型与数据路径
  llm.py        模型调用适配与密钥脱敏
  tools/        extract / dedupe / expiry / risk / pricing / storage / copywriter / scripts
                / public_library / resources / scheduling / session_intake / intake / stages / dm_control
  integrations/ 点评渠道导入适配器、硬件 Skill 适配器契约、联网搜索（web_search）适配器
  store/        Store 接口、Excel/SQLite/Postgres 实现、租户工作区、公共库 SQL 后端、导入 CLI、迁移与导出
  web/          网页版：审批、每邮箱工作区、运营台页面/API、DM 接口
mobile_app/     Flutter 双角色客户端源码
scripts/        01–09 演示脚本（02 会重写 data/）+ fetch_vshang66 离线采集 / refresh_public_library 增量刷新
tests/          pytest 离线测试
data/           sessions.xlsx / bookings.xlsx / scripts.xlsx / public_scripts.xlsx（CLI 与 Excel 兼容模式）
docs/           详细说明
Dockerfile / render.yaml   网页版容器与 Render 配置
```

## 测试约定

- **全量离线**：`python -m pytest -q` 不需要 API key、不联网、不写真实 `data/`。目前 1719 个通过，另有 2 个可选 Postgres 契约测试默认跳过（设 `TEST_DATABASE_URL` 才跑）。
- **LLM 必须可注入**：`tests/test_extract.py` 通过 autouse fixture 阻止任何测试意外初始化真实模型；工具 1 / 7 的模型调用默认注入 FakeLLM。
- **网络必须 mock**：`tests/test_web_search.py` 全部 mock，不产生真实调用。
- **npm 包装**：`npm run build` = `compileall` + 全量 pytest，会自动寻找真实 Python 解释器，避开 Windows 应用商店的占位程序。`npm run migrate -- --init` 会建表，`--` 之后的参数原样转发给 `app.store.migrate`（`--force` / `--source` / `--tenant` 等）。

## 代码约束

- **CSP**：`script-src 'self'`，页面无内联脚本、无内联样式、无外部 CDN、无 JS 构建步骤。DOM 用 `el()` / `textContent` 构建，几何计算用 CSSOM。
- **密钥**：只从 `.env` 读，启动时只显示「已检测到」，错误信息用 `sk-***` 脱敏；场控码、DM token、API key、`DATABASE_URL` 不进日志、不进 URL。
- **本地冒烟必须清空 `DATABASE_URL`**：否则服务会连到真实 Neon 库。本地导入 / 采集必须显式 `WEB_STORAGE_BACKEND=excel`。
