# 剧本杀拼场智能运营 Agent

> **当前状态：核心交付、网页版与 DM 场控代码已完成。** 12 个业务工具（原有文本抽取、场次去重、过期过滤、空场风险识别、动态折扣定价、表格读写、营销文案生成、剧本库 / 缩写别名，资源管理、排期、DM 场控只读查询，新增联网搜索 web_search）、ReAct Agent 组装、店长交互控制台、网页版（邮件审批 + 8 小时限时访问 + 每邮箱数据隔离）、公共剧本库（只读检索 + 私有遮蔽）、对话操作按钮、资源排期、增量录入、大众点评导出文件导入与 DM 场控均已实现；**1550 个 pytest 测试通过，另有 2 个可选 Postgres 测试默认跳过**，离线运行不需要 API key。A/B/C/D/E 五场景在脚本模型下离线跑通（E 含缩写查询与剧本档案补全两轮）；**A/B/C/D 已于 2026-10-19 用真实 DeepSeek 单次验证**，四场景均按预期调用工具，记录与限制见 4.3。新增的排期与场控能力由离线测试覆盖，并已于 2026-10-21 用真实 DeepSeek 单次验证资源缺口、场次录入 + 自动分配、排期缺口、DM 场控只读四条路由（见 4.3；当时场次录入尚经草稿转正，该机制已在后续迭代移除，现为硬必填齐全直接建场，见 4.5）；同样是单次样本，不构成稳定性结论。2026-10-24 新增（见 4.8）：Web 排期时间看板（拖拽改期 + 冲突建议）与状态看板、资源页剧本筛选分页、DM 请假与带本档案、对话剧本名唯一匹配免确认快速通道。2026-10-25 新增（见 4.9）：排期页建场卡片（类型 / 人数筛选，可手动指定房间与 DM）、建场与应用分配后不再整页刷新、「补齐未分配」与「生成优化方案」改为预览 → 逐条勾选 → 批量应用、资源模板中文表头（兼容旧英文表头）。
> 存储层已完成 Excel → 数据库的迁移代码：业务工具统一通过 Store 接口访问数据，生产使用 Neon Postgres；本地启用数据库后端且不填 `DATABASE_URL` 时落到 SQLite；Excel 保留为 CLI、开发默认模式与导入导出格式。**Neon 项目 `shiyuan`（分支 `production`）已创建，数据库 schema 已建好，并已在真实 Neon 上完成本机 HTTP 端到端验收**；生产业务数据尚未正式导入。网页版代码、Dockerfile 和 `render.yaml` 已准备，但**尚未实际公网部署**，仍需配置 GitHub 与 Render，见 4.4。**2026-10-20 已用真实 Resend 向 `OWNER_EMAIL` 成功发送并收到一封审批邮件**（仅开发者本人邮箱；没有验证发给其他人的情况）。
> 已实现：`app/config.py`、`app/llm.py`、`app/agent.py`、`app/cli.py`、`app/tools/`（extract / dedupe / expiry / risk / pricing / storage / copywriter / scripts / public_library / resources / scheduling / session_intake / intake / stages / dm_control）、`app/integrations/`（点评导入、硬件适配器契约、联网搜索 web_search 适配器）、`app/store/`（Store 接口、SQLite/Postgres 实现、租户工作区、公共库 SQL 后端、导入 CLI、迁移与导出）、`app/web/`、`tests/`（1550 个测试）、`scripts/`（9 个演示脚本 + vshang66 离线采集 / 增量刷新脚本）、`mobile_app/`（Flutter 双角色客户端源码）。
> **未验证 / 未完成**：2026-10-21 已在本机安装 Flutter 3.47.7（Dart 3.13.5）与 Android SDK 36 + JDK 17，`mobile_app/` 已执行 `flutter pub get`；`dart analyze .`（0 issues）与 `flutter test` 分日复跑：10-21 基线 29 项、10-22 增至 61 项、10-23 复跑 73 项、10-24 复跑 **74 项全部通过**、10-25 复跑 **82 项全部通过**（新增排期审核流纯函数 8 项）（新增 `sessionStatusKey` 中文状态映射 4 项；随后场次草稿功能移除，删去 3 项草稿用例；因项目路径含中文，`flutter analyze` 的 LSP 分析会崩溃，静态分析以 `dart analyze` 为准）；2026-10-22 已在 ASCII 路径副本上执行 `flutter build apk --debug` 并产出占位 debug APK（包名 `com.example.jbs_operator`，见 `mobile_app/README.md` 的构建注意事项）；但**未做 release 签名、未真机联调、尚未上架**（iOS 构建需要 macOS + Xcode，本机未构建 iOS；DM 端逐步测试清单见 `mobile_app/README.md`，属待执行步骤而非已通过项）；硬件模式只有通用 HTTP 适配器契约，**未对接任何具体灯光 / 音效厂商**；当前没有公网实例，生产业务数据尚未正式导入。第九节为未来规划，未实现；其余未标注「已实现」的内容为已定设计或明确标注的未验证项。

## 快速开始

**环境**：Python 3.10+；本项目在 Python 3.13.9 下验证。Windows 终端若中文乱码，命令前加 `PYTHONUTF8=1`。

```bash
python -m pip install -r requirements.txt

# 离线测试：不需要 API key、不联网、不写真实 data/
python -m pytest -q

# 可选：用 npm 统一调用上面的 Python 命令（会自动寻找真实解释器，
# 避开 Windows 应用商店的 python 占位程序）
npm run build     # compileall + 全量 pytest
npm start         # 启动网页版
npm run migrate -- --init   # 建表，其余参数原样转发

# 店长交互控制台（真实 DeepSeek）
python -m app.cli
python -m app          # 等价入口
python -m app.cli --data-dir data --store 迷雾剧场 --thread store-manager --trace
python -m app.cli --read-only   # 工具层拒绝场次/预约/资源/排期写入

# 网页版（本地）：先在 .env 填好 WEB_SESSION_SECRET / OWNER_EMAIL，
# EMAIL_BACKEND=console 时审批链接打印在服务端控制台
python -m app.web.main         # http://127.0.0.1:8000/login
```

**API key**：复制 `.env.example` 为 `.env`，在本地填入 `DEEPSEEK_API_KEY`。程序只从 `.env` 读取，不接受命令行传 key；启动时只显示“已检测到”，错误信息会用 `sk-***` 脱敏。

**数据文件**：`data/sessions.xlsx`（场次表）、`data/bookings.xlsx`（预约明细表）与 `data/scripts.xlsx`（剧本库）是 CLI 与 Excel 兼容模式的默认数据文件。文件不存在时按空表读取，首次写入时创建；剧本库也可以只依靠场次历史做临时匹配，不要求预先建档。网页版在 `WEB_STORAGE_BACKEND=database`（生产默认）时不读写这些 xlsx：生产环境写 Neon Postgres，本地未填 `DATABASE_URL` 时写 `data/web.sqlite3`；`WEB_STORAGE_BACKEND=excel` 仍按每邮箱目录使用 Excel。

**演示脚本**：

> ⚠️ `scripts/02_storage_demo.py` 会删除并重写真实 `data/` 下的两张表，且不备份。运行前确认没有需要保留的数据。

| 脚本 | 内容 | 需要 key |
| --- | --- | --- |
| 01 | 最小 ReAct 循环，玩具工具，直接调用真实模型 | 是 |
| 02 | ⚠️ 表格读写演示，删除并重写真实 `data/` | 否 |
| 03–06 | 过期 / 风险 / 定价 / 去重演示，临时目录，真实 `data/` 仅只读对照 | 否 |
| 07 | 文本抽取，默认 FakeLLM；`--live` 调真实模型 | 否 / 是 |
| 08 | 营销文案，默认 FakeLLM；`--live` 调真实模型 | 否 / 是 |
| 09 | Agent A–E 离线演示；`--live` 用真实模型跑 A/B/C/D + 新增路由四轮 | 否 / 是 |

**项目结构**：

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
mobile_app/     Flutter 双角色客户端源码（analyze/test 已通过，Android debug APK 已构建，未真机联调）
scripts/        01–09 演示脚本（02 会重写 data/）+ fetch_vshang66 离线采集 / refresh_public_library 增量刷新
tests/          pytest 离线测试
data/           sessions.xlsx / bookings.xlsx / scripts.xlsx / public_scripts.xlsx（CLI 与 Excel 兼容模式）
Dockerfile / render.yaml   网页版容器与 Render 配置
```

## 一、项目定位

**一句话**：基于 ReAct 范式的轻量智能体，面向剧本杀 / 密室门店店长，作为**私域运营决策辅助助手**。

**不做什么**：不是 C 端组队平台，也不是完整的收银 SaaS。

**与既有系统的关系**：与轻兔、谋杀之谜秘书等门店店务管理 SaaS 互补，而非替代。

## 二、核心设计：ReAct 范式

**循环定义**：Reason（思考）→ Act（调用工具）→ Observation（拿到结果），循环直到任务完成。

**分层原则**：自然语言理解交给 LLM；所有确定性规则、数学计算、数据读写全部封装成独立代码工具，规避大模型幻觉。

**会话记忆**：当前实现采用 checkpointer（`InMemorySaver`）保存每一轮 Thought / Action / Observation 上下文，按 `thread_id` 隔离会话，让 Agent 在多轮工具调用之间保持场次上下文连贯。

**数据源边界**：场次数据以表格为准，会话记忆只保存对话上下文，不作为数据源。

## 三、十二大业务工具

以下为 12 个工具的接口设计。**12 个工具均已实现**（见下方 ✅ 标注），并已注册进 `app/agent.py` 的 ReAct Agent，注册与依赖注入方式见 四.1。

> **公共不变量**：门店保本底价仅内部可见，不进入任何对外文案。

1. **文本抽取工具** —— ✅ **已实现**：接收社群 / 朋友圈口语化拼场文案、玩家预约 / 跳车消息，一次调用输出两组结构化记录：`sessions`（场次）与 `bookings`（预约明细）。

   **只读文本，不写库。** `sessions` 交给工具 2 去重、再交给工具 6 落盘；`bookings` 里的 `candidate_session_id` 只是「门店 + 剧本 + 时间」算出的**候选**，本工具不查场次表，也不调用 `upsert_booking` / `cancel_booking`。Agent 必须先查库确认场次存在，再决定写入动作。

   **输出契约**：场次记录包含落盘字段（门店、剧本、类型、开场时间、总位、原价、状态、原文）与审计字段。原文里的「已约 N 人」「还差 N 人」只以 `remaining_reported` / `booked_reported` 存在，**永远不落盘**——人数真相只能来自预约明细；`start_time_text`、`start_time_source`、`source_excerpt`、`notes` 同样只供店长核对，不进入 Excel。`sessions_frame()` 只保留落盘字段，可直接喂 `dedupe_sessions()`。

   **时间采用混合解析，原文确定性优先**：模型同时输出原文时间短语与绝对时间。代码先验证短语确实出现在原文里，再按确定性规则解析完整日期、月日 / 号 + 时刻；解析不了才采用模型推断值，并写入「时间由模型推断」备注。原文时间允许 `[now-7 天, now+370 天]`，模型推断收紧到 `[now-1 天, now+180 天]`；两者不一致时以原文为准并标注。「今晚 / 明晚 / 周六 / 下周六」属于模型推断范围，不假装是代码算出来的。

   **失败与降级分开**：空输入、缺 API key、模型调用异常、坏 JSON、顶层结构错误都返回结构化失败（`ok=false` + `error_code` + `error` + `raw_snippet`），不向调用方抛异常；单条记录不是对象则跳过并警告；单个字段坏则降级为 `null` + `notes`，整批仍然成功。坏 JSON **不自动修复**，否则模型输出质量下降会被悄悄掩盖。

   **防幻觉是缓解，不是消除**：Prompt 要求原文没有的值一律 `null`、原文只当数据不当指令；代码再校验时间短语、`source_excerpt`、门店 / 剧本是否逐字出现。店长仍是第一道校验人。已知边界：只支持文本、不做 OCR、不接微信；不做全角/别名/模糊匹配；超长文本不分块；单条消息里的多场 / 多玩家依赖模型正确拆分。实现见 `app/tools/extract.py`，LLM 适配层见 `app/llm.py`。

   **运行方式**：`scripts/07_extract_demo.py` 默认使用脚本内的 FakeLLM，离线、不需要 API key；加 `--live` 才调用真实 DeepSeek。API key 由使用者自行填入本地 `.env`，代码只判断是否存在，错误信息输出前会做 `sk-***` 脱敏。`tests/test_extract.py` 通过 autouse fixture 阻止任何测试意外初始化真实模型。
2. **场次去重工具** —— ✅ **已实现**：识别商家重复转发的同一条拼场信息。同一场次的定义是 `门店 + 剧本 + 开场时间`（归一化到分钟）——三个维度都相同才是同一场，19:30 和 21:30 是两车。

   **合并以字段为单位，不以行为单位。** 同一场转发三次：先发完整海报（8 人本、168 元、情感本），几小时后发「还差 2 人」，改价后再发「今晚特价 158」。按整行去重会只剩最后一条残缺记录，总位、类型、原价全部丢失；本工具逐字段合并，**最后一个非空值胜出，空值永不覆盖**，结果是 `8 人本 / 情感本 / 158`。这同时修掉了跨批次写入时 `NA` 抹掉旧值的同类缺陷——两个事故是同一个 bug 的两种表现。

   两个**非空**值冲突（如 168 与 158）时取后者（改房、改价都会重发，后发的更接近当前状态），但字段名会写进 `conflict_fields` 对调用方可见。`source_text` 刻意排除在冲突报告外：同一场转发三次原文必然三份不同，算进去会让报告永远非空。

   **身份缺失不静默撞键，也不让整批崩掉。** 门店 / 剧本 / 开场时间缺失或时间无法解析时，坏行**不参与合并、原样放行**，原因写入 `dedupe_note`（`门店缺失` / `剧本缺失` / `开场时间缺失` / `开场时间无法解析`）。旧实现会把 `str(pd.NA) == "<NA>"` 当身份参与哈希，所有缺门店的行撞成同一场；`pd.NaT.strftime()` 则直接抛异常。身份判定必须在类型整形之前完成——整形会把「无法解析」静默变成 `NaT`，之后就分不清了。

   **显式 `session_id` 是调用方的逃生舱。** 场次表**没有房间字段**，同店 + 同剧本 + 同时间完全可能是两个房间同时开。调用方给出两个不同的显式 ID 时，工具不合并，只标注 `联合键相同但session_id不同`；显式 ID 与联合键算出的值不一致时，保留显式值并标注。名称只做 `strip`，不做全角/半角、别名或模糊匹配（`年轮` 与 `年轮（情感本）` 是两个剧本）。

   与现有表比对后输出四种动作：`更新` / `无变化` / `新增` / `未比对`。`changed_fields` 只统计「传入值非空且与旧值不同」的字段——空值不会覆盖，所以不算变化；`booked_seats`、`status` 永不参与比对，因为 storage 落盘时会无条件保留它们。

   **全项目只有一份合并规则**：策略语义在工具 2，物理实现在工具 6 的 `storage.py`，`upsert_sessions` 复用同一份代码，两边不会漂移。错误策略刻意不对称：分析层放行并标注，落盘层对算不出主键的行抛 `ValueError`——主键为空的行一旦进 Excel，身份就永久丢失。分析列 `dedupe_action` / `merged_count` / `conflict_fields` / `changed_fields` / `dedupe_note` **永不落盘**。

   本工具**没有需要标定的阈值**：「同一场次」是定义，不是经验参数（与工具 4、5 不同）。已知边界：区分不了同店同剧本同时间的两间房（靠显式 ID）；已有重复数据不会被清理——本工具是写前分析层，不是表清洗器。实现见 `app/tools/dedupe.py`。
3. **过期过滤工具** —— ✅ **已实现**：对比当前时间，自动过滤已经开场、失效的场次，不纳入后续运营分析。是否过期**以 `start_time` 为准**，不信任表里存的 `status` 字段（写着「待开场」但时间已过，照样算过期）；时间无法解析的场次保留而非丢弃。实现见 `app/tools/expiry.py`。
4. **空场风险识别工具** —— ✅ **已实现**：根据「距离开场剩余时长、剩余空位」评估空场风险，输出**两个互相独立的维度**：

   - `fail_risk`（`高 / 中 / 低`）—— 这场**会不会黄**。预警用，决定要不要提前通知客人、准备流局。
   - `priority_level`（`高危 / 关注 / 正常`）—— 现在**该先动手救哪场**。派活用，决定朋友圈与折扣投给谁。

   两者的方向经常**相反**：缺口大 = 情况糟（更容易黄），同时缺口大 = 难救（更不值得动手）。同一个变量回答两个方向相反的问题，因此不能只算一个「危险度」。上一版正是按危险度排序，把救不回来的场次排在最前、推一把就锁定的排在最后，方向是反的。现在排序只按 `priority_level`；救不回来的场次不丢弃，仍留在结果里（带着 `fail_risk = 高`），可按该列单独筛出「该考虑流局的场次」。

   **核心量是需要的到客速率** = 剩余空位 ÷ 剩余小时（人/小时），再除以门店常态到客速率得到倍数。它把人数与时间放进同一尺度，且天然是绝对人数口径（房间大小不影响找人难度）。优先级 = 紧迫档（≤2h / ≤6h / 其余）× 可救性档（倍数 ≤1 / ≤2 / >2），两档**相乘**——只有「快开场了 + 还救得回来」才判高危，相加或取 max 会把「已经没救」和「还早得很」都误判成该动手。规模（总位）单独一列，**不计入打分**，只作排序的第二个关键字。

   已满员（含超额预约）的场次所有档位归零，直接判正常。等级**不落盘**，每次读取时重算，因此空位填满后高危标记会自行消失；已开场与已取消的场次由工具 3 在函数内部自动剔除，调用方不必记得先调工具 3。

   ⚠️ **阈值目前为占位值，未经任何真实门店数据标定。** 模型结构（时间越少越紧迫、需要的人比平时来的多就越难救、满员无风险）可论证，具体数值只能由门店标定。其中 `normal_arrival_rate`（门店常态到客速率）影响最大——**谁比谁优先直接取决于它**，详见 九.7。实现见 `app/tools/risk.py`。
5. **动态折扣定价工具** —— ✅ **已实现**：回答「这场该让多少、让到哪条线为止」。复用工具 4 的 `rate_multiple`，按倍数分四档给出对外推荐售价：原价 / 轻让 95% / 中让 88% / 重让 80%。

   **折扣只作用于剩余空位**（限时特价），已预约的客人仍按原价结算。设已约 `b` 人、原价 `p₀`、每人变动成本 `v`、降价后多来 `k` 人，相比不降价的差额是 `k·(p − v)`——**只要售价高于 `v`，每多来一个人都是净赚的，与降了多少无关**。所以折扣力度不影响「划不划算」，只影响「能拉来几个人」；后者取决于价格弹性，门店没有这个数据。因此档位是经验值，模型算的是让利的经济账，不声称算出了最优价格。

   **两条保本线，不能混用**：

   - `floor_price` = 每人变动成本，**边际底价**，硬约束。低于它，多卖一个位子多亏一笔。与人数、原价都无关。
   - `break_even_price` = 固定成本 ÷ 到客人数 + 变动成本，**均摊保本价**。人越少固定成本摊得越薄，它越高——10 人本到客 2 人时可达 260 元，高于原价。**不能当售卖下限**，它回答的是「这个价卖满，这场赚不赚」。

   最有行动含义的一列是 `contribution_per_seat` = 推荐价 − 变动成本，即「每多来一个客人，这场少亏多少钱」。

   ⚠️ **与工具 4 的结论经常相反，这是刻意的**：工具 4 的 `rescue_level = 0`（倍数 > 2）意思是**填不满**，判「正常，别浪费推广力气」；工具 5 问的是**能不能少亏**，填不满不等于不该少亏。所以倍数 4.0 的场次在工具 4 里是「正常」、在工具 5 里是「重让 80%」。反过来，工具 4 判「高危」的场次（倍数 ≤ 1）恰恰**不需要**打折——自然流量就够，发条朋友圈即可。一句话：**要推广 ≠ 要让利**。折扣的触发条件是倍数 > 1，不是工具 4 的 `rescue_level == 1`。

   **底价外泄防护做成结构而非约定**：内部财务列登记在 `INTERNAL_COLUMNS`，下游（工具 7 文案生成）通过 `public_view()` 取数，拿到的 DataFrame 里根本没有这些列，想写也写不出来。注意 `contribution_per_seat` **不在**这份名单里（它仍是成本推导值），工具 7 另有一层 `COPY_FIELDS` 白名单，见 三.7。公共不变量见本节开头。

   ⚠️ **成本与折扣参数目前为占位值，未经任何真实门店数据标定。** 结构（固定成本 + 变动成本、让利不破底价、人越少保本价越高）可论证，具体数值只能由门店账本标定，详见 九.7。已知偏差：`contribution_per_seat` 是上界（一部分降价买家本会按原价购买，这部分损失未计入）；单场视角，不处理同一晚多场折扣互相抢客；总位是时长的代理指标（12 人机制本与 12 人欢乐本时长可能差一倍，成本却算成一样）。实现见 `app/tools/pricing.py`。
6. **表格存储 & 读取工具** —— ✅ **已实现**：采用双表模型。**场次表**记录有哪些场、什么时间、几位、多少钱；**预约明细表**记录谁约了哪一场。玩家预约 / 跳车消息输入后自动更新空位。已约人数由预约明细汇总得出，同一玩家同一场次重复预约不会重复计数，避免消息被转发两次就多算一人。存储通过 `Store` 接口与业务逻辑解耦：CLI 与导入导出使用 `ExcelStore`；Web 数据库模式在本地（无 `DATABASE_URL`）使用 `SqlStore`（SQLite），生产使用 `SqlStore`（Neon Postgres）。三种后端共用同一套合并规则，写入语义均由整表替换保证。切换方式见 4.4。
7. **营销文案生成工具** —— ✅ **已实现**：读取实时场次数据，内部完成选场，生成**社群短版**与**朋友圈配图长版**两套文案。只读数据、不写库、不自动发布，店长人工复核后复制发布。

   **输入与选场**：直接接收 `storage.load_sessions_with_counts()` 的场次表，内部调用工具 4 / 5 取得优先级与推荐价，不要求调用方先定价。选场采用**逐级回退**：高危 → 关注 → 正常可推广场次；高危不足时用关注补，关注不足再用正常补满名额，低级别绝不排到高危前面。已满员、已开场、已取消、缺少门店 / 剧本 / 开场时间或剩余空位的场次被排除，并给出跳过说明。默认最多 3 场，硬上限 5 场。

   **一次只写一个门店**：一条文案混两家门店会让玩家不知道去哪。未指定 `store_name` 且可推广场次跨多个门店时返回 `bad_request`，要求调用方指定门店；指定后只取该门店。

   **两层脱敏，不靠注释提醒**：工具 5 的 `public_view()` 会剔除 `floor_price` / `break_even_price` / `sunk_cost` / `profit_if_sold_out`，但**保留 `contribution_per_seat`**——它同样是成本推导值。因此工具 7 在 `public_view()` 之后再加一层 `COPY_FIELDS` 白名单：只有门店、剧本、类型、开场时间、总位、已约、剩余、原价、推荐价、省下金额能进入 Prompt 和结果；风险等级、`contribution_per_seat`、`source_text`、内部财务列都不会出现。白名单是「按名单保留」，将来工具 5 新增列默认进不了 Prompt。

   **生成后两道整次失败**：提示词要求不写内部财务概念、不编造价格与折扣，但提示词不是保证。代码在生成后再查两件事：出现「保本 / 底价 / 成本 / 利润 / 内部价 / 进价」或内部列名 → `internal_leak`；出现数据里没有的价格数字、把省下金额当售价、或「X 折」等未授权折扣 → `price_mismatch`。两种情况下 `community` 与 `moments` **同时置空**，不返回半套文案——有字就可能被店长直接复制。

   **失败与降级**：空表 / 全部不可推广 → `no_sessions`；参数或表结构错误 → `bad_request`；缺 API key → `config_error`；模型初始化 / 调用异常 → `model_init_error` / `llm_call_error`（错误信息脱敏）；坏 JSON / 顶层非对象 → `bad_json` / `bad_root`；两版文案缺失或为空 → `bad_copy`。业务失败都返回结构化 `CopyResult`，不向 ReAct 循环抛异常；`KeyboardInterrupt` / `SystemExit` 原样抛出。

   **已知边界**：文案质量、语气、长度由模型决定，代码只校验结构、内部词和价格；内部词是保守关键词匹配，可能误杀「低成本快乐」这类普通表达；价格校验只覆盖常见数字写法，中文数字、模糊价格、半价 / 买一送一等语义优惠无法完全识别；选场只按风险优先级，不做同晚多场互相抢客或利润全局优化；不做玩家直连、不自动发送微信、不自动发布。实现见 `app/tools/copywriter.py`。

   **运行方式**：`scripts/08_copywriter_demo.py` 默认使用脚本内 FakeLLM，离线、不需要 API key；加 `--live` 才调用真实 DeepSeek，且只打印「检测到 DEEPSEEK_API_KEY：是/否」，不打印 key 的值。`tests/test_copywriter.py` 覆盖选场回退、双层白名单、`internal_leak` / `price_mismatch`、配置与模型异常、坏 JSON 等场景，全部离线。
8. **剧本库 / 缩写别名工具** —— ✅ **已实现**：把「剧本本身的信息」从场次表里独立出来。场次回答什么时候开、谁约了；剧本库回答全名、别名 / 缩写、类型、几人本、时长、参考原价和备注。数据在 `data/scripts.xlsx`（私有库），网页版每个邮箱各有一份；另有全店共享的**公共剧本库**（`data/public_scripts.xlsx` / `public_scripts` 表），见本节末尾。

   **允许残缺，先存后补**：只要店长给出全名或至少一个别名，`upsert` 就会先落一条档案，并返回 `profile_status`（`待确认名称` / `待补全` / `已完整`）、`missing_fields` 与 `next_question`。必填字段是全名、类型、适合人数、时长；别名、参考价、备注可选。下一轮带上返回的 `script_id` 继续补，空值不会覆盖已有值，已保存的字段不会被重复追问。人数 / 时长 / 价格接受 `8人`、`4小时`、`168元` 这类写法并做范围校验；无法识别的值进入 `invalid_fields`，不会为了凑完整而编造。

   **缩写怎么处理**：`search` 合并三类本地来源——私有剧本库的 `script_name` / `aliases`、公共剧本库（只读，`pub-` 前缀 id），以及 `sessions.xlsx` 里出现过的剧本名。场次历史只读，不会自动写回剧本库。匹配只做空白折叠、大小写归一和包含匹配，**不做拼音、模糊匹配**；本地查不到时，Agent 可以根据自己的知识提出「我猜是《…》」并等店长确认，也可以走第 12 个工具 `web_search` 查公开信息（标注「网络信息非官方」）；工具 8 本身不联网、不猜名字，也不会把模型候选静默写入档案。

   **公共库只读、私有遮蔽**：公共库对商家只读——`upsert` 永远只写私有库；`pub-` 开头的 id 传入 upsert 直接拒绝（`public_readonly`）。同名（`name_key`）时私有行遮蔽公共行，私有条目优先生效并有 warning 提示。场次**永不指向公共行**：命中公共库时预填类型 / 人数 / 价格，但不写 `script_id`，结果标 `script_source=公共库`。公共库数据来源与维护方式见 4.7（策展种子 + 运营方离线采集导入，应用运行时不带爬虫）。

   **数据与边界**：列包括 `script_id`（全名哈希或 `draft-xxxxxxxx`，公共行为 `pub-` 前缀）、`script_name`、`aliases`（`/`、逗号、分号分隔）、`script_type`、`total_seats`、`duration_minutes`、`list_price`、`notes`、`source_text`、`created_at`、`updated_at`（公共行另有 `origin` 来源标注与 `imported_at`）。剧本库只描述剧本，不承担排期；具体场次仍由工具 6 写入。当前没有独立的剧本库管理页面，入口是 Agent 工具；不做图片 / OCR。实现见 `app/tools/scripts.py` 与 `app/tools/public_library.py`，离线测试见 `tests/test_scripts.py`、`tests/test_public_library.py` 与 `tests/test_agent.py::TestScriptTool`。
9. **资源管理工具** —— ✅ **已实现**：维护房间、DM 与 DM 班次，是自动排期的输入。`setup_status` 返回房间数、DM 数，以及容量、技能、班次、剧本时长、阶段模板的缺口清单；`upsert_room` / `upsert_dm` / `set_shifts` 手工建档；`seed_demo` 仅在资源为空且 `confirm=true` 时写入演示资源。房间与 DM 不做物理删除，只停用，历史排期不会悬空。

   **缺字段不猜**：房间容量缺失时不参与自动排房，提示到 `/resources` 补容量；DM 技能为空按「未配置」放行并 warning；班次为空按全天可用并 warning。导入使用固定模板 `rooms.csv` / `dms.csv` / `shifts.csv`（也接受 XLSX），重复导入按名称或 ID 幂等合并。写 action 在只读模式下统一返回 `read_only`。实现见 `app/tools/resources.py`。
10. **资源排期工具** —— ✅ **已实现**：在场次时间固定的前提下，为每场分配一个房间和一个主 DM。硬约束包括房间启用与容量、DM 启用 / 技能 / 班次 / 每日上限、房间与 DM 的占用区间不重叠、翻台与休息缓冲；目标按「先最大化可安排场次数，再最大化预计贡献」排序，小规模用分支限界求到最优，超过节点上限时如实返回 `optimal=false`，不把有界搜索结果说成最优。

    **只分配，不改场**：不修改 `sessions.start_time`，不自动合并、取消或候补场次；已有手工 assignment 默认锁定，不被静默覆盖。排不开时返回原因码（容量、技能、班次、资源重叠等）和最多 3 条建议（换房、换 DM、改期、取消 / 候补、补资源）。只有换房 / 换 DM 会在店长选择后写库；改期 / 取消只给确认文案，不自动执行。收益估值使用默认上座率假设，只作内部参考，不进入营销文案。实现见 `app/tools/scheduling.py` 与 `app/tools/assignment.py`。
11. **DM 场控只读查询工具** —— ✅ **已实现**：`status` 查当前阶段、序号、剩余 / 超时、暂停、模式与硬件降级状态；`list_today` 列当日场控；`report` 在结束后返回基础汇总，进行中返回 `run_active`。不返回 DM 专用的 `cue_text`，也不返回玩家信息。

    **没有推进能力**：工具不存在 `start` / `next` / `pause` / `finish` / 改模式 action，店长只能看，不能远程操作；Agent 提示词也禁止建议「到点自动下一幕」。阶段推进只能由 DM 在 DM 端显式请求，服务端没有定时推进任务。场控状态机、场控码与硬件降级见 4.6。实现见 `app/tools/dm_control.py`。

12. **联网搜索工具（web_search）** —— ✅ **已实现**：本地剧本库未命中时查询公开网络信息（发行信息 / 类型 / 测评动态），可插拔 Provider（默认 Tavily，密钥只放本地 `.env` / Render Secret，由使用者自行填写）。**未配置 `WEB_SEARCH_API_KEY` 时工具恒注册但返回 `not_configured`**，提示改用本地剧本库——不因缺 key 报错，也不伪装有结果。

    **三道服务端硬闸门（在调用 Provider 之前执行，被拒请求零成本）**：(a) 入参校验——长度 2–120 字、拒绝名单（天气 / 股票 / 时政 / 明星等无关话题）、正向信号要求（剧本域关键词 / 《》书名号 / 「XX剧本杀」形态），不满足即 `query_rejected`；(b) 租户日配额 `WEB_SEARCH_TENANT_DAILY_LIMIT`（默认 10，夹取 0–100）；(c) 全局日配额 `WEB_SEARCH_GLOBAL_DAILY_LIMIT`（默认 30，夹取 0–500）。默认配置 30 次/天 × 30 天 ≈ 900 ≤ Tavily Free 档 1,000 credits/月，结构上不触发付费超量（2026-10-22 从 tavily.com/pricing 核实）。计数落 `search_usage_daily` 表（Excel 模式 JSON sidecar），仅在 Provider 调用成功后递增；失败 / 缓存命中不计数。

    **结果使用边界**：最多 3 条，必须标注「网络信息非官方」并附来源链接；**绝不根据搜索结果自动写入剧本库**——需店长确认全名后走 `script_library(upsert)`；禁查玩家隐私、商家后台、微信内容。查询放行 / 拒绝写一行不含 PII 的审计日志（query 截断 40 字），key 永不入日志。实现见 `app/integrations/web_search.py`，离线测试见 `tests/test_web_search.py`（全部 mock，不产生真实调用）。

## 四、五大业务场景与 ReAct Agent（已实现）

以下流程由**同一套 ReAct Agent** 驱动。具体工具链由模型根据系统提示词选择，不是代码级固定顺序。Agent 已组装完成，A/B/C/D/E 五条工具链在**脚本模型**下离线跑通（`scripts/09_agent_demo.py`）；脚本模型按固定顺序发起工具调用，只验证装配、参数传递、读写与结果回填，不代表真实模型一定会这样选工具。E 场景的剧本库工具、提示词路由和离线演示已实现，2026-10-22 已用真实模型单次跑通（三轮，见 4.3）。

1. **场景 A：解析朋友圈 / 微信群拼场广告**
   店长复制微信内拼车文案发给 Agent，自动抽取、去重、过滤过期场次，标记高危场次，自动计算推荐折扣，保存入库，并给出运营建议（由 LLM 综合各工具结果生成，非独立工具产出）。
   工具链：`extract_message → dedupe_sessions → store_data(upsert_sessions) → assess_risk + suggest_pricing`。

2. **场景 B：录入玩家预约**
   店长转发玩家预约消息，Agent 读取现有场次、更新空位数量；空位填满时自动取消高危标记，不再推荐折扣。
   工具链：`extract_message → store_data(get_session / query_sessions) → store_data(record_booking)`。抽取给出的 `candidate_session_id` 只是候选，必须先查库确认场次存在才能写入。

3. **场景 C：玩家临时跳车**
   店长转发玩家取消预约消息，Agent 扣减预约人数、增加空位，**重新执行风险评估 + 定价计算**，动态更新运营策略。
   工具链：`extract_message → store_data(cancel_booking) → assess_risk + suggest_pricing`。

4. **场景 D：自动生成私域营销推文**
   店长下达指令，Agent 查询当前全部可拼场次，重点高亮**高危场次**，生成可直接复制的微信群 / 朋友圈宣传文案。
   工具链：只调用 `write_marketing_copy`。选场、双层脱敏与生成后校验都在工具内部完成，Agent 原样转述结果，不自行改写或补写文案。

5. **场景 E：说缩写 / 加剧本，信息不全先存草稿**
   店长说「NL 是哪个本」或「帮我加个剧本，叫年轮」，Agent 先查本地剧本库（私有 + 公共只读层）与场次历史；查到就列出全名与缺失字段，本地查不到就先用 `web_search` 查一次公开信息（标注「网络信息非官方」）或标明「我猜是《…》」并等店长确认。店长确认或直接给出信息后，Agent 先保存已知字段，再根据 `missing_fields` 继续补类型、人数、时长；已保存的字段不会重复问。
   工具链：`script_library(search) →（本地没有时 web_search 一次）→ (店长确认) → script_library(upsert)`。缩写候选由模型提出，**确认前不写入**；搜索结果与模型联想都必须等店长确认全名。若只是要排一场具体场次，仍走场景 A 的场次写入，剧本档案和排期是两件事。

### 4.1 Agent 组装

实现见 `app/agent.py`。

- **工具注册**：`build_store_tools()` 用 `@tool` 把 12 个业务函数包装成 12 个 Agent 工具，统一返回 JSON 字符串信封 `{"ok", "data", "warnings", "error_code", "error"}`。包装层只做参数适配、依赖注入（Store 或 Excel 路径、当前时间、可注入的 LLM / 搜索 Provider）与结果序列化，**不重新实现业务规则**；确定性逻辑仍在 `app/tools/`。
- **图与记忆**：`build_agent()` 调用 `langchain.agents.create_agent`，接入 `InMemorySaver` checkpointer。`run_turn()` 以 `thread_id` 区分会话：同一 thread 保留多轮工具调用上下文，不同 thread 相互隔离。会话记忆只保存对话，不作为场次数据源（见 二）。
- **路由**：`SYSTEM_PROMPT` 写明了 A–N 路由的推荐工具链、写操作触发条件与失败转述规则，其中 F–K 覆盖资源初始化、场次增量录入与排班、资源不足、点评导入和 DM 场控查询，N 覆盖新公开信息 / 网络查询（本地库未命中 → web_search 一次 → 标注非官方 → 店长确认全名后才写库）。实际是否按推荐链执行由模型决定；离线脚本模型不能证明真实模型的调度能力。2026-10-21 的真实模型单次验证覆盖了 F（资源缺口）、G（场次录入 + 自动分配）、H（资源不足）、K（场控只读）四条路由，均按推荐链执行（见 4.3）；2026-10-22 的真实模型单次验证覆盖了场景 E（剧本缩写 / 残缺档案），同样按推荐链执行（见 4.3）。
- **写操作边界**：只有 `store_data` 的写 action（`upsert_sessions` / `record_booking` / `cancel_booking` / `create_session` / `update_session`）、`script_library` 的 `upsert`、`manage_resources` 的资源写 action，以及 `schedule_sessions` 的 `auto_assign` / `apply` / `resolve` 会写存储，且只在店长明确要求录入、预约、跳车、加剧本、补资源或确认排期时执行；`dm_control`、`web_search` 及另外 6 个工具只读。业务失败返回 `ok=false`，未预期异常兜底为 `internal_error`，均不向 ReAct 循环抛异常（`KeyboardInterrupt` / `SystemExit` 除外）。`--read-only` 在工具层硬拦截上述全部写动作，是代码边界而非提示词约定。Excel 模式下各表文件各自原子替换，跨文件写入之间没有事务；数据库模式下每次表写入在事务内完成，跨表操作仍不是单一事务。真实数据请保留备份。
- **运行方式**：`scripts/09_agent_demo.py` 默认离线，使用临时目录中的合成数据依次跑 A–E（E 为两轮：缩写查询、剧本档案补全），不写真实 `data/`；`--live` 才调用真实 DeepSeek，先跑 A/B/C/D，再跑新增路由四轮（资源缺口 / 场次录入 + 自动分配 / 排期缺口 / DM 场控只读），且只打印「检测到 DEEPSEEK_API_KEY：是/否」，不打印 key 的值。交互式连续使用见 4.2。

### 4.2 店长交互控制台（已实现）

实现见 `app/cli.py`，入口为 `python -m app.cli` 或 `python -m app`。Agent 只构建一次，同一进程内连续对话；`scripts/01–09` 仍是单向演示。

**参数**

| 参数 | 默认 | 说明 |
| --- | --- | --- |
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
- 控制台命令、会话隔离、错误恢复、轨迹渲染与只读闸门由 `tests/test_cli.py` 离线覆盖。已于 2026-10-19 用真实 DeepSeek 做一次交互观察，工具轨迹见 4.3；单次观察不代表稳定能力。

### 4.3 真实模型验证记录

**2026-10-19，模型 `deepseek:deepseek-chat`，单次运行。**

1. `PYTHONUTF8=1 python scripts/09_agent_demo.py --live`（临时目录，退出码 0）：
   - 场景 A：`extract_message → dedupe_sessions → store_data(upsert_sessions) → assess_risk → suggest_pricing`，全部 `ok=true`；
   - 场景 B：`extract_message → store_data(get_session) → store_data(record_booking)`，全部 `ok=true`；
   - 场景 C：`extract_message → store_data(get_session) → store_data(cancel_booking) → assess_risk → suggest_pricing`，全部 `ok=true`；
   - 场景 D：`write_marketing_copy`，`ok=true`，返回社群短版与朋友圈长版。
2. 控制台（临时目录 + `--store 迷雾剧场 --trace`）连续输入 4 条消息，退出码 0：
   - 拼场广告：与场景 A 相同的工具链；
   - “现在哪场最危险？”：`assess_risk`；
   - “帮我写今晚的推广文案”：`write_marketing_copy`；
   - `/status`：消息数 19，会话记忆正常。

**观察**

- 本次运行中模型均按系统提示词的推荐链选择工具；场景 B / C 在写入前先调用 `get_session` 确认场次存在，符合“先查库再写”的要求。
- 场景 A 中模型注意到广告写“还差 4 人”但系统已约 0 人，主动指出数据不一致并要求补录，没有编造预约记录。
- 四个场景与控制台三轮工具调用全部返回 `ok=true`，没有出现 `error_code`。

**限制**

- 这是单次观察，只证明当天这次调用可用，不证明模型稳定选对工具或答复正确。
- 演示与控制台均使用临时目录；真实 `data/` 下的文件 mtime 前后一致，未被改写。该次验证早于剧本库功能，未覆盖 `scripts.xlsx`。
- 日志中未出现 `sk-...` 形态的密钥；验证过程只打印「检测到 DEEPSEEK_API_KEY：是/否」。

**2026-10-21，模型 `deepseek:deepseek-chat`，单次运行。**

1. `PYTHONUTF8=1 python scripts/09_agent_demo.py --live`（临时目录，退出码 0）：场景 A–D 与 2026-10-19 记录一致，五条工具链全部 `ok=true`。场景 A 的自动排期返回 `resources_not_configured`（该轮还没建房间 / DM），模型如实说明“场次已保存但未分配”，并建议先建档，没有编造分配结果。
2. 同一次运行接着跑新增路由四轮（脚本内 `_seed_newtools` 载入演示资源、剧本档案与阶段模板）：
   - “房间里和 DM 还缺什么” → `manage_resources(action="setup_status")`，`ok=true`，按缺口清单逐项回答，并指出演示数据、时薪缺失会影响成本分析；
   - “记一场 10 月 23 日 20:00《雾隐山庄》” → `extract_message` 后先多调了一次 `store_data(get_session)`（返回 `session_not_found`），模型没有被带偏，继续 `script_library(search)` + 当时的场次草稿工具（草稿机制已于后续迭代移除，现为 `store_data(create_session)` 硬必填齐全直接建场，见 4.5）；硬必填齐全后自动建场，`auto_assign=assigned`（A 房 / 小北），并说明人数 6 / 价格 168 / 类型「推理」来自剧本档案而非店长原话；
   - “这周有没有排不开的场次？” → `schedule_sessions(list)` + `manage_resources(setup_status)` + `schedule_sessions(plan)`，识别出 10-20 22:00《年轮》的 `dm_unavailable`，列出改到 20:00、给 DM 补班次、让同晚低约场次改期三类选项，明确不自动执行，并指出三个剧本缺时长、按默认 240 分钟估算；
   - “现在场控演到哪一幕了？” → `dm_control(action="status")`，无进行中场次时如实说明为空。
3. 观察：八轮工具调用可用，新增四轮与提示词路由 F / G / H / K 一致；场次录入是“先查库再写”，那次失败的 `get_session` 属于多余的投机查询，不影响结果；排期答复给出了原因码与选项，没有替店长做决定。
4. 本轮还暴露出演示跨夜班次只到次日 02:00：22:00 开场的 4 小时本加 30 分钟 DM 缓冲到 02:30，会被判 `dm_unavailable`。已把演示跨夜班次延长到次日 04:00，并在 `tests/test_resources.py::test_demo_shifts_cover_late_session` 里加了 22:00 场的回归断言。

**限制**：同为单次样本，不构成稳定性结论；`--live` 只覆盖 CLI 聊天链路，数据写临时目录，真实 `data/` 的 mtime 前后一致；网页端与 Flutter 端未做真实模型联调。

**2026-10-22，模型 `deepseek:deepseek-chat`，场景 E 单次运行（临时驱动脚本，三轮，数据写临时目录）。**

1. 「WY 是哪个本？」→ 一次 `script_library(action="search", query="WY")`，命中别名返回《雾隐村》完整档案（恐怖 / 6 人 / 240 分钟 / 168 元），`ok=true`；答复里主动提示可按增量录入排队次。
2. 「帮我加个剧本《年轮》，8 人本」→ 模型先 `script_library(search)` 查重（离线脚本模型是直接 upsert），未命中后**没有把猜测直接写库**，而是先请店长确认全名；本轮未写任何数据。
3. 确认「全名就叫年轮」后（同会话线程）→ `script_library(action="upsert", script_name="年轮", total_seats="8人")`，`ok=true`，返回 `待补全`（缺 script_type、duration_minutes）；模型只追问类型一项、明说时长可后补，符合「一次只问一个」。落库核对：临时目录 `scripts.xlsx` 中存在《年轮》行（8 人、时长空）。

**观察**：三轮均按路由 E 的推荐链选择工具（先查别名 → 未命中先确认 → 存草稿再追问）；轮 2 的「先查重、先确认、不写猜测」比离线脚本模型更保守，属于路由允许的合理行为。

**限制（2026-10-22）**：仍是单次样本，不构成稳定性结论；只覆盖 CLI 链路与 Excel 临时目录；未覆盖网页端 / Flutter 端的真实模型联调。

**2026-10-25，模型 `deepseek:deepseek-chat`，单次运行（`--live` 新增第 9 轮观察）。**

1. `DATABASE_URL= PYTHONUTF8=1 python scripts/09_agent_demo.py --live`（临时目录，退出码 0；只打印「检测到 DEEPSEEK_API_KEY：是/否」）：
   - 场景 A–D 的工具链与 10-19 / 10-21 记录一致，全部 `ok=true`（A：`extract_message → dedupe_sessions → store_data(upsert_sessions) → assess_risk → suggest_pricing`；B/C：先 `get_session` 再写入；D：`write_marketing_copy`）。
   - 新增四轮 F / G / H / K：`manage_resources(setup_status)`；`script_library(search)` → `store_data(create_session)`；`schedule_sessions(list)` + `manage_resources(setup_status)` + `schedule_sessions(plan)`（默认 240 分钟估算仍在答复里主动提示）；`dm_control(status)`，全部 `ok=true`。
2. 脚本新增第 9 轮（只为观察，不改产品语义）：先写入一场 12 人《千灯愿》10-24 19:00（演示房间最大 10 人，必然排不上），再说「把没排上的场次补一下」。模型调用 `schedule_sessions(list)` → `schedule_sessions(auto_assign, session_ids=[…])` → `manage_resources(list_rooms)`，三个都 `ok=true`；答复说明排不上的原因是「三间房最大 10 人，装不下 12 人本」，给出补房 / 改人数 / 先不排三个选项并请店长决定。跑完后核对临时目录的 assignments 表，该场没有任何分配行写入。

**观察**

- 排期缺口（第 7 轮）与补齐（第 9 轮）都遵循「先读后答」，在没有可用资源时没有编造分配。
- 第 9 轮暴露出一条与 Web 端不一致的路径：Agent 的 `schedule_sessions(action="auto_assign")` 会**直接写库**，没有 Web 看板的「预览 → 逐条勾选 → 应用」审核步骤。这次因为房间放不下，实际什么都没写；但只要资源能排上，模型一句话就会落库。这是已知差异，本轮只记录、未改语义（统一需要新增参数与提示词，属于功能变更）。

**限制（2026-10-25）**：单次样本，不构成稳定性结论；只覆盖 CLI 链路与 Excel 临时目录，真实 `data/` 的 mtime 前后一致；网页端与 Flutter 端的真实模型联调仍未做。

**已知边界**：离线 demo 与 CLI 测试使用脚本模型，不验证真实模型的工具选择与参数填写；Agent 会按店长明确指令写 Excel，测试与 demo 使用临时目录，真实运行会写 `data/`；工具 5 返回的内部财务列只供店长分析，对外文案仍由工具 7 的生成后校验兜底；不做玩家直连、不自动发送微信、不实现支付履约。

### 4.4 网页版：邮件审批与限时访问（代码已实现，尚未公网部署）

**状态说明**：网页版代码、页面、测试、Dockerfile 与 `render.yaml` 已完成，可在本地启动并跑通「申请 → 开发者邮件审批 → 8 小时访问 → 到期重新申请」全流程。存储层已支持数据库后端：业务数据、审批记录与每日用量可存入 Neon Postgres；本地把 `WEB_STORAGE_BACKEND` 设为 `database` 且不填 `DATABASE_URL` 时使用 `data/web.sqlite3`，完整链路由离线测试覆盖。开发环境不设置时仍默认 `excel`，与旧演示行为一致。**Neon 项目与 schema 已创建，真实 Neon 的本机 HTTP 链路已验收；当前没有公网实例，生产业务数据尚未正式导入**。实际部署需要把代码推到 GitHub，并在 Render 配置环境变量（步骤见下方）。本节不使用「已上线」表述。2026-10-20 已在本机用真实 Resend 向 `OWNER_EMAIL` 成功发送并收到审批邮件；这只验证了「Resend 账号本人收件」这一条路径，未验证其他收件人，也未验证公网回调。

**访问流程**

1. 用户打开 `/login`，填写邮箱和称呼。邮箱会规范化为小写；称呼可选。
2. 服务端创建 `pending` 申请，并只向 `OWNER_EMAIL` 发送审批邮件。邮件包含申请编号、称呼、邮箱、申请时间和审批链接，不包含对话内容或其他用户数据。
3. 开发者打开 `/approve/<token>`。GET 只展示申请，必须点「批准」或「拒绝」才会 POST；避免邮件预览器自动批准。
4. 批准后获得 `WEB_ACCESS_HOURS`（默认 8）小时访问：
   - 用户停留在 `/pending` 时每 3 秒轮询，批准后自动解锁；
   - 审批页同时显示备用链接 `/unlock?token=...`，关闭页面后可由开发者转发；备用链接在 8 小时内可重复打开，签发的会话到期时间相同。
5. 8 小时后访问失效，接口返回 401，页面跳转 `/login?expired=1`；用户重新提交申请，开发者重新审批。
6. 审批链接默认 24 小时有效且只能处理一次。轮换 `WEB_SESSION_SECRET` 会让所有已签发会话立即失效。

本地开发用 `EMAIL_BACKEND=console`，审批链接打印在服务端控制台，不联网。生产使用 Resend HTTPS API（`EMAIL_BACKEND=resend`），需要 `RESEND_API_KEY`。使用测试发件人 `onboarding@resend.dev` 时，`OWNER_EMAIL` 必须与 Resend 账号注册邮箱一致；否则 Resend 会拒绝发送。验证自有域名后可把 `EMAIL_FROM` 改为该域名。

**没有自有域名也可以上线当前版本**：审批邮件只发给 `OWNER_EMAIL`，而 `onboarding@resend.dev` 允许发给 Resend 账号本人，所以单店长审批流程不受影响。只有以后需要给其他店长、员工或玩家发邮件时，才必须验证自己的域名。使用 QQ 邮箱收信时，建议把 `onboarding@resend.dev` 加入白名单或标记为非垃圾邮件。

**每邮箱数据隔离**

- 邮箱经 SHA-256 后取前 16 位作为租户 ID（`tenant_id = email_hash[:16]`）。数据库模式下，场次、预约、剧本都以 `tenant_id` 分区，所有查询由 `SqlStore` 强制带上租户条件；`tenants.email_hash` 保存完整 64 位哈希并加唯一约束。
- 同一邮箱重新获批进入同一租户；不同邮箱互相不可见、不可写。请求体不接受邮箱、uid 或文件路径，身份只来自签名 Session。
- 如果两个不同完整哈希落到同一 16 位前缀，系统拒绝创建或访问该租户并报错，不共用数据（fail closed）。
- 首次进入自动播种 4 个未来场次、2 条预约和 4 个同名剧本档案（演示时长是合成值），并尝试写入 3 个演示房间、3 个 DM、班次、阶段模板和演示分配；资源或模板播种失败不阻塞登录，可稍后在 `/resources` / `/stages` 手工补。数据库用 `tenants.seeded_at` 标记，播种在租户写锁内完成，只有一次；已有数据绝不覆盖。
- Excel 兼容模式（`WEB_STORAGE_BACKEND=excel`）仍使用 `WEB_DATA_ROOT/users/<hash>/sessions.xlsx`、`bookings.xlsx` 与 `scripts.xlsx`，以 `.seeded` 文件保证幂等。该模式不再作为生产默认。
- 同一用户的轮次由该用户锁串行执行；数据库模式下同一租户的写入再用 Postgres 事务级 advisory lock 串行（SQLite 用进程内锁）。不用 `SELECT ... FOR UPDATE`，是因为它会和子表的 FK `KEY SHARE` 锁互相等待。单进程部署仍是硬约束（原因见下方）。

**安全上限（8 小时内不限轮数，但不是无上限）**

| 配置 | 默认 | 作用 |
| --- | ---: | --- |
| `WEB_ACCESS_HOURS` | 8 | 批准后的访问时长 |
| `WEB_APPROVAL_TTL_HOURS` | 24 | 审批链接有效期 |
| `WEB_MAX_INPUT_CHARS` | 2000 | 单条消息长度 |
| `WEB_MAX_BODY_BYTES` | 32768 | 请求体上限，超出 413 |
| `WEB_MAX_HISTORY_MESSAGES` | 100 | 单会话消息数；超出 409，需新会话 |
| `WEB_MAX_HISTORY_CHARS` | 50000 | 单会话历史字符上限 |
| `WEB_MAX_CONCURRENT_TURNS` | 3 | 全站同时执行轮数 |
| `WEB_MAX_TURNS_GLOBAL_PER_DAY` | 300 | 全站每日硬上限，按 UTC+8 跨日重置 |
| `WEB_MODEL_MAX_TOKENS` | 1500 | 单次模型输出上限 |
| `WEB_MODEL_TIMEOUT_SECONDS` | 60 | 单次模型调用超时 |
| `WEB_REQUEST_COOLDOWN_MINUTES` | 10 | 同一邮箱申请冷却 |
| `WEB_REQUESTS_PER_IP_PER_HOUR` | 5 | 每 IP 每小时申请上限 |
| `WEB_REQUESTS_GLOBAL_PER_DAY` | 50 | 全站每日申请上限 |
| `WEB_MAX_IMPORT_BYTES` | 2097152 | 渠道单 / 资源导入上传上限（2 MB），普通请求仍受 `WEB_MAX_BODY_BYTES` 限制 |
| `DM_CODE_TTL_MINUTES` | 30 | 一次性场控码有效期 |
| `DM_TOKEN_HOURS` | 12 | DM run-scoped token 有效期 |
| `HARDWARE_TIMEOUT_SECONDS` | 2 | 硬件 Skill 调用超时；失败自动降级为文字提示 |

只有真正进入模型执行的轮次计入全站每日上限；未登录、CSRF 失败、输入超长、历史超长、并发已满、额度已满都不计数。模型调用失败或超时不退还计数，避免靠制造失败绕过总闸。全站计数存数据库（`database` 后端）或旧 SQLite 状态库（`excel` 后端）；数据库不可用时 fail closed（503），不放行模型。

审批链接和备用链接的 token 在 URL 中，因此应用关闭了 Uvicorn 访问日志（`--no-access-log`）；应用日志只记录申请编号、邮箱哈希前缀和异常类型，不记录 token、消息正文或 key。反向代理/平台边缘日志是否记录完整 URL 取决于平台配置，部署后可在平台侧确认或关闭路径日志。

**本地启动**

```bash
cp .env.example .env
# 编辑 .env：DEEPSEEK_API_KEY、WEB_SESSION_SECRET（至少 32 字符）、OWNER_EMAIL
# 本地保持 EMAIL_BACKEND=console
# 开发环境默认 WEB_STORAGE_BACKEND=excel；改成 database 且不填 DATABASE_URL
# 即可用 data/web.sqlite3 验证数据库链路（重启后数据仍在）
python -m app.web.main
```

打开 `http://127.0.0.1:8000/login` 提交申请，在启动终端找到 `/approve/...` 链接并打开，批准后返回用户页面等待自动解锁。发送聊天消息会真实调用 DeepSeek 并消耗 token。

如果提交后邮箱没有收到审批邮件：先确认 `.env` 的 `EMAIL_BACKEND`。默认 `console` 模式**不会联网发信**，审批链接只打印在运行服务端的终端里（搜索 `[本地邮件]` 或 `/approve/`）；审批邮件的收件人是 `OWNER_EMAIL`，不是申请者邮箱。要接收真实邮件，需按下方部署步骤配置 `EMAIL_BACKEND=resend`、`RESEND_API_KEY` 和 `EMAIL_FROM`，然后重启服务。

2026-10-20 已在本机用临时目录做 uvicorn 冒烟：`/healthz`、`/login`、`/static/app.css` 均返回 200。随后把本机 `.env` 切到 `EMAIL_BACKEND=resend`，提交申请后 Resend API 接受了发送请求，`OWNER_EMAIL` 收到了审批邮件；该验证未调用模型，也未做公网部署。若收件箱没有看到邮件，先检查垃圾邮件和 `EMAIL_BACKEND`，再检查 `OWNER_EMAIL` 是否就是 Resend 账号注册邮箱。

同日完成真实 Neon + 真实 uvicorn 的 HTTP 验收（`EMAIL_BACKEND=console`，未调用模型）：`/healthz` 200 → 提交申请 303 → 待审批页 200 → 控制台审批链接 → 批准页 200 → 批准 200 → 访问状态 `approved/claimable` → 领取访问 200 → 看板 200 → `/api/sessions` 返回 4 场。对应租户在 Neon 中为 4 场次、2 条预约、4 个剧本；验收结束后已删除测试邮箱的审批记录与租户。`npm run build` 同时通过 `compileall` 和全量测试。控制台邮件在 Windows GBK 终端下曾因编码失败导致申请被标记为 `email_failed`，现已改为编码失败时降级输出并立即 flush，相关回归测试已加入。

**Neon（本机已完成）**

当前 `.env` 已有 `DATABASE_URL`（池化，主机名带 `-pooler`）和 `DATABASE_URL_UNPOOLED`（直连）两份连接串；两者都包含用户名和密码，**不要提交到仓库、不要贴进聊天**。项目通过 `.neon` 关联 Neon 项目，`neon.ts` 只放分支策略，不包含密钥；`.neon` 已在 `.gitignore` 中。

- **应用运行时用池化 `DATABASE_URL`**：Neon 前面是 PgBouncer 事务模式，适合 Web 的短连接。为兼容事务池，连接参数已设置 `prepare_threshold=None`，关闭 psycopg 的服务端 prepared statement。
- **迁移、建表、导出用直连 `DATABASE_URL_UNPOOLED`**：`python -m app.store.migrate` 会自动优先读它，其次才回退 `DATABASE_URL`。显式传 `--sqlite` 时则只用本地文件，便于离线测试。
- 重新拉取环境变量：`neon env pull`。如果重新初始化：`neon login`，再在项目根运行 `neon init -y --branch production --services postgres`。CLI 版本、组织/项目 ID 以你的 Neon 控制台为准。

**Render（尚未部署，需要你操作）**

1. 生成会话密钥：`python -c "import secrets; print(secrets.token_urlsafe(48))"`。
2. 注册 Resend 并创建 API key；把 `OWNER_EMAIL` 设为 Resend 账号注册邮箱（测试发件人限制见上）。
3. 当前项目目录还不是 git 仓库；执行 `git init`、提交并推送到你自己的 GitHub 仓库。`.env`、`.neon`、`node_modules/`、`data/`、SQLite 文件和 Excel 已在 `.gitignore` 中排除。
4. Render 新建 Blueprint（读取 `render.yaml`）或 Docker Web Service，连接该仓库。需要设置：
   - `DATABASE_URL`（Secret，粘贴 Neon **池化**连接串，主机名带 `-pooler`）
   - `DEEPSEEK_API_KEY`、`RESEND_API_KEY`、`WEB_SESSION_SECRET`（Secret）
   - `OWNER_EMAIL`
   - `PUBLIC_BASE_URL=https://<你的服务>.onrender.com`
   - `WEB_ENV=production`
   - `EMAIL_BACKEND=resend`
   - `EMAIL_FROM=onboarding@resend.dev`（未验证域名时）
   - `WEB_STORAGE_BACKEND=database`（`render.yaml` 已写好）
5. 应用启动时会对 Neon 执行幂等的 `CREATE TABLE IF NOT EXISTS`（`ensure_schema()`），不需要先手工建表；也可以在本机提前运行 `python -m app.store.migrate --init`。
6. 部署完成后访问 `/healthz` 确认返回 200；再提交一次申请，确认收到审批邮件、批准后自动解锁、能发一条测试消息。首次访问或 Neon 休眠后，冷启动可能多等 1–2 秒。
7. 推送前可先在本机执行 `npm run build`（或 `python -m compileall -q app && python -m pytest -q`）确认全量测试通过。

**把本地 Excel 导入 Neon（可选）**

本地 `.env` 填好 `DATABASE_URL_UNPOOLED`（或只有 `DATABASE_URL`）后，迁移命令会自动读取 `.env`；也可以显式传 `--database-url`。先 dry-run，再正式写入：

```bash
# 本地 data/*.xlsx → local 租户（CLI 演示数据）
python -m app.store.migrate --source data --tenant local --dry-run
python -m app.store.migrate --source data --tenant local

# 旧 Excel Web 用户目录 → 每个目录一个租户（16 位哈希）
python -m app.store.migrate --source data/web_users/users --all --dry-run
python -m app.store.migrate --source data/web_users/users --all

# 旧审批 / 每日用量 SQLite → Neon
python -m app.store.migrate --state data/web_state.sqlite3 --state-only

# 导出某个租户为十二个 xlsx（3 张业务表 + 9 个 sidecar，备份 / 回滚）
python -m app.store.export --tenant local --out backups/local
```

默认按主键幂等合并，重复执行不会产生重复行；`--force` 才会覆盖该租户对应表。每次实际写入都会记录到 `import_runs`。旧目录只有 16 位哈希时，可加 `--state-db data/web_state.sqlite3` 从旧审批记录反查完整邮箱哈希。**正式导入前先备份 `data/`**；迁移不会删除或修改源 Excel。

**免费层与单 worker 限制**

- Render 免费实例的磁盘是临时的，且会休眠；数据库模式下业务数据、审批记录与每日用量都在 Neon，**重启 Render 不会丢**。Excel 兼容模式（`WEB_STORAGE_BACKEND=excel`）仍依赖本地磁盘，重启会重置工作区与审批记录，旧的审批 / 备用链接随之失效。
- Neon 免费版：约 1 GB 存储、100 CU-hours/月；无访问约 5 分钟后自动休眠，冷启动约 1–2 秒；没有 SLA。按单店几千场次、上万条预约估算，数据量远低于 1 GB。额度耗尽后写入可能失败；定期用 `app.store.export` 导出备份。
- 只能运行 **1 个 Uvicorn worker**。`InMemorySaver` 是进程内存储，多 worker 会导致同一用户的会话记忆分裂；数据库锁解决了业务数据并发写，但没有解决对话记忆。`Dockerfile` 已固定 `--workers 1`，不要在部署平台横向扩实例。
- **对话记忆不持久**：Neon 只存场次、预约、剧本、审批与用量；重启进程后同一 `thread_id` 的聊天历史仍是空的。需要持久对话记忆需另接 checkpointer，本期不做。
- 容器以非 root 的 `app` 用户运行，`$PORT` 由平台注入；`/healthz` 会执行数据库 `SELECT 1`，数据库不可用时返回 503，不会静默回退 Excel。
- 本仓库已在本机完成真实 Neon 的 schema 创建、本机 uvicorn HTTP 端到端验收和 `npm run build`；**尚未执行公网 Docker 构建或 Render 部署**（开发机未安装 Docker），也尚未导入生产业务数据。部署结果需以 Render 构建日志和公网 `/healthz` 为准。

**本轮不做**：玩家直连对话、微信自动抓取或自动发消息、支付与订单履约、管理员后台、多 worker、token 级流式输出、Markdown/HTML 富文本渲染。网页版仍是店长端私域运营辅助工具。

### 4.5 资源排期、增量录入与渠道单（已实现）

**资源与排期**：Web 新增 `/resources`（房间、DM、班次、缺口清单、演示资源、CSV/XLSX 导入）与 `/schedule`（看板置顶：时间看板 / 状态看板 + 建场卡片，见 4.8、4.9）。场次录入**默认不填房间 / DM**：店长只填剧本、开场时间，人数与价格可后补；房间与 DM 由建场后的自动分配决定，也可在建场卡片里手动指定（失败不回滚场次，返回警告并保留自动分配结果）。没有资源时返回 `resources_not_configured`，不阻塞录入；发生冲突时场次照常保存，返回换房、换 DM、改期、取消 / 候补、先不排等选项，店长选择后才写排期。排期只写 `session_assignments`，**不改开场时间、不合并、不取消场次**。

自动分配使用固定场次的约束搜索：房间容量、DM 技能与班次、每日上限、翻台与休息缓冲、已有手工分配锁定。小规模求到最优；触发搜索节点上限时返回 `optimal=false` 和 `search_truncated`，页面与 Agent 都不能把它说成「最优」。收益估值按当前预约与默认上座率估算，只供内部排序，不是收入承诺。实现见 `app/tools/scheduling.py`、`app/tools/assignment.py` 和 `app/web/operations.py`。

**建场逻辑（没有草稿）**：Agent 对话的 `store_data(create_session)` 与 Web 的 `POST /api/sessions`、App 的 `POST /api/app/sessions`（平铺字段或 `{"fields": {...}}` 包装）共用 `app/tools/session_intake.py`。硬必填只有「剧本全名」和「开场时间」；两项齐全就直接建场并自动排期（`status=promoted`，触发 `auto_assign`），人数、价格、类型、时长能从剧本库推断就推断，推断不到也不阻塞。缺任意一项硬必填时只在对话里追问最关键的一项，**不落任何数据**——Web / App 端返回 400 `incomplete_session` + 追问文案；剧本名命中多个候选时停在 `needs_confirmation`（同样不落数据），由店长点选候选（「就开《XX》」重发文本按钮）或补全信息后再建；唯一候选自动确认并回显规范全名。`room_id` / `dm_id` 在 Web 与 App 建场时是**可选**字段（Agent 对话工具仍不接受这两个字段）：不填就走自动分配；填了则先按原逻辑建场并自动分配，再用手工指定覆盖，覆盖失败不回滚场次、也不吞错误——响应里返回 `manual_assign.status="failed"` 和「已建场，但手动指定未生效：…；已保留自动分配结果」的警告。

已保存场次只改人数 / 价格 / 类型时原地更新，不换 `session_id`；改门店、剧本或开场时间会重算 `session_id`，并在同一个写锁内搬移预约、assignment、渠道单引用和已结束的场控历史。目标 ID 已存在返回 `duplicate_session`，新容量小于已约人数返回 `capacity_exceeded`；该场有 `pending/active/paused` 场控时返回 `run_active`，禁止改场次身份，也不留下半套数据。

**渠道单**：`/imports` 提供快速 +N 占位与大众点评订单 CSV/XLSX 导入。快速占位由前端生成 `client_ref`，服务端用确定性 `booking_id` 去重，重复点击不会重复占座；容量检查与写入在同一个写锁内完成，满员返回 `capacity_exceeded`。点评导入先预览映射、前 10 行脱敏数据、未匹配场次与超容量场次，确认后才写入；按订单号幂等，重复导入返回 `unchanged`，退款 / 取消同步取消对应预约，某场超容量则整组拒绝该场，其他场正常导入。

**系统内部不会超售**；外部渠道仍存在时间差：只有订单导出并导入后，系统才看见这些座位，两次导入之间点评端仍可能接单。界面持续显示 `data_as_of`，超过 2 小时提示数据已过期，**不承诺实时同步**。实现见 `app/tools/intake.py`、`app/integrations/booking_source.py` 与 `app/web/imports.py`。

**页面与安全**：新增 `/resources`、`/schedule`、`/imports`、`/stages`、`/runs`、`/runs/{run_id}`。所有非 GET 请求走 CSRF；页面保持无内联脚本、无内联样式、无外部 CDN；上传上限默认 2 MB，普通请求仍是 32 KB。Web 是桌面后台与兜底，不注册 service worker、不缓存登录态页面。

### 4.6 DM 场控与 Flutter 双角色客户端（代码已实现；Flutter 已通过 analyze/test 并于 2026-10-22 构建 debug APK，未签名、未真机联调）

**阶段模板**：商家在 Web `/stages` 从剧本库已有剧本创建阶段模板，填写阶段名、时长、提前提醒、DM 提示 `cue_text`、到点文案 `prompt_text` 和可选硬件场景 JSON。保存时校验序号连续、时长与提醒范围、JSON 结构；模板版本加一。场控启动时把当前 active 阶段快照进 run，**运行中修改模板不影响本场**，下一场才使用新版本。

**场控状态机**：`pending → active ⇄ paused → completed`，另有 `abandoned`。商家对已有场次生成一次性 8 位场控码（默认 30 分钟、单次使用、库中只存 HMAC），DM 兑换后得到 run-scoped Bearer token（默认 12 小时）。兑换后仍是 `pending`，DM 点「开始场控」才进入第一幕计时。DM 可暂停 / 继续、上一幕 / 下一幕、延长 1–20 分钟、结束（二次确认）；最后一幕点「下一幕」返回 `at_last_stage`，**不会自动结束**。商家只能生成新码、撤销令牌和只读直播，**不能远程推进、暂停或结束**。实现见 `app/tools/dm_control.py`、`app/tools/stages.py`、`app/web/dm.py`。

**倒计时只提醒，绝不推进**：服务端没有 cron、后台线程或 `asyncio.sleep` 推进阶段，`stage_ends_at` 只用于展示和客户端提醒；DM 端到点只发本地通知、标红「已超时」并显示超时秒数，不调用 `next`。测试会把时间冻结并推过 `stage_ends_at`，断言阶段、版本和事件数都不变。App 被杀后重开，用服务端时间戳重建剩余时间，不续跑本地倒计时。

**硬件增值模式**：默认纯软件模式，只推送文字提示。每场可单独切换硬件模式；切换阶段时先提交阶段状态与事件，再调用通用 HTTP `HardwareAdapter` 下发灯光 / 音效场景。硬件超时、连接失败或非 2xx 时，阶段**不回滚、不阻塞**，run 标记 `hardware_degraded`，后续降级为文字提示并记录 `hardware_failed` 事件，DM 可手动「重试硬件」。离线补传的动作不补发硬件指令。`HARDWARE_SKILL_URL` / `HARDWARE_SKILL_TOKEN` 只来自服务端环境变量；未配置时硬件模式返回 `hardware_not_configured`。

**断线与并发**：每个动作带客户端生成的 `client_event_id` 和 `expected_version`；重复事件幂等，版本冲突返回 `stale_state` 与当前 run，客户端以服务端状态重同步。断网时 DM 端把动作写入本地队列（上限 200 条），恢复后按序补传；补传不发送硬件。同一场次最多一个未结束 run，同一 DM 最多一个进行中 run。

**Flutter 双角色客户端**：`mobile_app/` 是同一套 Flutter 工程，商家端覆盖邮箱申请 / 轮询、Agent 对话（含操作按钮）、排期（紧凑列表 / 时间轴双视图 + 场次详情页）、快速占位、点评文件导入、生成场控码和只读场控；DM 端只读本场排期 / 房间 / 阶段模板，负责计时和手动推进，不能创建场次、剧本或资源。token 存 `flutter_secure_storage`，不写日志或 URL；离线队列只存动作与版本，不存玩家信息。**不做微信小程序**：Flutter 不能直接编译为小程序，单独维护第二套 UI 的成本不在本轮范围；Web 兜底页仍可在微信内打开。

> ⚠️ **验证状态**：2026-10-21 本机已安装 Flutter 3.47.7（Dart 3.13.5）与 Android SDK 36 + JDK 17，`mobile_app/` 已执行 `flutter pub get`（依赖解析成功）。`dart analyze .` 现为 **0 issues**；`flutter test` 分日累计：2026-10-21 基线 29 项（local_state / clock / 离线队列，含 `sqflite_common_ffi`）、2026-10-22 增至 61 项（对话按钮映射 + 时间轴纯函数）、2026-10-23 复跑 73 项、2026-10-24 复跑 **74 项全部通过**、10-25 复跑 **82 项全部通过**（新增排期审核流纯函数 8 项）（新增 `timelineDateKey` 营业日分组与 `timelineHourMarks` / `timelineBlockTop` 时间轴渲染策略共 12 项；10-24 再增 `sessionStatusKey` 中文状态映射 4 项；随后场次草稿功能前后端移除，删去 3 项草稿用例）。注意：`flutter analyze` 在含中文的项目路径上会因 LSP 的 UTF-8/Content-Length 解析缺陷崩溃，静态分析结果以 `dart analyze` 为准。2026-10-22 已执行 `flutter build apk --debug` 并成功产出 `app-debug.apk`（约 160 MB、多 ABI、占位包名 `com.example.jbs_operator`）；由于本仓库路径含中文，CMake/Ninja 在编译原生依赖时会损坏输出路径，**debug 构建必须在 ASCII 路径的完整副本上进行**（另见 `mobile_app/README.md` 的 Gradle 镜像与 compileSdk 覆盖说明）。**尚未完成**：未做 release 签名、未生成 IPA（iOS 构建需要 macOS + Xcode）、未上架、未真机联调（DM 端 10 步测试清单见 `mobile_app/README.md`）。因此客户端能力目前只能视为**已通过静态分析与单元测试并已构建 debug 包、未签名未联调**；硬件适配器也**未对接任何具体灯光 / 音效厂商**，真实地址、鉴权与场景字段需要拿到厂商协议后再联调。详见 `mobile_app/README.md`。

### 4.7 公共剧本库、对话按钮与排期双密度（已实现，2026-10-22）

**公共剧本库**：所有商家共享一份只读公共库（`data/public_scripts.xlsx`；数据库模式为 `public_scripts` 表，无 tenant_id），商家各自维护私有库；同名时私有行遮蔽公共行。读路径已接入三处匹配点（`script_library` 的 search/get/list、`session_intake` 的类型/人数/价格预填、`scheduling` 的时长来源）；写路径不变——`upsert` 永远只进私有库，`pub-` 前缀 id 直接拒绝，场次永不指向公共行。种子 `data/public_scripts_seed.csv` 约 10 条真实剧本的事实元数据（`origin=策展`，与演示剧本零重名），空库首次启动自动灌入；清库后重灌是期望行为（守卫就是库本身的空/非空，无额外标记文件）。

- **数据来源是运营方离线动作，应用运行时不带爬虫**：采集脚本 `scripts/fetch_vshang66.py`（开发工具，不进 Docker 镜像、不进服务端定时任务）只抓 vshang66.com 列表页事实元数据（剧名 / 作者工作室 / 状态标签 / 时长 / 人数 / 收录时间 / 详情页 URL 作 origin 溯源），**不抓剧透详情页、不绕过访问控制**；收到 403/429/503 当场停止、robots.txt 变更即停、串行礼貌限速（默认 1.0–1.5 秒）。首次全量约 5800+ 条已导入（2026-10-22）。定位：单店内部运营参考、事实元数据、逐条溯源、非商用转售——最终合规判断是运营方决策，代码只承载边界。
- **导入通道**：`python -m app.store.import_public_scripts <file.json|.csv> [--dry-run]`，按 `name_key` 幂等合并（空值不覆盖、origin 变更追加保留），缺名称 / origin 的行进错误清单不静默丢弃。导入后新开对话才生效（公共帧随 Agent 重建刷新）。
- **定时增量刷新（本机任务计划，可选）**：`scripts/refresh_public_library.bat` 跑增量采集（只翻列表前几页、按收录时间水位线比对）→ 幂等导入 → 追加摘要到 `data/refresh_log.txt`（不含 key / DATABASE_URL）。注册与卸载：

```bat
schtasks /create /tn "jbs_public_library_refresh" /sc DAILY /st 03:30 /tr "C:\Users\<你>\Desktop\job\剧本杀拼场智能运营Agent\scripts\refresh_public_library.bat"
schtasks /delete /tn "jbs_public_library_refresh" /f
```

  定时任务只在本机启用期间有效——本机没开就不会刷新；**不做成服务端常驻任务**，部署到 Render 的公网实例不带爬虫、不带定时采集。

**对话操作按钮**：`/api/app/chat` 与 `/api/chat` 响应在助手回合附带 `choices`（服务端从工具结果白名单提取，≤8 个按钮、label ≤40 字、payload 只含 JSON 标量，绝不拷贝裸信封字段）。只有两类按钮：快捷追问（`quick_reply`，payload 只含预填文本，点击即重发一条消息，不提交任何写）与冲突解决（`resolve`，仅 `switch_room` / `switch_dm` / `keep_unassigned`）。建场结果 `needs_confirmation` 时，剧本候选拆成最多 5 个「就开《XX》」的 quick_reply 按钮，点选即重发文本让 Agent 重新建场。Flutter 端 resolve 按钮直连业务 API（`/api/app/schedule/resolve`，Bearer 鉴权），不经模型；回执以系统气泡展示，不伪造成功文案。**Web 端诚实降级**：`/api/app/*` 需要 Bearer token，Cookie 会话不可达——浏览器里 quick_reply 可用（重发文本），业务按钮渲染为「（请在 App 内操作）」文本，不新增 web 专属写端点。

**排期双密度（Flutter）**：App 排期页默认紧凑单行（时间 + 剧名 + 座位/状态点，无房间/DM/价格），AppBar 菜单可切时间轴（10:00–次日 04:00 视窗、跨午夜映射、越界 clamp 并标记）；点场次进详情页（全字段只读 + 生成场控码 + 换房/换 DM）。密度偏好存 `SharedPreferences`（key `schedule_density`）。时间轴换算为纯函数（`schedule_timeline.dart`），已单测覆盖。

**DM 端测试**：`mobile_app/README.md` 提供 10 步逐步测试清单（阶段模板前置 → 发码/兑换 → 开始计时 → 逐阶段操作 → 超时不自动推进 → 结束失效 → 失败用例 → 硬件降级）。清单是**待执行的验证步骤**，DM 端当前仍未真机联调。

### 4.8 排期看板、剧本筛选分页与 DM 档案（Web 已实现，2026-10-24）

**排期数据基础**：`/api/schedule` 与 `/api/app/schedule` 的场次载荷补 `duration_minutes`（剧本库时长，缺省 240），Flutter 时间轴直接吃到真实时长，不再 fallback 45 分钟。手工改期（PATCH `/api/sessions/{id}`）在写入前做**分配撞档预检**：本场手工分配的房间 / DM 与他人区间（房间含翻台缓冲、DM 含 30 分钟休息缓冲）重叠则拒绝并返回对方 session_id 明细；带 `__force__` 才放行（仅 Web 看板二次确认后发送），人确认 = 显式动作。

**时间看板（Web `/schedule`，✅ 已实现；Flutter 未实现，下一轮）**：客户端从 `/api/schedule` 构建 06:00–26:00 业务轴日历板，Pointer Events 手写拖拽（鼠标 6px 阈值 / 触屏 250ms 长按、15 分钟吸附、克隆卡跟随）；drop 走 PATCH 乐观更新，失败回弹原位；id 重命名事件委托自动生效。冲突 chips 直接复用 `build_schedule` 的建议结构：换房 / 换 DM 点击即调既有 resolve 端点，改时间建议需二次确认后走同一条 PATCH，加资源类只读。**系统绝不自动改开场时间或取消场次**——每个 chips 动作都是人点击触发的显式动作。拖拽无 JS 测试设施，靠手动冒烟清单（CSP 下 CSSOM 写位置已在本地冒烟验证无拦截）。

**状态看板（同页 tab 切换，✅ 已实现）**：待开场 / 已开场 / 已取消三列，与时间看板共用一次 fetch。拖拽换列走 `PATCH /api/sessions/{id}` 改 status（确认框注明「场控计时与剧情推进仍归 DM 端控制」），状态值只接受三态之一；已取消→已开场等非法转换前端直接拦截。

**快速建场（✅ 已实现，2026-10-25 改为建场卡片）**：`/schedule` 看板置顶，「新增场次」按钮和点击时间看板空位都会弹出**建场卡片**（原生 `<dialog>`，自动带上点击处的日期与 15 分钟吸附时间）：日期 / 小时 / 分钟按钮点选、剧本搜索 + **类型下拉 + 人数筛选**（全部 / 4–9 人及以上）、可选房间与 DM（首项「自动分配（推荐）」）、人数与价格收在「更多信息」。提交走 `POST /api/sessions`，成功后**不整页刷新**，只关闭卡片、弹 toast、重拉一次 `/api/schedule?include_scripts=0` 原地重绘两块看板。缺硬必填时服务端返回 400 `incomplete_session` + 追问文案，卡片原地显示，不落任何数据（没有草稿）。场次 ID 由「门店 + 剧本 + 开场时间」决定，同一剧本同一时间再提交一次是**合并更新而不是新增**，这时提示会明确写「已合并更新，没有新建」，避免店长以为多了一场。

**剧本筛选分页（资源页，✅ 已实现）**：新端点 `GET /api/scripts`（名称/别名包含、类型、人数区间、时长区间、来源 私有/公共库），page_size 夹取 10–200 默认 50；pandas 向量化过滤 5800+ 公共行毫秒级，不缓存。首屏服务端渲染第一页（无 JS 可读），筛选交互由 app.js 增强；来源列公开展示「公共库」badge，公共库对商家只读。`/api/app/resources`（Flutter 契约）冻结不动。

**DM 档案（✅ 已实现，Web 优先；agent / Flutter 本轮不加写入口）**：新表 `dm_leaves`（请假，Excel 后端 sidecar `dm_leaves.xlsx`，按 dm_id+日期幂等）。带本历史从 `dm_runs ⋈ sessions` 自动派生（已取消场次不计），无需手动录入；`resources()` 与 `/api/schedule` 的 dms[] 附加 `leaves` / `past_runs` / `distinct_scripts` / `recent_scripts`（`script_counts` 只留在服务端排期用，不进载荷）。排期集成两个可选 kwarg：**请假 DM 当天不参与自动分配**（按落地业务日过滤，跨夜场次归开场那天）；**熟悉度只作同收益时的次级排序键**（排在经济贡献之后，不翻盘最优性），选中 DM 没带过该本则 warning「DM X 没带过《Y》，请注意」。Web DM 卡可增删请假、展开带本记录。

**对话剧本名快速通道（✅ 已实现）**：候选唯一时（别名或全名只命中 1 部）自动确认，回复追加「唯一匹配自动确认，请核对剧本全名《…》」并回显绝对日期时间；≥2 个候选仍出选择按钮。**唯一匹配存在误建风险，靠「warning 回显 + 撤销」两层兜底**：店长说「撤销」→ Agent 调 `cancel_session`（只改 status=已取消，不删数据；缺确认拒绝、只读模式拒绝）。

**双端分工**（现状与原因）：

- **Web = 配置与管理端**：资源建档（房间 / DM / 班次 / 请假）、剧本库筛选分页、排期看板（时间拖拽 + 状态转换）、CSV 导入、阶段模板、审批与访问管理。桌面大屏操作密集型任务都在这里。
- **Flutter = 现场与响应端**：店长移动端查看排期 / 快速占位 / 对话操作按钮 / 生成场控码；DM 端场控计时与手动推进（这是移动端**必须存在**的理由——场控发生在房间现场，DM 手不能离开手机）。断线离线队列保证房间内弱网可用。
- **chat choices 为什么 App 优先**：操作按钮直连业务 API（Bearer token），Web 端 `/api/app/*` 是 Cookie 不可达的，诚实降级为「（请在 App 内操作）」文本 + quick_reply 重发文本，不为按钮另开 web 写端点。
- **移动端不做拖拽看板**：触屏拖拽在小屏的误触成本高，改期属于低频管理动作，下一轮再评估；当前 Flutter 排期是紧凑列表 / 时间轴只读 + 详情页换房换 DM。

**商业模式参考**（方向探讨，非承诺）：本产品定位是**门店私域运营辅助工具**，与轻兔、谋杀之谜秘书等店务 SaaS 互补而非替代——对方管支付、收银、C 端售票履约，本产品管「场次能不能开起来」这一段：拼场进度、DM 排班与带本评级、剧本库事实元数据、场控复盘、只读经营报表。**明确不做**：储值 / 支付 / 订单履约、C 端售票、微信自动触达（风控）。引流钩子的思路：DM 端必须用手机 → 场控码兑换即「体验入口」→ DM 熟悉产品后反向推动门店采购 Web 端；离线可用是现场刚需卖点。

**竞品功能参考**：基于行业公开资料的归纳，本环境未能实时联网核实；仅作方向参考。常见商家版功能簇里与本项目契合、值得后续借鉴的：DM 出勤 / 值班表（本项目已有班次 + 请假，可扩展出勤统计）、场次复盘（已有场控事件流，汇总报表见 九.6）、剧本销量 / 上座率排行（需要先积累场次数据）、店员绩效提成（依赖出勤与带本记录，数据基础已具备）。不契合的：支付分账、C 端拼车小程序、库存进销存——边界见 七。

### 4.9 排期变更先审后写与资源模板中文表头（Web 已实现；移动端仅「补齐未分配」，2026-10-25）

**不整页刷新（✅ 已实现）**：排期页不再有 `location.reload()`。建场、应用分配、手工调整成功后统一走一次 `GET /api/schedule?include_scripts=0` 并**原地重绘**时间看板与状态看板；单调 token 丢弃乱序返回的旧响应，旧看板在新数据到达前保持可见。`include_scripts=0` 让看板刷新不再序列化 5800+ 行公共剧本库（时长映射仍在服务端算好随场次下发）。这是「一次请求 + 重绘」，不是零延迟；拖拽改期仍走原有的单卡增量更新。

**先审后写（✅ 已实现）**：

- 「补齐未分配」与「生成优化方案」都改成**预览 → 逐条勾选（默认全选）→ 批量应用**。预览接口（`POST /api/schedule/auto-assign` 默认 `preview:true`；`POST /api/schedule/plan`）只计算不写库，返回统一的 `proposals`：每行带 `session_id`、剧本名、开场时间、`before`（原房间/DM）、`after`（建议房间/DM）、`changed`、`source`。
- 应用走 `POST /api/schedule/apply`（必须 `confirm:true`），提交的是**用户看到并勾选的那几行**，服务端**不重跑求解器**，因此不会出现「看的是一套、写的是另一套」。服务端在同一个 `store_lock` 里重新校验房间容量、房间/DM 时段重叠后**一次性写入，全有或全无**；任一行失败则一条都不写。**DM 班次覆盖只在预览/求解时校验，apply 不重算班次**——预览之后改了某位 DM 的班次，apply 仍会照写（见下方已知取舍）。
- 每行还带 `expect_room_id` / `expect_dm_id`（键存在即比对，空串表示「当时确实没有」）：期间被他人改过则返回 409 `preview_stale`，一条都不写，提示重新生成预览。
- 只缺房间或只缺 DM 的场次，`补齐未分配` 不动它（不冒险移动已有资源），而是在预览里单列 `partial` 并提示用「生成优化方案」；完全没分配的场次仍由 `补齐未分配` 处理。
- 已取消场次不占资源：求解器、预检与应用三处口径一致，避免「预览说可写、应用报冲突」。
- 所有写入都不碰 `start_time`、不改 `status`——系统仍然绝不自动改开场时间或取消场次。

**移动端（✅ 已实现，未真机联调）**：删除了排期页首次加载时的**静默自动分配**；新增「补齐未分配」预览 + 勾选 + 应用（同一后端契约，走 `/api/app/schedule/auto-assign` 与 `/api/app/schedule/apply`）。移动端本轮**不做**「生成优化方案」，只缺一项资源的场次在预览里只读并指向 Web 端。

**资源模板中文表头（✅ 已实现）**：资源页下载的三个 CSV 模板改为中文表头（房间：`房间编号,房间名称,可容纳人数,标签,每小时成本,缓冲分钟,是否启用,备注`；DM：`DM编号,DM名称,擅长类型,每小时成本,每日最多场次,是否启用,备注`；班次：`DM名称,星期,上班时间,下班时间`）。导入器把表头归一化（忽略大小写、空格、下划线、连字符）后**同时接受中文与旧英文表头**，导出 sidecar `rooms.xlsx` / `dms.xlsx` 仍可回导；缺必需列时提前返回 400 `missing_columns` 并给出中文指引。

**本地浏览器验收（2026-10-25，本地 Excel 实例，非公网）**：用 Playwright + 本机 Chrome 在独立 8001 实例（`DATABASE_URL` 清空、`WEB_STORAGE_BACKEND=excel`、独立状态库与数据目录，避免影响 8000 与本机 `data/`）跑完 15 条矩阵，全部通过：建场卡片两种打开方式与默认时间、类型 / 人数筛选真的收窄、建场后不整页刷新、同剧本同时间提示「已合并更新」且不新增、手动指定容量不足时保留场次并说明原因、补齐预览关闭后一条都没写、预览期间该行被改则 409 且其余行也不写、只缺一项资源的场次单列只读、只应用勾选的子集、优化方案只读弹窗与子集应用、三个模板中文表头 + 中文导入 + 旧英文表头兼容 + 缺列中文 400、全程无 CSP 报错。唯一的控制台错误是上面这条 409 本身（浏览器会把失败的 fetch 记为 error，属预期）。验收脚本放在临时目录，不进仓库、不进 CI；只覆盖本地 Excel 模式与桌面 Chrome，**移动端仍是未真机联调**。

**验收中发现并修复**：剧本搜索结果列表原来是绝对定位浮层，会盖住下方的「剧本类型 / 人数」筛选条，筛选看不全也点不到；已改成参与文档流（结果把卡片内容往下推），结果仍最多显示 10 条。

**已知取舍**：「补齐未分配」只处理完全没有任何分配的场次；预览精度取决于预览那一刻的数据，人在预览与点击之间改动资源时会出现 `preview_stale`（这是预期行为，不是故障）；优化方案不会移动已完整分配的场次（`lock_existing=True`）。`apply` 的重新校验只覆盖房间容量与房间/DM 时段重叠：`expect_room_id` / `expect_dm_id` 能挡住房间或 DM 被换掉，但**挡不住「DM 班次被改」**——apply 不重算班次，预览后把某位 DM 的班次删掉或改短，这次写入依然会成功。要补这一步需要把班次校验搬进 apply（或在 `expect_*` 之外再加一种基线），属于行为变更，本轮未做。

## 五、技术选型：LangChain

本节选型已落地为代码：Python 环境、依赖清单与最小 ReAct 循环均已验证通过（进度见 5.4）。

### 5.1 选型决策

**框架用现成轮子，业务工具自己开发。**

- **本项目负责**：12 个业务 Tool 函数（文本抽取、场次去重、过期过滤、空场风险识别、动态定价、表格读写、文案生成、剧本库 / 缩写别名、资源管理、排期、DM 场控只读查询、联网搜索），见 三。
- **不自建**：ReAct 调度器、大模型 API 调用、Agent 执行循环、Memory 上下文管理。

### 5.2 框架能力与选型理由

选用 **LangChain**——Python 生态中最成熟的 Agent 框架之一，适配本项目的 Python 技术栈。

框架提供的能力：

- **Agent 循环**：由 `langchain.agents.create_agent` 构建，底层是 LangGraph 状态机，自带工具注册与 Thought / Action / Observation 调度。项目仅需将 12 个业务函数以 `@tool` 装饰器注册为工具。
- **LLM 后端**：采用 **DeepSeek**（`langchain-deepseek`）。DeepSeek 提供 OpenAI 兼容接口，经 `init_chat_model` 统一接入，替换为通义千问 / 文心一言 / 豆包等只需改一处模型标识，不涉及业务代码。
- **存储**：业务工具通过 `Store` 接口读写 pandas DataFrame。生产 Web 使用 Neon Postgres（SQLAlchemy 2.x Core + psycopg 3），本地数据库模式与离线测试使用 SQLite；`pandas + openpyxl` 仍用于 CLI、Excel 兼容模式和导入导出（已实现，见 三.6 与 4.4）。

**版本基线**：以 LangChain 1.x 为准，即 `create_agent` + checkpointer 的组合。0.x 时代的 `AgentExecutor` 与 `ConversationBufferMemory` 已在 1.0 中移除（迁至 `langchain-classic`），本项目不采用。

### 5.3 选型权衡

**优点**

- 工具注册简单，代码简洁，可快速完成原型验证。
- 官方模板与社区案例充足，参考资料多。

**缺点**

- 需要具备 Python 基础。
- LangChain 版本迭代较快，需注意版本兼容。

### 5.4 落地实施路径（已落地）

1. ✅ 搭建 Python 环境，安装 `langchain`、`langchain-deepseek`、`langgraph`、`pandas`、`openpyxl`。
2. ✅ 以玩具工具验证 ReAct 调用链路：工具调用、多轮记忆、`thread_id` 会话隔离，均已通过。
3. ✅ 逐个编写 12 个业务 tool。工具 1（文本抽取）、工具 2（场次去重）、工具 3（过期过滤）、工具 4（空场风险识别）、工具 5（动态折扣定价）、工具 6（表格读写）、工具 7（营销文案生成）、工具 8（剧本库 / 缩写别名）、工具 9（资源管理）、工具 10（排期）、工具 11（DM 场控只读查询）、工具 12（联网搜索 web_search）全部完成；其中 10 个确定性工具全部落地，工具 1 与工具 7 的 LLM 调用可注入、默认测试全部离线；工具 12 未配置 key 时返回 `not_configured`，测试全部 mock 不产生真实调用。
4. ✅ 组装 ReAct Agent：12 个工具注册、系统提示词路由、`InMemorySaver` 多轮记忆与 `thread_id` 隔离均已完成；A/B/C/D/E 五场景在脚本模型下离线跑通（`scripts/09_agent_demo.py`，全量测试计数见文件头状态行）。真实 DeepSeek 已于 2026-10-19 单次验证 A/B/C/D，四个场景均按预期调用工具（见 4.3）；单次样本不构成稳定性结论。E 场景（剧本缩写 / 剧本档案补全）有离线工具测试与两轮演示，2026-10-22 已用真实模型单次验证；资源、排期与场控路由有离线工具测试，并已于 2026-10-21 用真实 DeepSeek 单次验证四条新增路由（见 4.3）。
5. ✅ 增加异常处理与字段校验，降低幻觉。工具 1 已落地「Null-first Prompt + 原文子串校验 + 字段级降级 + 结构化失败」；工具 7 已落地「双层白名单 + 内部财务词检查 + 价格 / 折扣校验 + 整次失败」；Agent 层已落地统一 JSON 信封、业务失败不抛异常、未预期异常兜底 `internal_error`、写操作前场次存在性校验。剩余边界：真实模型的工具误选与参数填写错误只能靠系统提示词和工具内校验缓解；单次验证中未出现误选，但样本量不足以覆盖。
6. ✅ 新增店长端交互控制台：`python -m app.cli` 一次构建 Agent、同一 `thread_id` 多轮记忆、`/reset` 新线程、`--read-only` 工具层写拦截、`--trace` 紧凑工具轨迹；命令、错误、会话隔离与只读行为由 `tests/test_cli.py` 离线覆盖。真实模型交互已于 2026-10-19 单次验证（见 4.3）。
7. ✅ 新增网页版代码：邮箱申请、开发者邮件审批、8 小时限时访问、备用解锁链接、每邮箱数据隔离、全站安全上限、FastAPI 页面与 API、Docker/Render 配置；并新增资源、排期、渠道单、阶段模板与场控只读页面。离线测试全部通过（计数见文件头状态行）；本地 uvicorn 启动冒烟（`/healthz`、`/login`、静态资源）均返回 200；真实 Resend 已向 `OWNER_EMAIL` 成功发送并收到一封审批邮件；真实 Neon + uvicorn 的申请、审批、领取、看板 HTTP 链路已验收。**公网部署未执行**，真实模型的网页聊天链路与新增页面未做真实模型联调（见 4.4）。
8. ✅ 新增数据库存储后端：`Store` 接口、`ExcelStore` / `SqlStore`、SQLite/Postgres schema（18 张表）、租户工作区播种、审批与每日用量的数据库实现、Excel/SQLite → 数据库迁移和反向导出（12 个 xlsx）。离线契约测试覆盖双后端一致性、租户隔离、并发播种、重启持久化、JSON 文本 sidecar 往返与迁移幂等；可选 Postgres 契约测试由 `TEST_DATABASE_URL` 启用。**Neon 项目与 schema 已创建，本机 HTTP 端到端验收通过；生产数据尚未正式导入**（见 4.4）。
9. ✅ 新增 Flutter 双角色客户端源码：商家端与 DM 端共用一套工程，包含安全存储、服务端时间偏移、本地通知、离线动作队列与场控台。**2026-10-21 已安装 Flutter 3.47.7 并通过 `dart analyze`（0 issues）与 `flutter test`；2026-10-22 已构建 Android debug APK（占位包名）；未签名、未真机联调、未上架，iOS 未构建**（见 4.6 与 `mobile_app/README.md`）。
10. ✅ 新增公共剧本库、联网搜索、对话按钮与排期双密度（2026-10-22，见 4.7）：公共库存储 / 导入 CLI / 策展种子 + vshang66 离线采集（首次全量 5800+ 条已导入）与本机每 3 天增量刷新任务；三处读路径接入公共库且私有遮蔽、`pub-` 写守卫；工具 12 `web_search`（Tavily 可插拔 + 三道服务端成本闸门，未配置返回 `not_configured`）；`choices` 服务端暴露 + Flutter 按钮直连业务 API + Web 端诚实降级；App 排期紧凑 / 时间轴双密度与场次详情页。对应离线测试：`test_public_library`、`test_web_search`、`test_chat_choices` 等；Flutter `choice_actions_test`、`schedule_timeline_test`。

## 六、技术亮点

1. **多层机制抑制幻觉**：自然语言理解交给 LLM，确定性规则、数值计算与数据读写全部落在代码工具内，从结构上压缩幻觉的产生空间（分层原则见 二）。

2. **工具可插拔、场景可迁移**：剧本杀只是验证场景，整套工具链可以迁移到密室、轰趴、美业、餐饮包间等本地生活预约业态。

3. **由 ReAct 循环按轮次选择工具**：`create_agent` 的图在「模型输出工具调用 → 执行工具 → 回填结果」之间循环，是否继续调用由模型决定。项目没有额外编写「因场次过期就跳过风险 / 定价 / 文案」这类业务条件分支；过期场次由 `filter_expired` 显式查询，风险与定价工具内部会自动排除已开场 / 已取消场次。离线脚本模型固定了调用顺序，只能验证装配；2026-10-19 的真实模型单次观察中四场景均按预期路由（见 4.3），但单次样本不构成稳定性结论。

## 七、能力边界

1. **输入侧**：不自动抓取微信群消息，全部输入由店长手动粘贴，规避微信机器人风控。
2. **履约侧**：不实现支付、订单、收银履约，与成熟门店 SaaS 互补；商业化阶段可按 API 对接第三方店务系统（见 九.4），当前设计不做。
3. **交互侧**：玩家不直接和 Agent 交互，所有信息由店长中转。（注：九.1 是后续突破该边界的规划。）
4. **模态侧**：设计范围仅支持文本输入。（OCR 海报解析见 九.2。）
5. **剧本库侧**：工具 8 本身不做拼音 / 模糊匹配，也不自动把模型联想写入档案。本地查不到缩写时，Agent 只能给出带「猜」标记的候选，或经 `web_search`（工具 12）查公开信息并标注「网络信息非官方」；两者都必须由店长确认全名后才能保存。公共库只读、私有遮蔽，场次永不指向公共行。缩写识别率取决于本地档案、公开搜索命中与模型知识，不能保证正确。
6. **渠道单侧**：大众点评没有单店可用的自助公开 API；本系统**不抓取公开页、不登录商家后台、不绕过访问控制**。只支持人工导出的 CSV/XLSX 导入，因此无法做到实时防超卖，界面显示数据截止时间，外部渠道在两次导入之间仍可能接单。未来只接官方 API / webhook，不做爬虫。
7. **场控侧**：DM 拥有阶段最高控制权，倒计时到期只提醒、标红和记录超时，**绝不自动推进剧情**；服务端没有定时推进任务。商家侧只能只读和发码 / 撤销，不能远程推进或结束。硬件模式只提供通用 HTTP 适配器契约，未对接具体厂商；失败自动降级为文字提示，不回滚阶段。
8. **客户端侧**：Flutter 源码已写，Android debug APK 已于 2026-10-22 在本机构建成功（占位包名、未签名），但未真机验证、未上架；iOS 构建需要 macOS + Xcode，本机未构建。Web 保留为桌面后台与兜底，不承诺完整移动体验，也不注册 service worker 缓存已登录数据。

## 八、风险与约束

### 8.1 业务出错风险

大模型存在幻觉，Agent 有可能解析文本出错，误判剧本名称、时间、预约人数，造成空位统计错误，引发超售、承诺错误价格，最终拼车失败，带来客诉与门店口碑损失。

**本设计对策**：玩家不直接与 Agent 交互（见 七.3），所有玩家预约、跳车消息均由店长复制粘贴转发，**店长是信息的第一道校验人**。剧本库能减少「同一剧本多种叫法」造成的识别错误，但缩写候选仍可能猜错；系统要求店长确认全名后才写入档案，不能把这一步省掉。

### 8.2 责任边界风险（商业化重点考量）

如果无人工审核，Agent 自动确认预约、承诺优惠，一旦产生履约纠纷，门店容易将责任推给 Agent / 开发者。

- 本设计中，Agent 仅作为店长的后台辅助工具，**所有业务决策、对外承诺，全部由店长人工确认**。线下服务履约主体为剧本杀门店，开发者只提供软件工具，不承担线下经营、预约履约相关责任。
- 若后续开放玩家入口（见 九.1），必须设置**店长人工终审闸门**，Agent 仅做信息采集与临时预登记，无权直接确认预约、承诺优惠。

### 8.3 合规风控约束

本项目不实现微信消息自动抓取、自动发消息机器人功能。全部输入由店长手动粘贴，所有对外营销文案需店长人工复核后再发布，以规避 IM 平台机器人风控。

### 8.4 权限隔离设计（未来迭代）

区分两套 Agent 权限：

- **店长端 Agent（高权限）**：读写场次数据、查看保本底价、高危识别、智能定价、生成营销文案。
- **玩家端 Agent（低权限）**：仅支持空位查询、预约信息收集、临时预登记；**看不到门店保本底价，不能直接确认预约，不能承诺折扣**。

### 8.5 DM 场控的权限与自动化边界（已实现）

- **DM 最高控制权**：阶段只能由 DM 在 DM 端显式发起 `start` / `next` / `previous` / `pause` / `resume` / `extend` / `finish`；最后一幕的 `next` 返回 `at_last_stage`，不自动结束。
- **倒计时绝不推进**：`stage_ends_at` 只用于展示、提前提醒和超时标记；服务端没有 cron、后台线程或定时任务调用 `next`。测试冻结时间越过结束点后，断言阶段、版本与事件数不变。
- **商家只读**：商家可生成 / 重发场控码、撤销 DM token、查看只读直播，**不能远程推进、暂停或结束**；商家端接口没有这些动作。
- **DM 最小权限**：场控码一次性、默认 30 分钟有效，库中只存 HMAC；DM token 只绑定单个 run，默认 12 小时，不能访问商家 API，也不能列出其他场次。越权 run_id 统一 404，不泄露是否存在。
- **硬件失败不阻断**：先落阶段状态与事件，再调用硬件；失败时标记降级、记录日志、返回文字提示，不回滚阶段、不自动重试、不阻塞 DM 操作。离线补传不补发硬件指令。
- **模板快照隔离**：启动场控时快照当前阶段模板；运行中改模板只影响下一场，不影响本场计时与提示。

## 九、未来迭代规划

1. **玩家交互能力（带人工审核闸门）**
   开放玩家对话入口：玩家直接提交预约咨询，Agent 收集信息并临时锁位（30 分钟有效期），生成待办推送给店长；店长人工审核通过后，预约才正式生效，超时未审核则自动释放临时锁定的空位。
   核心规则：Agent 只有信息采集能力，**最终预约确认权始终保留给门店店长**（原则见 八.2），既规避幻觉带来的业务事故，也清晰划分责任。
   待设计项：30 分钟超时释放需要定时调度组件，当前 12 个工具中仍没有对应实现；DM 场控倒计时也刻意不自动推进，不能复用作锁位释放任务。

2. **多模态扩展**
   增加 OCR 工具，识别门店海报图片中的拼场信息，支持图片类朋友圈广告解析。

3. **多 Agent 架构拆分**
   拆分为多个独立子智能体协同工作——抽取 Agent、业务决策 Agent、查询检索 Agent、营销文案生成 Agent，解耦任务，便于单独迭代测试。

4. **对接第三方店务 SaaS（轻兔、谋杀之谜秘书等）**
   Agent 处理完成、清洗后的结构化场次数据，可通过 API 回填门店原有 SaaS，与现有店务系统互补，而非替代。

5. **商家移动端看板（部分已实现）**

   > 状态：Web 排期页已实现时间看板（拖拽改期 + 冲突建议）与状态看板（见 4.8，2026-10-24）；`/runs`、`/runs/{run_id}` 提供服务端渲染的场控只读视图。Flutter 商家端源码已通过 `dart analyze` 与 `flutter test`（2026-10-25 复跑 82 项），Android debug APK 已构建（2026-10-22），但未签名、未真机联调；**移动端拖拽看板未实现**，本节保留其设计方向，作为 Flutter 端后续细化参考。

   - 全局概览：按开场时间排序，展示剧本、时间、剩余空位与状态；Web 排期页与 Flutter 首页已按这个信息层级组织。状态（拼车中 / 已满 / 已取消）与工具 4 的风险等级是两个维度，Web 现有页面分开呈现，未合并成单一「危险度」。
   - 单场详情：展示该场完整信息与排期结果。Web 通过场次卡 / 详情页查看；Flutter 详情页仍待真机验证。
   - 空位修改：`+N` 走快速占位（`/imports` 与 `/api/bookings/quick`），每次生成 `client_ref`，落一条可追溯的预约记录，默认玩家名为 `散客-xxxx`，重复点击不会重复占座。**减位仍走取消预约流程**，不提供直接改空位数字的按钮，避免绕过预约明细造成超售。
   - 取消场次：Web 状态看板已支持（拖到「已取消」列或操作按钮，PATCH status，需确认；见 4.8）。系统不会自动取消；Flutter 端仍无取消入口，走聊天或 Web 确认。

6. **经营复盘报表**
   场控结束后的基础汇总已实现（`dm_control(action="report")` 与 Web `/api/runs/{run_id}/report`：各阶段计划 / 实际停留、超时、暂停、硬件失败次数）。跨场次的每周空场率、折扣使用情况、发车成功率与周期性经营复盘报表仍未实现。

7. **分级阈值标定（工具 4 / 工具 5）**

   > 参数已可配置，尚未标定。默认值为占位值，不来自任何真实门店数据。

   工具 4 的分级阈值（多久算紧迫、门店常态到客速率是多少、多少人算大本）、工具 5 的定价参数，都属于**经验值**：只能由门店用自身经营数据标定，无法从业务逻辑推导出来。

   当前做法是把阈值与模型结构分离：

   - **结构**写在代码里，可论证：时间越少越紧迫、需要的人比平时来的多就越难救、满员无风险；定价侧则是固定成本 + 每人变动成本、让利不破边际底价、人越少均摊保本价越高。
   - **数字**抽成显式命名、可覆盖的参数（工具 4 的 `RiskParams`、工具 5 的 `PricingParams`），默认值在代码与 三.4 / 三.5 中均标注为占位。

   工具 5 需要标定的具体项：三档固定成本（`small/medium/large_fixed_cost`）、每人变动成本（`marginal_cost_per_seat`，它同时就是边际底价）、四档折扣率（`light/medium_discount` 等）。其中 `heavy_discount = 0.80` 的性质与前两者不同——数学上只要售价高于变动成本就该卖，停在 8 折是门店的**价格保护线**，属于经营策略，不是推导结果。

   **标定有先后顺序，`normal_arrival_rate`（门店常态到客速率，默认 0.5 人/小时）必须最先标定**，因为工具 4 的排序本身取决于它，而工具 5 的折扣档次又建立在工具 4 给出的倍数上。这不是一句提醒，是能跑出来的结论：`scripts/04_risk_demo.py` 第 7 步把同一批演示数据换四个 λ 各跑一遍，高危场次的名单随之改变；而默认的 0.5 低到只剩「差 1 人、剩整 2 小时」一个格子能判高危。同理，工具 5 的阶梯表在 `scripts/05_pricing_demo.py` 第 5 步现算打印，改参数即变，不会与文档脱节。

   已知偏差（本轮不修正）：到客速率**并非恒定**，周末晚上与周二下午差得多，用一个常数 λ 是粗糙近似，真实做法需按时段分别标定；且自然到客与主动推广拉来的人可能是同一批，因此优先级的边际效果被高估了一部分。定价侧另有两条：`contribution_per_seat` 是上界（一部分降价买家本会按原价购买）；模型没有价格弹性数据，无法回答「打几折能多来几个人」，只保证「不破底价就不亏」。

   `scripts/04_risk_demo.py` 第 4 步会按当前参数**现算**并打印一张分档对照表（标定视图），不手抄文档，改参数则表随之变化，不会出现「代码改了文档没改」。门店接入后，店长对照该表指出哪一档不合理，改 `RiskParams` 中的数字即可，无需改动任何逻辑。
