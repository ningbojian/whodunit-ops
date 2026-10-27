# 部署：Neon、Render 与数据迁移

> **当前状态：尚未实际公网部署。** 开发机未安装 Docker，公网 Docker 构建与 Render 部署都没有执行过；当前没有公网实例，生产业务数据尚未正式导入。部署结果需以 Render 构建日志和公网 `/healthz` 为准。

## Neon（本机已完成）

当前 `.env` 已有 `DATABASE_URL`（池化，主机名带 `-pooler`）和 `DATABASE_URL_UNPOOLED`（直连）两份连接串；两者都包含用户名和密码，**不要提交到仓库、不要贴进聊天**。项目通过 `.neon` 关联 Neon 项目，`neon.ts` 只放分支策略，不包含密钥；`.neon` 已在 `.gitignore` 中。

- **应用运行时用池化 `DATABASE_URL`**：Neon 前面是 PgBouncer 事务模式，适合 Web 的短连接。为兼容事务池，连接参数已设置 `prepare_threshold=None`，关闭 psycopg 的服务端 prepared statement。
- **迁移、建表、导出用直连 `DATABASE_URL_UNPOOLED`**：`python -m app.store.migrate` 会自动优先读它，其次才回退 `DATABASE_URL`。显式传 `--sqlite` 时则只用本地文件，便于离线测试。
- 重新拉取环境变量：`neon env pull`。如果重新初始化：`neon login`，再在项目根运行 `neon init -y --branch production --services postgres`。CLI 版本、组织 / 项目 ID 以 Neon 控制台为准。

## Render

代码已推送到 <https://github.com/ningbojian/whodunit-ops>，`.env`、`.neon`、`node_modules/`、`data/`、SQLite 文件与 Excel 已在 `.gitignore` 中排除。

1. 生成会话密钥：`python -c "import secrets; print(secrets.token_urlsafe(48))"`。
2. 用注册 Resend 时创建的 API key（本地发信已验证，Render 侧尚未配置）；把 `OWNER_EMAIL` 设为 Resend 账号注册邮箱（测试发件人限制见 [网页版](web.md)）。
3. Render 新建 Blueprint（读取 `render.yaml`）或 Docker Web Service，连接该仓库。需要设置：

| 变量 | 值 |
|---|---|
| `DATABASE_URL` | Secret，粘贴 Neon **池化**连接串（主机名带 `-pooler`） |
| `DEEPSEEK_API_KEY` / `RESEND_API_KEY` / `WEB_SESSION_SECRET` | Secret |
| `OWNER_EMAIL` | Resend 账号注册邮箱 |
| `PUBLIC_BASE_URL` | `https://<你的服务>.onrender.com` |
| `WEB_ENV` | `production` |
| `EMAIL_BACKEND` | `resend` |
| `EMAIL_FROM` | `onboarding@resend.dev`（未验证域名时） |
| `WEB_STORAGE_BACKEND` | `database`（`render.yaml` 已写好） |

4. 应用启动时会对 Neon 执行幂等的 `CREATE TABLE IF NOT EXISTS`（`ensure_schema()`），不需要先手工建表；也可以在本机提前运行 `python -m app.store.migrate --init`。
5. 部署完成后访问 `/healthz` 确认返回 200；再提交一次申请，确认收到审批邮件、批准后自动解锁、能发一条测试消息。首次访问或 Neon 休眠后，冷启动可能多等 1–2 秒。
6. 推送前可先在本机执行 `npm run build`（或 `python -m compileall -q app && python -m pytest -q`）确认全量测试通过。

## 把本地 Excel 导入 Neon（可选）

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

## 免费层与单 worker 限制

- Render **免费实例的磁盘是临时的**，且会休眠；数据库模式下业务数据、审批记录与每日用量都在 Neon，**重启 Render 不会丢**。Excel 兼容模式（`WEB_STORAGE_BACKEND=excel`）依赖本地磁盘，重启会重置工作区与审批记录，因此**生产环境已在启动校验中禁止该模式**。
- Neon 免费版：约 1 GB 存储、100 CU-hours/月；无访问约 5 分钟后自动休眠，冷启动约 1–2 秒；没有 SLA。按单店几千场次、上万条预约估算，数据量远低于 1 GB。额度耗尽后写入可能失败；定期用 `python -m app.store.export` 导出备份。
- 只能运行 **1 个 Uvicorn worker**。`InMemorySaver` 是进程内存储，多 worker 会导致同一用户的会话记忆分裂；数据库锁解决了业务数据并发写，但没有解决对话记忆。`Dockerfile` 已固定 `--workers 1`，不要在部署平台横向扩实例。
- **对话记忆不持久**：Neon 只存场次、预约、剧本、审批与用量；重启进程后同一 `thread_id` 的聊天历史仍是空的。需要持久对话记忆需另接 checkpointer，本期不做。
- 容器以非 root 的 `app` 用户运行，`$PORT` 由平台注入；`/healthz` 会执行数据库 `SELECT 1`，数据库不可用时返回 503，不会静默回退 Excel。

## 本轮不做

玩家直连对话、微信自动抓取或自动发消息、支付与订单履约、管理员后台、多 worker、token 级流式输出、Markdown / HTML 富文本渲染。网页版仍是店长端私域运营辅助工具。
