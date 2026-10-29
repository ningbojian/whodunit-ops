# 网页版：邮件审批与限时访问

状态与部署进度见 [README「网页版与部署状态」](../README.md#网页版与部署状态)。本文只讲机制。

## 访问流程

1. 用户打开 `/login`，填写邮箱和称呼。邮箱会规范化为小写；称呼可选。
2. 服务端创建 `pending` 申请，并只向 `OWNER_EMAIL` 发送审批邮件。邮件包含申请编号、称呼、邮箱、申请时间和审批链接，**不包含对话内容或其他用户数据**。
3. 开发者打开 `/approve/<token>`。GET 只展示申请，必须点「批准」或「拒绝」才会 POST——避免邮件预览器自动批准。
4. 批准后获得 `WEB_ACCESS_HOURS`（默认 720 小时，即 30 天）的访问：
   - 用户停留在 `/pending` 时每 3 秒轮询，批准后自动解锁；
   - 审批页同时显示备用链接 `/unlock?token=...`，关闭页面后可由开发者转发；备用链接在访问期内（默认 30 天）可重复打开，签发的会话到期时间相同。
5. 访问期结束后访问失效，接口返回 401，页面跳转 `/login?expired=1`；用户重新提交申请，开发者重新审批。
   - 会话存在浏览器 Cookie 里。换了浏览器、清了 Cookie 或点过「退出」之后，访问本身仍然有效但浏览器没有凭证。此时**重新提交申请即可**：服务端不会以「该邮箱已有有效访问」拒绝，而是照常给开发者发一封审批邮件，页面会说明这是「重新通知开发者」。原记录的到期时间不受影响。之所以去掉拦截，是因为访问期从 8 小时变成 30 天后，一次误退出等于把人锁在外面一个月。
   - 代价是：已批准的人也能触发审批邮件。审批权始终在开发者手里，且仍然受下面的邮箱冷却、每 IP 每小时和全站每日上限约束。
6. 审批链接默认 24 小时有效且只能处理一次。轮换 `WEB_SESSION_SECRET` 会让所有已签发会话立即失效，并让当前这一小时的每 IP 申请计数归零（IP 限流键就是用它加盐的）。
7. 调整 `WEB_ACCESS_HOURS` 只影响此后的批准：已批准记录的到期时间在批准时就写进了数据库，不会自动延长。改完配置后，已有访问到期时需要重新批准一次。`WEB_SESSION_HOURS` 短于 `WEB_ACCESS_HOURS` 会在启动时报错——否则访问还在有效期内，Cookie 先过期，用户会被反复踢回 `/login`。

本地开发用 `EMAIL_BACKEND=console`，审批链接打印在服务端控制台，不联网。生产使用 Resend HTTPS API（`EMAIL_BACKEND=resend`），需要 `RESEND_API_KEY`。使用测试发件人 `onboarding@resend.dev` 时，`OWNER_EMAIL` 必须与 Resend 账号注册邮箱一致；否则 Resend 会拒绝发送。验证自有域名后可把 `EMAIL_FROM` 改为该域名。

**没有自有域名也可以上线当前版本**：审批邮件只发给 `OWNER_EMAIL`，而 `onboarding@resend.dev` 允许发给 Resend 账号本人，所以单店长审批流程不受影响。只有以后需要给其他店长、员工或玩家发邮件时，才必须验证自己的域名。使用 QQ 邮箱收信时，建议把 `onboarding@resend.dev` 加入白名单或标记为非垃圾邮件。

## 每邮箱数据隔离

- 邮箱经 SHA-256 后取前 16 位作为租户 ID（`tenant_id = email_hash[:16]`）。数据库模式下，场次、预约、剧本都以 `tenant_id` 分区，所有查询由 `SqlStore` 强制带上租户条件；`tenants.email_hash` 保存完整 64 位哈希并加唯一约束。
- 同一邮箱重新获批进入同一租户；不同邮箱互相不可见、不可写。请求体不接受邮箱、uid 或文件路径，身份只来自签名 Session。
- **不按租户隔离的一共有五张表**，其中只有一张会写入用户产生的内容：`tenants`（鉴权元数据）、`access_requests`（审批申请与决定）、`usage_global_daily`（全站每日轮次计数）、`public_scripts`（运营方策展的只读剧本元数据），以及共享剧名记忆 `script_aliases`（无 `tenant_id`）。前四张都不含任何租户业务内容（`tenants` / `access_requests` 存的是鉴权元数据：邮箱哈希、称呼、审批状态、token 哈希，以及用于按 IP 限流的请求方 IP 哈希——这个哈希以 `WEB_SESSION_SECRET` 为密钥做 HMAC-SHA256，不是裸 SHA-256，否则库泄露后可以枚举 IPv4 反推地址；这次改动之前写入的行仍是旧的裸哈希，`--state-only` 导入会丢弃该字段，线上历史行的清理语句见 [部署](deployment.md#把本地-excel-导入-neon可选)）；`script_aliases` 在店长确认全名后记录一条「说法 → 公共库 `script_id`」，供全站检索复用。它不存对话正文、报价、排期或客户信息，也不含任何租户标识；只可能指向公共剧本库条目。登录页与对话页的隐私告知已如实写明这一点。
- 如果两个不同完整哈希落到同一 16 位前缀，系统**拒绝创建或访问该租户**并报错，不共用数据（fail closed）。
- 首次进入自动播种 4 个未来场次、2 条预约和 4 个同名剧本档案（演示时长是合成值），并尝试写入 3 个演示房间、3 个 DM、班次、阶段模板和演示分配；资源或模板播种失败不阻塞登录，可稍后在 `/resources` / `/stages` 手工补。数据库用 `tenants.seeded_at` 标记，播种在租户写锁内完成，只有一次；**已有数据绝不覆盖**。
- Excel 兼容模式（`WEB_STORAGE_BACKEND=excel`）使用 `WEB_DATA_ROOT/users/<hash>/sessions.xlsx`、`bookings.xlsx` 与 `scripts.xlsx`，以 `.seeded` 文件保证幂等。该模式只用于本地开发与导入导出；**生产环境会被启动校验拒绝**，因为容器磁盘是临时的。
- 同一用户的轮次由该用户锁串行执行；数据库模式下同一租户的写入再用 Postgres 事务级 advisory lock 串行（SQLite 用进程内锁）。不用 `SELECT ... FOR UPDATE`，是因为它会和子表的 FK `KEY SHARE` 锁互相等待。单进程部署仍是硬约束。

## 安全上限

访问期内不限轮数，但不是无上限。

| 类别 | 配置 | 默认 | 作用 |
|---|---|---:|---|
| 访问 | `WEB_ACCESS_HOURS` | 720（30 天） | 批准后的访问时长 |
| 访问 | `WEB_SESSION_HOURS` | 720 | 会话 Cookie 有效期；配置得比 `WEB_ACCESS_HOURS` 短，用户就会在访问仍有效时被迫重新走一遍申请 |
| 访问 | `WEB_APPROVAL_TTL_HOURS` | 24 | 审批链接有效期 |
| 访问 | `WEB_REQUEST_COOLDOWN_MINUTES` | 10 | 同一邮箱申请冷却 |
| 访问 | `WEB_REQUESTS_PER_IP_PER_HOUR` | 5 | 每 IP 每小时申请上限 |
| 访问 | `WEB_REQUESTS_GLOBAL_PER_DAY` | 50 | 全站每日申请上限 |
| 输入 | `WEB_MAX_INPUT_CHARS` | 2000 | 单条消息长度 |
| 输入 | `WEB_MAX_BODY_BYTES` | 32768 | 请求体上限，超出 413 |
| 输入 | `WEB_MAX_HISTORY_MESSAGES` | 100 | 单会话消息数；超出 409，需新会话 |
| 输入 | `WEB_MAX_HISTORY_CHARS` | 50000 | 单会话历史字符上限 |
| 输入 | `WEB_MAX_IMPORT_BYTES` | 2097152 | 渠道单 / 资源导入上传上限（2 MB），普通请求仍受 `WEB_MAX_BODY_BYTES` 限制 |
| 执行 | `WEB_MAX_CONCURRENT_TURNS` | 3 | 全站同时执行轮数 |
| 执行 | `WEB_MAX_TURNS_GLOBAL_PER_DAY` | 300 | 全站每日硬上限，按 UTC+8 跨日重置 |
| 执行 | `WEB_MODEL_MAX_TOKENS` | 1500 | 单次模型输出上限 |
| 执行 | `WEB_MODEL_TIMEOUT_SECONDS` | 60 | 单次模型调用超时 |
| 场控 | `DM_CODE_TTL_MINUTES` | 30 | 一次性场控码有效期 |
| 场控 | `DM_TOKEN_HOURS` | 12 | DM run-scoped token 有效期 |
| 场控 | `HARDWARE_TIMEOUT_SECONDS` | 2 | 硬件 Skill 调用超时；失败自动降级为文字提示 |

只有真正进入模型执行的轮次计入全站每日上限；未登录、CSRF 失败、输入超长、历史超长、并发已满、额度已满都不计数。模型调用失败或超时不退还计数，避免靠制造失败绕过总闸。全站计数存数据库（`database` 后端）或旧 SQLite 状态库（`excel` 后端）；数据库不可用时 fail closed（503），不放行模型。

审批链接和备用链接的 token 在 URL 中，因此应用关闭了 Uvicorn 访问日志（`--no-access-log`）；应用日志只记录申请编号、邮箱哈希前缀和异常类型，不记录 token、消息正文或 key。反向代理 / 平台边缘日志是否记录完整 URL 取决于平台配置，部署后可在平台侧确认或关闭路径日志。

## 本地启动

```bash
cp .env.example .env
# 编辑 .env：DEEPSEEK_API_KEY、WEB_SESSION_SECRET（至少 32 字符）、OWNER_EMAIL
# 本地保持 EMAIL_BACKEND=console
# 开发环境默认 WEB_STORAGE_BACKEND=excel；改成 database 且不填 DATABASE_URL
# 即可用 data/web.sqlite3 验证数据库链路（重启后数据仍在）
python -m app.web.main
```

打开 `http://127.0.0.1:8000/login` 提交申请，在启动终端找到 `/approve/...` 链接并打开，批准后返回用户页面等待自动解锁。发送聊天消息会真实调用 DeepSeek 并消耗 token。

**如果提交后邮箱没有收到审批邮件**：先确认 `.env` 的 `EMAIL_BACKEND`。默认 `console` 模式**不会联网发信**，审批链接只打印在运行服务端的终端里（搜索 `[本地邮件]` 或 `/approve/`）；审批邮件的收件人是 `OWNER_EMAIL`，不是申请者邮箱。要接收真实邮件，需配置 `EMAIL_BACKEND=resend`、`RESEND_API_KEY` 和 `EMAIL_FROM`，然后重启服务。

## 已验证的本地链路

- 2026-10-20 本机 uvicorn 冒烟（临时目录）：`/healthz`、`/login`、`/static/app.css` 均返回 200。
- 同日把 `.env` 切到 `EMAIL_BACKEND=resend`，提交申请后 Resend API 接受了发送请求，`OWNER_EMAIL` 收到了审批邮件；该验证未调用模型，也未做公网部署。
- 同日完成真实 Neon + 真实 uvicorn 的 HTTP 验收（`EMAIL_BACKEND=console`，未调用模型）：`/healthz` 200 → 提交申请 303 → 待审批页 200 → 控制台审批链接 → 批准页 200 → 批准 200 → 访问状态 `approved/claimable` → 领取访问 200 → 看板 200 → `/api/sessions` 返回 4 场。对应租户在 Neon 中为 4 场次、2 条预约、4 个剧本；验收结束后已删除测试邮箱的审批记录与租户。
- 控制台邮件在 Windows GBK 终端下曾因编码失败导致申请被标记为 `email_failed`，现已改为编码失败时降级输出并立即 flush，相关回归测试已加入。
- 2026-10-29 本机 uvicorn 冒烟（`WEB_STORAGE_BACKEND=database` + 临时 SQLite，`DATABASE_URL` 置空，临时数据目录）：`/healthz` 200（`db` / `schema` / `model` 均为 `true`）→ `/login` 200；启动时 `ensure_schema` 建出含 `script_aliases` 在内的全部 19 张表。该验证只走到登录页，未提交申请、未调用模型。

部署（Neon、Render、数据迁移）见 [部署](deployment.md)。
