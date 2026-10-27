# 迷雾运营台 · Flutter 双角色客户端

同一套 Flutter 工程，登录后分为两种角色：

- **商家**：今日场次、Agent 对话（含快捷追问 / 冲突解决按钮）、排期（紧凑列表 / 时间轴双视图 + 场次详情页 + 新增场次表单）、快速占位、大众点评文件导入、只读场控、生成一次性场控码、账号信息。
- **DM**：输入店长给的 8 位场控码，只读本场排期/房间/阶段模板，负责开始、暂停/继续、上一幕/下一幕、延长和结束本场。

Web 端仍是桌面后台：资源建档、阶段模板编辑、渠道单详细预览、场控直播看板都在 Web。手机端不承担这些编辑职责。

## 当前实现状态（如实说明）

2026-10-21 在本机（Windows）完成静态验证，2026-10-22 完成 Android debug 构建：

- 已安装 Flutter 3.47.7（Dart 3.13.5）与 Android SDK 36 + JDK 17；`flutter pub get` 成功解析全部依赖。
- `dart analyze .`：**0 issues**。注意：`flutter analyze` 在含中文的项目路径上会因 LSP 的 UTF-8/Content-Length 解析缺陷崩溃，本目录的静态分析结果以 `dart analyze` 为准。
- `flutter test`：**82 项全部通过**（分日累计，见下文「已验证 / 未验证」；10-25 新增 `schedule_actions_test.dart` 8 项：审核流载荷映射与应用前的勾选过滤；此前随场次草稿功能移除，较 10-24 早间的 77 项删去 3 项草稿用例）。
- 已用 `flutter create --platforms=android` 生成 `android/` 平台目录（`ios/` 未生成，iOS 构建需要 macOS + Xcode）；已执行 `flutter build apk --debug` 并成功产出 `app-debug.apk`（约 160 MB、多 ABI、占位包名 `com.example.jbs_operator`、应用名「迷雾运营台」）。

尚未完成的验证：

- debug APK 未做 release 签名、未上架、未真机联调；没有生成 IPA。
- 中文路径下 CMake/Ninja 会损坏原生依赖（`jni`）的输出路径，**debug 构建必须在 ASCII 路径的完整副本上进行**（见下文「构建注意事项」）。
- 硬件模式只实现了通用 HTTP 适配器契约；没有对接任何具体灯光/音效厂商，真实地址与鉴权需要服务端另行配置并联调。
- DM 场控的服务端状态机、场控码、离线补传和接口已经实现；客户端代码与这些接口对齐，但未经过真机联调。

analyze / test 通过 + debug 构建成功还不能说“客户端跑通”：仍需要签名、真机联调与（iOS 侧）在 macOS 上的构建。

## 硬边界

- 倒计时到点只做本地通知、界面标红和超时提示，**绝不自动推进阶段**。服务端也没有定时推进任务；阶段控制权始终在 DM 手上。
- 不做玩家直连对话，不做微信自动抓取/自动发送，不做支付与订单履约。
- 大众点评没有实时接口；客户端只上传人工导出的 CSV/XLSX，并显示数据截止时间，不声称实时同步。
- DM 端不能创建场次、剧本或资源，不能修改开场时间，不能远程操作商家端；商家端也不能远程推进/结束场控。
- 场控码、DM token 只保存在系统安全存储中，不打印、不写 URL、不写日志。

## 目录

```text
lib/
  main.dart                     # 入口、API_BASE_URL、通知初始化
  app.dart                      # MaterialApp、角色路由
  core/
    api_client.dart             # Bearer、超时、统一错误信封
    auth_store.dart             # flutter_secure_storage
    clock.dart                  # 服务端时间偏移
    device.dart                 # 本机设备标识（仅 DM 兑换限流用）
    notifications.dart          # 阶段提前/到点本地通知
    queue.dart                  # sqflite 离线动作队列（上限 200）
    session.dart                # 角色与登录态
    theme.dart                  # 暗色 token
  features/auth/                # 商家邮箱申请/轮询、DM 场控码
  features/merchant/            # 今日、对话（按钮动作）、排期（双密度/详情/时间轴/新增场次）、渠道单、我的
  features/dm/                  # 场控台、离线乐观状态转移
  ui/                           # 通用组件（含 choice_button、run_code_sheet、session_tile 紧凑模式）
test/
  choice_actions_test.dart      # 对话按钮 → API 的路径与请求体映射
  clock_test.dart
  local_state_test.dart
  queue_test.dart
  schedule_timeline_test.dart   # 时间轴纯函数（跨午夜映射、clamp）
```

## 环境与首次生成

要求：

- Flutter stable（Dart `>=3.3.0 <4.0.0`）
- Android 构建：Android SDK + JDK
- iOS 构建：macOS + Xcode（Windows 上无法构建 iOS）

平台目录状态：`android/` 已于 2026-10-21 用下面的命令生成并保留在工程目录中（本工程当前不是 git 仓库，无提交记录）；`ios/` 尚未生成（需在 macOS 上执行）。

```bash
cd mobile_app
flutter create . --platforms=android --org com.example --project-name jbs_operator
```

该命令会补齐 `android/` 脚手架，不会覆盖已有的 `lib/` 和测试文件；执行前建议先备份或提交当前目录。

然后：

```bash
flutter pub get
dart analyze .    # flutter analyze 在含中文路径上会崩溃（LSP 缺陷），用 dart analyze
flutter test
```

### 构建注意事项（本仓库特有的环境约束）

本仓库位于中文路径（`剧本杀拼场智能运营Agent`），当前工具链有三处需要额外配置，均已落地，换机器构建时请按此清单核对：

1. **必须在 ASCII 路径副本上构建**。AGP 9 用 `android.overridePathCheck=true`（见 `android/gradle.properties`）可放行非 ASCII 工程路径，但下游 CMake/Ninja 编译 `jni` 插件时仍会把中文路径损坏成乱码，导致找不到 `libdartjni.so`。已验证的做法：把 `mobile_app/` 完整复制到 ASCII 路径（例如 `C:\jbs_build\mobile_app`，排除 `build/`、`.dart_tool/`），从副本目录执行 `flutter build apk --debug`，产物拷回本仓库 `build/app/outputs/flutter-apk/`。junction/Junction 无效：Java/Flutter 会把 junction 解析回真实中文路径。
2. **Gradle 发行版走腾讯镜像**。`services.gradle.org` 会 307 重定向到 github.com，本机的 GitHub 代理证书（MITM）会导致 JDK PKIX 校验失败；`android/gradle/wrapper/gradle-wrapper.properties` 已把 `distributionUrl` 改为腾讯云镜像的同版本发行版。
3. **插件模块 compileSdk 抬到 36**。部分 Flutter 插件（如 `file_picker`）固定 `compileSdk=34`，而 `flutter_plugin_android_lifecycle` 等 AAR 要求依赖方以 36 编译。仓库内 Kotlin DSL 根脚本的 `afterEvaluate` 会因求值时序报错，已改为机器级 init 脚本 `~/.gradle/init.d/force-compile-sdk.gradle`（动态 Groovy，对所有 Android 子模块的 `compileSdk` 下限抬到 36）。换机器需重新放置该脚本。
4. `android/gradle.properties` 中另有 `kotlin.jvm.target.validation.mode=warning`：第三方插件模块（如 `flutter_timezone`）未统一 JVM 目标，按 Kotlin 官方建议降为警告，不改插件源码。
5. `flutter_local_notifications` 需要核心库脱糖（`desugar_jdk_libs:1.2.2`），已配置在 `android/app/build.gradle.kts`；缺失会在运行时崩溃。

### Android 本地 HTTP 调试

Release 包必须使用 HTTPS。若要用 Android 模拟器访问本机 `http://10.0.2.2:8000`，只在 **debug** 覆盖清单中允许明文流量：

`android/app/src/debug/AndroidManifest.xml`

```xml
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />
    <application android:usesCleartextTraffic="true" />
</manifest>
```

不要把这个属性加到 `src/main/AndroidManifest.xml`，也不要在 release 中使用 HTTP。

### 权限

主清单只需要：

```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
<uses-permission android:name="android.permission.WAKE_LOCK" />
<uses-permission android:name="android.permission.VIBRATE" />
```

不申请定位、相机、麦克风、通讯录。通知使用 `inexactAllowWhileIdle`，不申请精确闹钟权限；厂商省电策略可能让通知略有延迟，前台倒计时仍按服务端时间每秒刷新。iOS 只申请通知权限。

## 运行

模拟器（Android）：

```bash
flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8000
```

真机：把地址换成电脑的局域网 IP，并确保服务端监听该网卡。iOS 模拟器可用 `http://127.0.0.1:8000`。

Release：

```bash
flutter build apk --release --dart-define=API_BASE_URL=https://你的域名
```

API 地址在构建时固定，App 内不提供可编辑服务器地址，降低钓鱼风险。

### 真机联调（Android；截至 2026-10-22 尚未执行）

debug APK 已构建，但还没装到真机上跑过。下面是完整的真机联调步骤：

**1. 构建指向电脑的 debug 包**（debug 清单已允许明文 HTTP；地址换成电脑当前局域网 IP，`ipconfig` 查看；IP 是 DHCP 分配的，换网络后需重查）：

```bash
flutter build apk --debug --dart-define=API_BASE_URL=http://<电脑局域网IP>:8000
```

产物在 `build/app/outputs/flutter-apk/app-debug.apk`（约 160–190 MB；如在中文路径构建失败，见上文「构建注意事项」的 ASCII 副本做法）。传到手机后安装（需允许「安装未知应用」）。

**2. 电脑启动本地服务**，两个必须注意的点：

- **启动前清空 `DATABASE_URL`**（临时注释 `.env` 里那行，或 `DATABASE_URL= uvicorn ...`），否则会直连 Neon 生产库；
- Windows 防火墙放行 Python 的 8000 端口**入站**，否则手机连不上。

```bash
uvicorn app.web.main:create_app --factory --host 0.0.0.0 --port 8000
```

服务必须监听 `0.0.0.0`（默认只听 127.0.0.1 的话手机连不上）。

**3. 手机与电脑连同一个 Wi-Fi**，打开 App 走完整闭环：

- 商家：输入邮箱发起访问申请 → 从邮件批准 → 自动登录；对话页试「明晚八点开雾隐」看缺项追问与自动排房；排期页对某场生成场控码（码只显示一次）。
- DM：输入场控码兑换 → 点「开始场控」才计时 → 暂停/继续/延长/下一幕 → 结束（二次确认）。

**4. 重点验证两条硬边界**：

- 把某幕时长设 1 分钟，等倒计时到点：界面应标红「已超时」并发本地通知，**阶段绝不自动推进**，推进只能由 DM 手动点。
- 断开手机 Wi-Fi 后推进两幕再恢复：动作应进入本地队列并按序补传，补传不触发硬件。

联调通过前，不要对外声称客户端已跑通；Release 包必须用 HTTPS 并重新构建。

## DM 端联调测试清单（逐步）

真机联调时按此清单逐项走 DM 端；每步写明期望结果。当前 DM 端**未真机联调**——服务端状态机与接口已实现并对齐，以下清单是待执行的验证步骤，不代表已通过。

**前置**

1. Web `/stages` 为将要开的剧本建/确认阶段模板。无模板时 DM 点「开始场控」会收到 `template_missing`（显示无阶段可推进）——这是当前如实行为，不是故障。
2. 同租户有未开场场次：Web `/schedule` 或 App 排期页新增一场（硬必填：剧本全名 + 开场时间，齐全即建场）。

**生成与兑换**

3. 生成场控码：Web 场次操作，或 App 排期页 → 点场次进详情 → 生成场控码。选 DM（可留空 = 临时 DM）、软件/硬件模式；明文只显示一次，关闭后需重新生成（旧令牌失效）。
4. DM 手机打开 App → DM 登录页输 8 位码兑换。期望：进入待开始，只读本场排期/房间/阶段模板；不能改时间、不能创建任何资源。

**场控主流程**

5. 点「开始场控」→ 第一幕开始计时（此前倒计时不走）。
6. 逐阶段操作：上一幕 / 下一幕 / 暂停 / 继续 / 延长（1–20 分钟）→ 每次操作后事件时间线追加，服务端状态一致（Web 场控详情可核对）。
7. 超时硬边界：把某幕时长设 1 分钟等待 → 界面标红「已超时」+ 本地通知；**阶段绝不自动推进**，推进只能 DM 手动点。
8. 结束（二次确认）→ token 立即失效；客户端显示本场摘要；逐阶段实际时长在 Web 场控详情查看。

**失败与边界用例**

9. 错误码：故意输错码 → 限流提示；过期码（TTL 后）→ 拒绝兑换；店长生成后又撤销的码再兑换 → 拒绝；同一码在第二台设备兑换后，旧设备操作 → 服务端 `stale_state` 拒绝（服务端已实现、真机未验证——如实标注）。
10. 硬件：未配置 `HARDWARE_SKILL_URL` 时选硬件模式 → 返回 `hardware_not_configured` 降级标记，软件模式不受影响；离线补传不补发灯光/音效。

## 商家端流程

1. 输入邮箱发起访问申请；审批仍在邮件里完成。客户端轮询申请状态，通过后调用 `/api/app/auth/claim` 换取 Bearer token。
2. 今日页显示今日场次和进行中场控（只读）。
3. 对话页调用 `/api/app/chat`，把“已保存 / 自动排到 / 冲突选项”展示出来。助手回复下可能带操作按钮：剧本候选「就开《XX》」与快捷追问（均为重发预填文本），以及换房/换 DM（直连 `/api/app/schedule/resolve`，不经模型）；按钮提交结果以系统气泡展示 API 回执。
4. 排期页默认**紧凑单行**（时间 + 剧名 + 座位/状态点），AppBar 右上角菜单可切到**时间轴**（10:00–次日 04:00 视窗，跨午夜场次映射到底部凌晨段）；点任意场次进**详情页**（全字段只读 + 生成场控码 + 换房/换 DM）。新增场次只填剧本、时间、人数、价格，**不填房间/DM**（移动端建场仍由后端自动分配；手动指定只在 Web 建场卡片里提供）。提交到 `/api/app/sessions`；硬必填（剧本全名 + 开场时间）齐全即建场并自动分配，冲突时给出换房/换 DM/先不排等选项；缺硬必填时接口返回 400 并在表单原地报错，不落草稿数据。**首次打开排期页只读一次数据，不再静默改分配**；页头「补齐未分配」走「预览 → 逐条勾选（默认全选）→ 应用」两步：预览调用 `POST /api/app/schedule/auto-assign {"preview": true}` 且不写库，应用把用户看到的行连同 `expect_room_id`/`expect_dm_id` 提交给 `POST /api/app/schedule/apply`（`confirm: true`），服务端重新校验后全有或全无地写入；预览过期返回 409 `preview_stale` 并提示重新生成。只缺房间或只缺 DM 的场次在预览里单列只读，指引到 Web 端「生成优化方案」处理——移动端本轮**不做**优化方案。
5. 渠道单页可快速 +1/+2/+3 占位，也可选择 CSV/XLSX 预览并提交点评订单；提交后刷新场次余位。
6. 对某场生成场控码：选择 DM（可留空为临时 DM）和软件/硬件模式；明文只显示一次。
7. 我的页显示访问到期时间；退出会清除商家 token。

## DM 端流程

1. 输入场控码兑换 run-scoped token。兑换后仍是待开始，点“开始场控”才进入第一幕计时。
2. 大号计时按服务端时间显示；超时只标红并提示，不自动切幕。
3. 支持暂停/继续、上一幕/下一幕、延长 1–20 分钟、结束（二次确认）。
4. 到点与提前提醒使用本地通知；暂停时取消当前阶段通知，继续/延长/切幕/结束时重排。
5. `active/paused` 时保持屏幕常亮；退出或结束时释放。
6. 断网时本地应用动作并写入 sqflite 队列（最多 200 条），恢复网络后按顺序调用 `/api/dm/run/sync`。离线补传不会补发灯光/音效。
7. 另一台设备已操作导致版本冲突时，以服务端状态为准并提示，不盲目重试。
8. 结束场控后 DM 令牌立即失效，客户端显示本场摘要；逐阶段实际时长和事件时间线请到 Web 场控详情查看。

## 已验证 / 未验证

2026-10-21 已执行（Windows，Flutter 3.47.7 / Dart 3.13.5）：

- `dart analyze .`：0 issues（曾有 2 error / 2 warning / 70 info，已全部修复）。
- `flutter test` 基线 29 项全部通过（local_state / clock / queue）。

2026-10-22 已执行（同机）：

- `dart analyze .`：0 issues（对话按钮 + 排期双密度改造后复跑）。
- `flutter test`：**61 项全部通过**，在基线之外新增：
  - `test/choice_actions_test.dart`：对话按钮的 API 路径与请求体映射（resolve 直连 `/api/app/schedule/resolve`、quick_reply 走重发文本；resolve 无 `target_id` 时不携带该键、字段缺失不发半截请求）。
  - `test/schedule_timeline_test.dart`（17 项）：时间轴纯函数——19:00→540、次日 01:00→900、04:00 恰在视窗末界、05:00–09:59 为负（视窗外）、`blockTop` 越界 clamp、`clampDurationMinutes` null→45 及 15–720 夹取。
- Android SDK 36 + JDK 17，从 ASCII 路径副本构建：

```bash
flutter build apk --debug    # 产出 build/app/outputs/flutter-apk/app-debug.apk
```

- 构建成功：包名 `com.example.jbs_operator`（占位）、versionName `0.1.0`、minSdk 24 / targetSdk 36、应用名「迷雾运营台」，仅申请 INTERNET / POST_NOTIFICATIONS / WAKE_LOCK / VIBRATE。
- 该包为 debug 包：未做 release 签名，不能用于应用商店上架，也未在真机/模拟器上安装联调。

2026-10-23 已执行（同机，时间轴渲染缺陷修复后复跑）：

- `dart analyze .`：0 issues。
- `flutter test`：**73 项全部通过**，在 10-22 的 61 项之外，`schedule_timeline_test.dart` 增至 29 项：
  - `timelineDateKey` 营业日分组 7 项（凌晨 00:00–04:59 归前一天、05:00 回自然日、跨月、无时刻日期串保持原样、解析失败回退）。
  - `timelineHourMarks` 与 `timelineBlockTop` 渲染策略 5 项（刻度覆盖 10..23 与次日 0..4 共 19 项、偏移严格递增；视窗内块不位移——相邻深夜场次不虚构重叠、04:00 之后贴底保持可见、块高超过视窗贴顶为 0）。

2026-10-24 已执行（同机，场次中文状态映射修复后复跑）：

- `dart analyze .`：0 issues。
- `flutter test`：**74 项全部通过**，在 10-23 的 73 项之外新增 `test/session_status_test.dart`（4 项）：`sessionStatusKey` 把服务端中文状态（待开场 / 已开场 / 已取消）映射为稳定 key，未知值透传、空值走 fallback。修复前 `session_tile` / 详情页 / 排期时间轴用英文值比较中文数据，取消标记与状态徽章从不生效；同时 DM 换人弹层在服务端载荷带 `distinct_scripts` 时显示「带过 N 个本」副行（向后兼容，老服务端无该字段则不显示）。随后场次草稿功能前后端移除，删去 3 项草稿用例，总数由 77 变为 74。

2026-10-25 已执行（排期审核流改造后复跑）：

- `dart analyze .`：0 issues。
- `flutter test`：**82 项全部通过**，在 10-24 的 74 项之外新增 `test/schedule_actions_test.dart`（8 项）：勾选行 → 应用载荷的映射（`expect_room_id`/`expect_dm_id` 取预览时的原值、按原顺序提交、缺失字段降级为空串、空选择也返回合法请求体），以及「未分配 / 原名 → 新名 / 名称缺失回退 id」的文案函数。这些是纯函数测试，不覆盖真实接口；「补齐未分配」的端到端行为只在 Web 端做过本地浏览器冒烟，移动端**未真机联调**。

尚未执行：

```bash
flutter build apk --release --dart-define=API_BASE_URL=https://你的域名   # release 需要签名配置
```

iOS 需要 macOS + Xcode：

```bash
flutter build ios --no-codesign
```

在 release 构建、真机联调和上架真正完成之前，不要对外声称客户端已跑通或已上架。

## 安全说明

- 商家/DM token 只进 `flutter_secure_storage`；不使用 `shared_preferences` 保存 token。
- 离线队列只保存动作类型、时间、版本和延长/模式参数，不保存玩家姓名、电话或剧本 cue 文本。
- 退出登录会清除 token 和队列；DM 退出不会结束进行中的场控，重新进入需要店长重新生成场控码。
- 场控码只在生成时显示一次；客户端不缓存明文码。
- Release 构建必须使用 HTTPS；不要把 `HARDWARE_SKILL_TOKEN`、`DATABASE_URL`、模型 API key 或真实域名凭据写进客户端。

## 已知限制

- Android debug APK 已构建（2026-10-22，占位包名、未签名），未真机测试、未上架、未生成 IPA（`dart analyze` 与 `flutter test` 已通过，见上）。
- 客户端不提供资源建档、阶段模板编辑、数据库维护；这些仍在 Web 后台。
- 本场结束摘要只包含计划总时长、停留阶段、累计暂停和硬件状态；逐阶段实际时长尚未在客户端计算。
- 首次加载场控需要联网；如果 App 在完全离线状态下重启，无法从本地恢复上一场的完整界面（离线队列只覆盖已加载后的操作）。
- 系统通知是“尽力而为”，受通知权限和厂商省电策略影响；前台计时不受影响。
- 未对接任何具体硬件厂商；硬件模式需要服务端配置 `HARDWARE_SKILL_URL` 和可选 token。
- 不做玩家端、不做微信小程序、不做玩家直连对话。
