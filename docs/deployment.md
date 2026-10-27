# 部署：Neon、Hugging Face Spaces 与数据迁移

> **当前状态：尚未实际公网部署。** 开发机未安装 Docker，公网 Docker 构建与 Render 部署都没有执行过；当前没有公网实例，生产业务数据尚未正式导入。部署结果需以 Render 构建日志和公网 `/healthz` 为准。

## Neon（本机已完成）

当前 `.env` 已有 `DATABASE_URL`（池化，主机名带 `-pooler`）和 `DATABASE_URL_UNPOOLED`（直连）两份连接串；两者都包含用户名和密码，**不要提交到仓库、不要贴进聊天**。项目通过 `.neon` 关联 Neon 项目，`neon.ts` 只放分支策略，不包含密钥；`.neon` 已在 `.gitignore` 中。

- **应用运行时用池化 `DATABASE_URL`**：Neon 前面是 PgBouncer 事务模式，适合 Web 的短连接。为兼容事务池，连接参数已设置 `prepare_threshold=None`，关闭 psycopg 的服务端 prepared statement。
- **迁移、建表、导出用直连 `DATABASE_URL_UNPOOLED`**：`python -m app.store.migrate` 会自动优先读它，其次才回退 `DATABASE_URL`。显式传 `--sqlite` 时则只用本地文件，便于离线测试。
- 重新拉取环境变量：`neon env pull`。如果重新初始化：`neon login`，再在项目根运行 `neon init -y --branch production --services postgres`。CLI 版本、组织 / 项目 ID 以 Neon 控制台为准。

## Hugging Face Spaces（当前选定的部署目标）

选它的原因只有一个：**免费层不需要信用卡**。CPU Basic 是 2 vCPU / 16 GB 内存 / 50 GB 临时磁盘，跑这个应用绰绰有余。代价是 Space 必须公开、免费层不能绑自定义域名、空闲 48 小时后休眠。

> 2026 年的免费容器平台变动很大：ClawCloud Run 已于 2026-05-11 停服、Zeabur 公告 2026-12-31 停止运营、Render 与 Koyeb 免费层均要求绑卡。选型前先核对平台是否还在运营。

**HF 的硬性约束（代码里已满足）**：

- 容器必须以 **UID 1000** 运行——`Dockerfile` 已改为 `useradd --create-home --uid 1000 user`。SQLite 的 `data/` 目录归属也据此调整。
- 端口由 Space 卡片的 `app_port` 指定。本项目的卡片在 [deploy/huggingface/README.md](../deploy/huggingface/README.md)，值为 `8000`，与 `Dockerfile` 的 `${PORT:-8000}` 一致。
- 应用必须监听 `0.0.0.0`——`Dockerfile` 的 `CMD` 已经是。
- 磁盘是临时的，重启或重建会清空。业务数据在 Neon，**只有对话记忆会丢失**（它本来就在进程内）。

### 步骤

1. **先建 Space，拿到最终网址**。打开 <https://huggingface.co/new-space>，SDK 选 **Docker**，模板选 **Blank**，可见性选 **Public**。创建后在页面里复制直接访问地址（形如 `https://<用户名>-<space名>.hf.space`）。
2. **把网址写进 `PUBLIC_BASE_URL`**。这个值决定审批邮件里的链接，必须是最终的 HTTPS 地址；先用别的地址发信再改，已发出的审批链接会全部失效。
3. **在 Space 的 Settings → Variables and secrets 里配置**：

   | 类型 | 变量 |
   |---|---|
   | Secret | `DATABASE_URL`（Neon **池化**串，主机名带 `-pooler`）、`DEEPSEEK_API_KEY`、`RESEND_API_KEY`、`WEB_SESSION_SECRET` |
   | Variable | `WEB_ENV=production`、`EMAIL_BACKEND=resend`、`EMAIL_FROM=onboarding@resend.dev`、`WEB_STORAGE_BACKEND=database`、`OWNER_EMAIL`（Resend 注册邮箱）、`PUBLIC_BASE_URL` |

4. **推送代码**（在项目根目录）：

   ```bash
   # PowerShell
   $env:HF_TOKEN = "hf_xxx"
   $env:HF_SPACE = "用户名/space名"
   python scripts/publish_hf_space.py
   ```

   脚本只同步 `app/`、`requirements.txt`、`Dockerfile` 和 Space 卡片，不会上传 `data/`、`tests/`、`.env`。token 只从环境变量读取，不写文件、不打印。

5. Space 会自动构建镜像并启动，进度看该页面的 **Logs**。构建完成后访问 `/healthz`，`db`、`schema`、`model` 三项必须都是 `true`。

### 已知限制

- **免费 Space 必须公开**，访问控制完全依赖站点的邮件审批门控。任何人都能提交申请，但只有 `OWNER_EMAIL` 批准后才能进入。
- **自定义域名需要 PRO**（约 $9/月）。免费层只能用 `*.hf.space` 地址。
- **空闲 48 小时休眠**，之后首次访问要等冷启动（通常几十秒）。
- **重建会重置容器**：每次 `publish_hf_space.py` 推送后，进程内对话记忆清空，业务数据不受影响。
- 中国大陆的可达性需要实测——不同网络环境下 `*.hf.space` 的表现不一致，部署后请用移动数据打开一次确认。

## Render（备选）

Render 免费层现在会要求绑定银行卡做验证（部分地区为 $1 预授权），没有外币卡时走不通；下面的步骤保留给有卡或以后恢复免卡的情况。2026-10 起本项目以 Hugging Face Spaces 为主。

代码已推送到 <https://github.com/ningbojian/whodunit-ops>，`.env`、`.neon`、`node_modules/`、`data/`、SQLite 文件与 Excel 已在 `.gitignore` 中排除。

1. 生成会话密钥：`python -c "import secrets; print(secrets.token_urlsafe(48))"`。
2. 用注册 Resend 时创建的 API key（本地发信已验证，云端侧尚未配置）；把 `OWNER_EMAIL` 设为 Resend 账号注册邮箱（测试发件人限制见 [网页版](web.md)）。
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

- 免费托管平台的**磁盘普遍是临时的**，且会休眠；数据库模式下业务数据、审批记录与每日用量都在 Neon，**重启实例不会丢**。Excel 兼容模式（`WEB_STORAGE_BACKEND=excel`）依赖本地磁盘，重启会重置工作区与审批记录，因此**生产环境已在启动校验中禁止该模式**。
- Neon 免费版：约 1 GB 存储、100 CU-hours/月；无访问约 5 分钟后自动休眠，冷启动约 1–2 秒；没有 SLA。按单店几千场次、上万条预约估算，数据量远低于 1 GB。额度耗尽后写入可能失败；定期用 `python -m app.store.export` 导出备份。
- 只能运行 **1 个 Uvicorn worker**。`InMemorySaver` 是进程内存储，多 worker 会导致同一用户的会话记忆分裂；数据库锁解决了业务数据并发写，但没有解决对话记忆。`Dockerfile` 已固定 `--workers 1`，不要在部署平台横向扩实例。
- **对话记忆不持久**：Neon 只存场次、预约、剧本、审批与用量；重启进程后同一 `thread_id` 的聊天历史仍是空的。需要持久对话记忆需另接 checkpointer，本期不做。
- 容器以非 root 的 `user` 用户（UID 1000）运行，`$PORT` 由平台注入（缺省 8000）；`/healthz` 会检查数据库连通性、核心表是否存在以及生产环境的模型 key，任一不满足返回 503，不会静默回退 Excel。

## 本轮不做

玩家直连对话、微信自动抓取或自动发消息、支付与订单履约、管理员后台、多 worker、token 级流式输出、Markdown / HTML 富文本渲染。网页版仍是店长端私域运营辅助工具。
