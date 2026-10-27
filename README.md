# Whodunit Ops · 剧本杀拼场智能运营 Agent

**whodunit** = 推理凶案题材，**ops** = 门店运营。给剧本杀 / 密室店长的私域运营助手：ReAct Agent + 12 个确定性业务工具，装成 CLI 控制台、FastAPI 网页后台和 Flutter 双角色客户端。

它只解决一件事——**场次能不能开起来**：拼场进度、排期与 DM 派工、场控计时、剧本库事实元数据。不是 C 端组队平台，也不是收银 / 支付 SaaS；与轻兔、谋杀之谜秘书等店务 SaaS **互补，而非替代**。

| | |
|---|---|
| 仓库 | <https://github.com/ningbojian/whodunit-ops> |
| 技术栈 | Python 3.13 · LangChain 1.x / LangGraph · FastAPI · pandas · Neon Postgres · Flutter |
| 离线 | 跑测试与大部分演示脚本不需要 API key、不联网、不写真实 `data/` |

---

## 当前状态

| 模块 | 状态 | 说明 |
|---|---|---|
| 12 个业务工具 + ReAct Agent | ✅ 已实现 | A–E 五场景在**脚本模型**下离线跑通（不代表真实模型也这样选工具）；真实 DeepSeek 做过 4 次单次验证，见 [验证记录](docs/verification.md) |
| 店长 CLI 控制台 | ✅ 已实现 | `python -m app.cli`，多轮记忆 / `--read-only` / `--trace` |
| 网页版（审批 + 限时访问 + 每邮箱数据隔离） | ⚠️ **代码已实现，尚未公网部署** | 本地可跑通全流程；Neon 项目与 schema 已建，本机 HTTP 端到端已验收 |
| 资源 / 排期 / 增量录入 / 渠道单 / DM 场控 | ✅ 已实现 | 以 Web 为主，Flutter 覆盖部分 |
| 公共剧本库 | ✅ 已实现 | 5800+ 条事实元数据，只读共享 + 私有遮蔽 |
| Flutter 双角色客户端 | ⚠️ 部分验证 | `dart analyze` 0 issues、`flutter test` **82 项全部通过**、Android debug APK 已构建；**未做 release 签名、未真机联调、尚未上架** |
| 硬件灯光 / 音效联动 | ⚠️ 仅契约 | 通用 HTTP 适配器契约，**未对接任何具体灯光 / 音效厂商** |
| 公网部署 | ❌ 未执行 | **当前没有公网实例**，生产业务数据尚未正式导入 |
| 测试 | ✅ | **1550 个 pytest 通过**，另 2 个可选 Postgres 测试默认跳过 |

**怎么读这份文档**：只有标了 ✅ 的才是已实现；其余内容为已定设计或明确标注的未验证项。**明确没做的四件事**——公网部署、Flutter 真机联调、阈值标定、真实模型稳定性——不在这份文档里假装已完成。

---

## 快速开始

环境：Python 3.10+，本项目在 Python 3.13.9 下验证。Windows 终端中文乱码时命令前加 `PYTHONUTF8=1`。

```bash
python -m pip install -r requirements.txt

# 离线测试：不需要 API key、不联网、不写真实 data/
python -m pytest -q

# 店长交互控制台（真实 DeepSeek）
python -m app.cli
python -m app.cli --data-dir data --store 迷雾剧场 --thread store-manager --trace
python -m app.cli --read-only   # 工具层拒绝场次/预约/资源/排期写入

# 网页版（本地）：先在 .env 填好 WEB_SESSION_SECRET / OWNER_EMAIL，
# EMAIL_BACKEND=console 时审批链接打印在服务端控制台
python -m app.web.main         # http://127.0.0.1:8000/login
```

- **API key**：复制 `.env.example` 为 `.env`，本地填入 `DEEPSEEK_API_KEY`。程序只从 `.env` 读，不接受命令行传 key；启动时只显示「已检测到」，错误信息用 `sk-***` 脱敏。
- **可选 npm 包装**：`npm run build`（compileall + 全量 pytest）、`npm start`、`npm run migrate -- --init`。
- **数据文件**：`data/sessions.xlsx`、`data/bookings.xlsx`、`data/scripts.xlsx` 是 CLI 与 Excel 兼容模式的默认文件；文件不存在时按空表读取，**首次写入时自动创建**。网页版在 `WEB_STORAGE_BACKEND=database`（生产默认）时不读写这些 xlsx：生产写 Neon Postgres，本地未填 `DATABASE_URL` 时写 `data/web.sqlite3`。
- **演示脚本**：`scripts/01–09`，其中 `02_storage_demo.py` ⚠️ 会删除并重写真实 `data/` 的两张表且不备份。详见 [开发说明](docs/development.md)。

---

## 它能做什么

同一套 ReAct Agent 驱动五种场景。**具体工具链由模型根据系统提示词选择，不是代码级固定顺序。**

| 场景 | 店长做什么 | 工具链 |
|---|---|---|
| **A** 解析拼场广告 | 复制微信拼车文案 | `extract_message → dedupe_sessions → store_data(upsert_sessions) → assess_risk + suggest_pricing` |
| **B** 录入玩家预约 | 转发预约消息 | `extract_message → store_data(get_session / query_sessions) → store_data(record_booking)` |
| **C** 玩家临时跳车 | 转发取消消息 | `extract_message → store_data(cancel_booking) → assess_risk + suggest_pricing` |
| **D** 生成私域推文 | 下指令 | 只调用 `write_marketing_copy` |
| **E** 说缩写 / 加剧本 | 「NL 是哪个本」 | `script_library(search) →（本地没有时 web_search 一次）→（店长确认）→ script_library(upsert)` |

三条贯穿全篇的硬规则：

- **先查库再写**：抽取给出的 `candidate_session_id` 只是候选，Agent 必须先 `get_session` 确认场次存在才能写入。
- **确认前不落库**：剧本缩写候选、搜索到的公开信息、模型联想，都必须等店长确认全名后才进剧本库。
- **系统绝不自动改开场时间或取消场次**：所有改期、换房、取消都是人点击触发的显式动作。

另外两条行为约束：**场景 A 的运营建议是模型综合各工具结果的汇总输出**，不是第 13 个工具；**场景 D 的 Agent 只原样转述**——选场、两层脱敏、生成后校验都在工具内部完成，Agent 不自行改写或补写文案。

---

## 十二个业务工具

12 个工具均已实现并注册进 ReAct Agent。这里是总览，**完整契约、字段清单、错误码与已知边界见 [工具参考](docs/tools.md)**。

| # | 工具 | 一句话职责 | 读写 |
|---|---|---|---|
| 1 | [文本抽取](docs/tools.md#1-文本抽取) | 口语化拼场 / 预约消息 → 结构化 `sessions` + `bookings` | 只读 |
| 2 | [场次去重](docs/tools.md#2-场次去重) | 识别重复转发的同一场；字段级合并，空值不覆盖 | 只读 |
| 3 | [过期过滤](docs/tools.md#3-过期过滤) | 按 `start_time` 剔除已开场场次，不信 `status` 字段 | 只读 |
| 4 | [空场风险识别](docs/tools.md#4-空场风险识别) | 两个独立维度：会不会黄（`fail_risk`）、先救哪场（`priority_level`） | 只读 |
| 5 | [动态折扣定价](docs/tools.md#5-动态折扣定价) | 按到客速率倍数给四档推荐价，守住边际底价 | 只读 |
| 6 | [表格存储与读取](docs/tools.md#6-表格存储与读取) | 场次表 + 预约明细表，三种后端共用一套合并规则 | 读写 |
| 7 | [营销文案生成](docs/tools.md#7-营销文案生成) | 选场 → 双层脱敏 → 社群短版 + 朋友圈长版 | 只读 |
| 8 | [剧本库与缩写别名](docs/tools.md#8-剧本库与缩写别名) | 全名 / 别名 / 类型 / 人数 / 时长建档，先存后补 | 读写（**公共剧本库**只读） |
| 9 | [资源管理](docs/tools.md#9-资源管理) | 房间、DM、班次与缺口清单 | 读写 |
| 10 | [资源排期](docs/tools.md#10-资源排期) | 固定场次时间下分配房间 + 主 DM，排不开给原因码 | 读写 |
| 11 | [DM 场控只读查询](docs/tools.md#11-dm-场控只读查询) | 查阶段 / 剩余时间 / 降级状态，**不能推进** | 只读 |
| 12 | [联网搜索](docs/tools.md#12-联网搜索-web_search) | 本地库未命中时查公开信息；三道成本闸门，未配置 key 返回 `not_configured` | 只读（不写库） |

> **公共不变量**：门店保本底价仅内部可见，不进入任何对外文案。这条由结构保证——内部财务列登记在 `INTERNAL_COLUMNS`，下游拿到的 DataFrame 里根本没有这些列。

---

## 架构

**分层原则**：自然语言理解交给 LLM；确定性规则、数学计算、数据读写全部封装成独立代码工具，从结构上压缩幻觉空间。

**循环**：Reason → Act → Observation，直到任务完成。**记忆**：checkpointer（`InMemorySaver`）按 `thread_id` 隔离会话；会话记忆只保存对话，**不作为数据源**，场次数据以表格为准。

**技术选型**：用 LangChain 1.x 的 `create_agent` + LangGraph 状态机，不自建 ReAct 调度器、LLM 调用与 Memory 管理。LLM 后端是 DeepSeek（OpenAI 兼容接口，换模型只改一处模型标识）。0.x 时代的 `AgentExecutor` 与 `ConversationBufferMemory` 已移除，本项目不采用。

**写操作边界**：只有 `store_data` 的写 action、`script_library(upsert)`、`manage_resources` 的资源写 action、`schedule_sessions` 的 `auto_assign` / `apply` / `resolve` 会写存储，其余 8 个工具（含 `dm_control`、`web_search`）只读。业务失败返回 `ok=false`，未预期异常兜底 `internal_error`，都不向 ReAct 循环抛异常。`--read-only` 在工具层硬拦截，是代码边界而非提示词约定。

**事务边界**：Excel 模式下各表文件各自原子替换，跨文件写入没有事务；数据库模式每次表写入在事务内，跨表操作仍不是单一事务。真实数据请保留备份。

**不写业务条件分支**：图在「模型输出工具调用 → 执行工具 → 回填结果」之间循环，是否继续由模型决定；项目没有编写「因场次过期就跳过风险 / 定价 / 文案」这类分支——过期场次由 `filter_expired` 显式查询，风险与定价工具内部会自动排除已开场 / 已取消场次。

**可迁移性**：剧本杀只是验证场景。工具链本身不依赖剧本杀，可迁移到密室、轰趴、美业、餐饮包间等本地生活预约业态。

详见 [Agent 组装与控制台](docs/agent.md)。

---

## 网页版与部署状态

**代码已实现，尚未公网部署。**网页版可在本地跑通「申请 → 开发者邮件审批 → 8 小时访问 → 到期重新申请」全流程，每邮箱数据隔离（`tenant_id = email_hash[:16]`，不同邮箱互不可见）。**当前没有公网实例**，**尚未实际公网部署**。

- **存储**：业务数据、审批记录与每日用量可存入 Neon Postgres（`WEB_STORAGE_BACKEND=database`）；本地不填 `DATABASE_URL` 时写 `data/web.sqlite3`；`WEB_STORAGE_BACKEND=excel` 仍按每邮箱目录用 Excel，已不是生产默认。
- **已验证**：**Neon 项目 `shiyuan`（分支 `production`）已创建**，schema 已建，本机 uvicorn 的申请 / 审批 / 领取 / 看板 HTTP 链路端到端验收通过；2026-10-20 用真实 Resend 向 `OWNER_EMAIL` 发信成功（只验证了 Resend 账号本人收件）。**生产业务数据尚未正式导入。**
- **部署前还需要**：把**注册 Resend** 时创建的 API key 与生成的 `WEB_SESSION_SECRET` 配进 Render 环境变量，并设置 `EMAIL_BACKEND=resend`、`EMAIL_FROM=onboarding@resend.dev`、`WEB_STORAGE_BACKEND=database`（本地 Resend 发信已验证，Render 侧尚未配置）。开发机未装 Docker，所以 Dockerfile 与 `render.yaml` 虽然写好，但**尚未实际公网部署**。
- **限制**：**只能运行 1 个 Uvicorn worker**——`InMemorySaver` 是进程内存储，多 worker 会让同一用户的会话记忆分裂。**对话记忆不持久**：重启进程后聊天历史为空。Render **免费实例的磁盘是临时的**且会休眠，所以业务数据必须放 Neon。
- **不做**：**本轮不做**玩家直连对话、微信自动抓取或自动发消息、支付与订单履约、管理员后台、多 worker、富文本渲染。
- **迁移与备份**：`python -m app.store.migrate` 把本地 Excel 迁到 Neon，`python -m app.store.export` 导出租户为**十二个 xlsx**（3 张业务表 + 9 个 sidecar）用于备份 / 回滚。

访问流程、安全配额、本地启动与 Render 逐步清单见 [网页版](docs/web.md) 与 [部署](docs/deployment.md)。

---

## 能力边界

| # | 边界 |
|---|---|
| 1 | **输入侧**：不自动抓取微信群消息，全部输入由店长手动粘贴，规避微信机器人风控 |
| 2 | **履约侧**：不实现支付、订单、收银履约，与成熟门店 SaaS 互补 |
| 3 | **交互侧**：玩家不直接和 Agent 交互，信息由店长中转 |
| 4 | **模态侧**：只支持文本输入，不做 OCR |
| 5 | **剧本库侧**：不做拼音 / 模糊匹配，**绝不根据搜索结果自动写入剧本库**；公共库只读，**场次永不指向公共行** |
| 6 | **渠道单侧**：点评没有单店可用的自助公开 API，**不抓取公开页**、不登录商家后台；只支持人工导出文件导入，**不承诺实时同步** |
| 7 | **场控侧**：**DM 拥有阶段最高控制权**，**倒计时只提醒，绝不推进**；硬件只有**通用 HTTP 适配器契约** |
| 8 | **客户端侧**：Flutter **未真机联调（DM 端 10 步测试清单见 `mobile_app/README.md`，属待执行步骤）**、未上架；Web 是桌面后台与兜底，**Web 端诚实降级**——`/api/app/*` 需要 Bearer token，浏览器里业务按钮渲染为「（请在 App 内操作）」文本 |

**不做的产品形态**：**不做微信小程序**（Flutter 不能编译为小程序，维护第二套 UI 不在本轮范围；Web 兜底页仍可在微信内打开）。公共库采集是运营方离线动作，**不做成服务端常驻任务**。

---

## 风险与约束

**业务出错风险**：大模型会幻觉，可能误判剧本名、时间、人数，导致空位统计错误甚至超售。**对策**：玩家不直接与 Agent 交互，所有消息由店长转发，**店长是信息的第一道校验人**。

**责任边界**：如果无人工审核，Agent 自动确认预约、承诺优惠，履约纠纷时责任容易推给开发者。**对策**：Agent 只是店长后台辅助工具，**所有业务决策与对外承诺由店长人工确认**；线下履约主体是剧本杀门店，开发者只提供软件工具，**不承担线下经营、预约履约相关责任**。若将来开放玩家入口，必须设置**店长人工终审闸门**。

**合规约束**：不做微信自动抓取、自动发消息机器人。输入由店长手动粘贴，营销文案需人工复核后发布，以规避 IM 平台机器人风控。

**权限设计**：店长端 Agent 高权限（读写场次、看保本底价、定价、写文案）；玩家端 Agent 低权限（只能查空位、收集预约、临时预登记，**看不到底价、不能确认预约、不能承诺折扣**）。玩家端尚未实现。DM 端的权限与自动化边界见 [DM 场控](docs/dm-control.md)。

---

## 未来迭代规划

以下**均未实现**，只是方向。

1. **玩家交互能力（带人工审核闸门）**——玩家提交预约咨询，Agent 收集信息并临时锁位（30 分钟），店长审核通过才生效。待设计项：30 分钟超时释放需要定时调度组件，当前工具集中没有；DM 场控倒计时也刻意不自动推进，不能复用。
2. **多模态扩展**——OCR 识别门店海报。
3. **多 Agent 拆分**——抽取 / 决策 / 检索 / 文案各自独立，便于单独迭代测试。
4. **对接第三方店务 SaaS**——清洗后的结构化场次数据经 API 回填门店原有系统。
5. **商家移动端看板**——Web 时间看板与状态看板已实现；**移动端拖拽看板未实现**，改期属低频管理动作，触屏误触成本高，下一轮再评估。
6. **经营复盘报表**——场控结束后的单场汇总已实现；跨场次的周空场率、折扣使用率、发车成功率**仍未实现**。
7. **分级阈值标定**——工具 4 / 5 的阈值与成本参数全是占位值，必须由门店用自身数据标定。`normal_arrival_rate` 要最先标定，因为风险排序本身取决于它。结构可论证、数字不可推导，详见 [参数标定](docs/tuning.md)。

---

## 文档索引

| 文件 | 内容 |
|---|---|
| [docs/tools.md](docs/tools.md) | 12 个工具的完整契约、字段、错误码、已知边界 |
| [docs/agent.md](docs/agent.md) | Agent 组装、写操作边界、CLI 控制台参数 |
| [docs/web.md](docs/web.md) | 网页版访问流程、数据隔离、安全配额、本地启动 |
| [docs/deployment.md](docs/deployment.md) | Neon、Render、Excel→数据库迁移、免费层限制 |
| [docs/scheduling.md](docs/scheduling.md) | 资源排期、建场、渠道单（含大众点评导出文件导入）、看板、先审后写 |
| [docs/dm-control.md](docs/dm-control.md) | 阶段模板、场控状态机、硬件降级、Flutter 双角色端 |
| [docs/public-library.md](docs/public-library.md) | 公共剧本库的读写下界、采集与合规边界 |
| [docs/verification.md](docs/verification.md) | 真实模型验证记录、Flutter 验证状态、浏览器验收矩阵 |
| [docs/development.md](docs/development.md) | 演示脚本、项目结构、测试约定 |
| [docs/tuning.md](docs/tuning.md) | 阈值标定顺序与已知偏差 |

## 项目结构

```text
app/
  agent.py      12 个 @tool 包装、SYSTEM_PROMPT、build_agent / run_turn
  cli.py        店长端交互控制台
  tools/        extract / dedupe / expiry / risk / pricing / storage / copywriter / scripts
                / public_library / resources / scheduling / session_intake / intake / stages / dm_control
  integrations/ 点评渠道导入、硬件 Skill 契约、联网搜索
  store/        Store 接口、Excel/SQLite/Postgres 实现、租户工作区、迁移与导出
  web/          网页版：审批、每邮箱工作区、运营台页面/API、DM 接口
mobile_app/     Flutter 双角色客户端源码
scripts/        01–09 演示脚本（02 会重写 data/）+ 公开库离线采集
tests/          pytest 离线测试
docs/           本文档索引中的详细说明
```
