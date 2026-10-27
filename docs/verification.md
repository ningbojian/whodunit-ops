# 验证记录

诚实原则：**每一次真实模型调用都是单次样本，不构成稳定性结论。**这里记录的是"某天某次跑出来什么"，不是"这个能力已验证稳定"。

---

## 真实模型验证

模型均为 `deepseek:deepseek-chat`。数据写临时目录，真实 `data/` 的 mtime 前后一致；日志中未出现 `sk-...` 形态的密钥，只打印「检测到 DEEPSEEK_API_KEY：是/否」。

| 日期 | 覆盖范围 | 结果 |
|---|---|---|
| 2026-10-19 | `09_agent_demo.py --live` 跑 A/B/C/D + CLI 连续 4 条消息 | 全部沿推荐链选工具，`ok=true`，无 `error_code` |
| 2026-10-21 | A–D + F（资源缺口）/ G（场次录入 + 自动分配）/ H（资源不足）/ K（场控只读） | 全部 `ok=true`；A 返回 `resources_not_configured` 时如实说明「已保存但未分配」 |
| 2026-10-22 | 场景 E（剧本缩写 / 残缺档案）三轮 | 先查别名 → 未命中先确认 → 存草稿再追问 |
| 2026-10-25 | A–D + F/G/H/K + 第 9 轮「补齐未分配」 | 全部 `ok=true`；第 9 轮暴露 Agent 直接写库的问题（见下） |

### 2026-10-19（A/B/C/D）

- 场景 A：`extract_message → dedupe_sessions → store_data(upsert_sessions) → assess_risk → suggest_pricing`；B：先 `get_session` 再 `record_booking`；C：先 `get_session` 再 `cancel_booking` 再重算风险与定价；D：`write_marketing_copy` 返回两版文案。
- 控制台 4 条消息（临时目录 + `--store 迷雾剧场 --trace`，退出码 0）：拼场广告走 A 链；「现在哪场最危险？」→ `assess_risk`；「帮我写今晚的推广文案」→ `write_marketing_copy`；`/status` 显示消息数 19，会话记忆正常。
- **观察**：场景 A 中模型注意到广告写「还差 4 人」但系统已约 0 人，主动指出数据不一致并要求补录，**没有编造预约记录**。
- **限制**：该次验证早于剧本库功能上线，**未覆盖 `scripts.xlsx`**。

### 2026-10-21（新增路由四轮）

同一次运行接着跑新增路由四轮；脚本内 `_seed_newtools` 先载入演示资源、剧本档案与阶段模板。

- 「房间里和 DM 还缺什么」→ `manage_resources(action="setup_status")`，按缺口清单逐项回答，并指出演示数据、时薪缺失会影响成本分析。
- 「记一场 10 月 23 日 20:00《雾隐山庄》」→ `extract_message` 后多调了一次 `store_data(get_session)`（返回 `session_not_found`），模型没被带偏，继续 `script_library(search)` + 当时的场次草稿工具（草稿机制已在后续迭代移除，现为硬必填齐全直接建场）；建场后 `auto_assign=assigned`（A 房 / 小北），并说明人数 6 / 价格 168 / 类型「推理」来自剧本档案而非店长原话。
- 「这周有没有排不开的场次？」→ `schedule_sessions(list)` + `manage_resources(setup_status)` + `schedule_sessions(plan)`，识别出 10-20 22:00《年轮》的 `dm_unavailable`，列出三类选项——改到 20:00、给 DM 补班次、让同晚低约场次改期——**明确不自动执行**，并指出三个剧本缺时长、按默认 240 分钟估算。
- 「现在场控演到哪一幕了？」→ `dm_control(action="status")`，无进行中场次时如实说明为空。
- 场景 A 的自动排期返回 `resources_not_configured`——该轮还没建房间 / DM。模型如实说明「场次已保存但未分配」，并建议先建档，没有编造分配结果。
- **本轮顺带修掉一个演示缺陷**：演示跨夜班次原来只到次日 02:00，22:00 开场的 4 小时本加 30 分钟 DM 缓冲到 02:30，会被判 `dm_unavailable`。已延长到次日 04:00，并加了回归断言 `tests/test_resources.py::test_demo_shifts_cover_late_session`。

### 2026-10-22（场景 E，三轮）

1. 「WY 是哪个本？」→ 一次 `script_library(search, query="WY")`，命中别名返回《雾隐村》完整档案（恐怖 / 6 人 / 240 分钟 / 168 元），`ok=true`；答复里主动提示可按增量录入排队次。
2. 「帮我加个剧本《年轮》，8 人本」→ 先 `search` 查重，未命中后**没有把猜测直接写库**，先请店长确认全名；本轮未写任何数据。（离线脚本模型在这一步是直接 upsert，真实模型更保守——属路由允许的合理行为。）
3. 确认后 → `upsert(script_name="年轮", total_seats="8人")`，返回 `待补全`（缺 `script_type`、`duration_minutes`）；模型只追问类型一项、明说时长可后补，符合「一次只问一个」。落库核对：临时目录 `scripts.xlsx` 中存在《年轮》行（8 人、时长空）。

### 2026-10-25（含第 9 轮）

复现命令（临时目录，`DATABASE_URL=` 显式清空以免连到真实 Neon 库，退出码 0；只打印「检测到 DEEPSEEK_API_KEY：是/否」）：

```bash
DATABASE_URL= PYTHONUTF8=1 python scripts/09_agent_demo.py --live
```

- A–D 与 F/G/H/K 与既往记录一致，全部 `ok=true`。
- **新增第 9 轮（只为观察，不改产品语义）**：先写入一场 12 人《千灯愿》（演示房间最大 10 人，必然排不上），再说「把没排上的场次补一下」。模型调用 `schedule_sessions(list)` → `schedule_sessions(auto_assign, session_ids=[…])` → `manage_resources(list_rooms)`，三个都 `ok=true`；答复说明排不上的原因是「三间房最大 10 人，装不下 12 人本」，给出补房 / 改人数 / 先不排三个选项并请店长决定。跑完后核对临时目录的 assignments 表，该场**没有任何分配行写入**。

> ⚠️ **第 9 轮暴露了一条与 Web 端不一致的路径**：Agent 的 `schedule_sessions(action="auto_assign")` 会**直接写库**，没有 Web 看板的「预览 → 逐条勾选 → 应用」审核步骤。这次因为房间放不下，实际什么都没写；但只要资源能排上，模型一句话就会落库。这是已知差异，本轮只记录、未改语义（统一需要新增参数与提示词，属于功能变更）。

### 这些记录能证明什么、不能证明什么

- **能证明**：当天这次调用可用；工具装配正确、参数填得对；失败时会如实说明而不编造。
- **不能证明**：模型稳定选对工具或答复正确。每次都是一个样本；网页端与 Flutter 端**没有做过真实模型联调**。
- 离线 demo 与 CLI 测试使用脚本模型，固定了调用顺序，**只能验证装配**——工具注册、参数传递、读写与结果回填——不能证明真实模型的调度能力。

---

## 浏览器验收（2026-10-25，本地 Excel 实例，非公网）

用 Playwright + 本机 Chrome 在独立 8001 实例上跑完 15 条矩阵，全部通过。该实例 `DATABASE_URL` 清空、`WEB_STORAGE_BACKEND=excel`、使用独立状态库与数据目录，避免影响 8000 端口与本机 `data/`。

| 覆盖项 | 结果 |
|---|---|
| 建场卡片两种打开方式与默认时间 | ✅ |
| 类型 / 人数筛选真的收窄结果 | ✅ |
| 建场后不整页刷新 | ✅ |
| 同剧本同时间提示「已合并更新」且不新增 | ✅ |
| 手动指定容量不足时保留场次并说明原因 | ✅ |
| 补齐预览关闭后一条都没写 | ✅ |
| 预览期间该行被改则 409 且其余行也不写 | ✅ |
| 只缺一项资源的场次单列只读 | ✅ |
| 只应用勾选的子集 | ✅ |
| 优化方案只读弹窗与子集应用 | ✅ |
| 三个模板中文表头 + 中文导入 + 旧英文表头兼容 + 缺列中文 400 | ✅ |
| 全程无 CSP 报错 | ✅ |

唯一的控制台错误是上面这条 409 本身（浏览器会把失败的 fetch 记为 error，属预期）。

**验收中发现并修复**：剧本搜索结果列表原来是绝对定位浮层，会盖住下方的「剧本类型 / 人数」筛选条，筛选看不全也点不到；已改成参与文档流（结果把卡片内容往下推），结果仍最多显示 10 条。

**范围限制**：验收脚本放在临时目录，不进仓库、不进 CI；**只覆盖本地 Excel 模式与桌面 Chrome**，移动端仍是未真机联调。

---

## Flutter 客户端

> **结论先行**：客户端能力只能视为「已通过静态分析与单元测试、并已构建 debug 包，未签名、未联调、未上架」。

### 已做到的

- 2026-10-21 本机安装 Flutter 3.47.7（Dart 3.13.5）与 Android SDK 36 + JDK 17，`mobile_app/` 已执行 `flutter pub get`。
- `dart analyze .` 为 **0 issues**。
- `flutter test` 分日复跑：10-21 基线 29 项（`local_state` / `clock` / 离线队列，含 `sqflite_common_ffi`）→ 10-22 增至 61 项（对话按钮映射 + 时间轴纯函数 12 项）→ 10-23 复跑 73 项 → 10-24 复跑 74 项（新增 `sessionStatusKey` 中文状态映射 4 项）→ 10-25 复跑 **82 项全部通过**（新增排期审核流纯函数 8 项）。期间场次草稿功能前后端移除，删去 3 项草稿用例。
- 2026-10-22 执行 `flutter build apk --debug` 并产出 `app-debug.apk`（约 160 MB、多 ABI、占位包名 `com.example.jbs_operator`）。

### 两个环境坑

- **`flutter analyze` 在含中文的项目路径上会崩溃**（LSP 的 UTF-8 / Content-Length 解析缺陷），静态分析结果以 `dart analyze` 为准。
- **debug 构建必须在 ASCII 路径的完整副本上进行**：本仓库路径含中文，CMake / Ninja 编译原生依赖时会损坏输出路径。Gradle 镜像与 compileSdk 覆盖说明见 `mobile_app/README.md`。

### 还没做的

- **未做 release 签名**、未生成 IPA（iOS 构建需要 macOS + Xcode，本机未构建）、**未真机联调、尚未上架**。
- DM 端的 10 步逐步测试清单在 `mobile_app/README.md`——那是**待执行的验证步骤，不是已通过项**。
