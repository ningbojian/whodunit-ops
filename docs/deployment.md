# 部署：Neon、Cloudflare Tunnel 与数据迁移

> **当前状态：尚未实际公网部署。** 开发机未安装 Docker，镜像构建与隧道部署都没有执行过；当前没有公网实例。部署结果以目标机上 `docker compose logs` 和公网 `/healthz` 为准。

## Neon（本机已完成）

当前 `.env` 已有 `DATABASE_URL`（池化，主机名带 `-pooler`）和 `DATABASE_URL_UNPOOLED`（直连）两份连接串；两者都包含用户名和密码，**不要提交到仓库、不要贴进聊天**。项目通过 `.neon` 关联 Neon 项目，`neon.ts` 只放分支策略，不包含密钥；`.neon` 已在 `.gitignore` 中。

- **应用运行时用池化 `DATABASE_URL`**：Neon 前面是 PgBouncer 事务模式，适合 Web 的短连接。为兼容事务池，连接参数已设置 `prepare_threshold=None`，关闭 psycopg 的服务端 prepared statement。
- **迁移、建表、导出用直连 `DATABASE_URL_UNPOOLED`**：`python -m app.store.migrate` 会自动优先读它，其次才回退 `DATABASE_URL`。显式传 `--sqlite` 时则只用本地文件，便于离线测试。
- 重新拉取环境变量：`neon env pull`。如果重新初始化：`neon login`，再在项目根运行 `neon init -y --branch production --services postgres`。CLI 版本、组织 / 项目 ID 以 Neon 控制台为准。

## 阿里云 ECS + Cloudflare Tunnel（当前方案）

应用跑在大陆地域的 ECS 上，宿主机**不开放任何入站端口**：`cloudflared` 容器主动向 Cloudflare 边缘建立出站隧道，公网请求经 Cloudflare 转发进来。这样绕开了「大陆服务器绑定域名必须 ICP 备案」的限制，也不需要外币信用卡。

> 这是技术上的可行路径，**不是合规上的豁免**——隧道不改变「大陆服务器对外提供 Web 服务」这一事实。需要完全合规时，正解是完成备案或改用香港 / 境外机器。

部署文件在 [deploy/cloudflare/](../deploy/cloudflare/)。

### 为什么是这个形状

- **用子域名 `demo.`，不做路径分发。** 全站链接是根绝对路径（`/static/app.css`、`action="/request"`、`/login`、`/api`），代码里没有 `root_path` / `SCRIPT_NAME` 支持，挂在子路径下会全部失效。
- **一台机器、一个 uvicorn 进程。** 对话记忆是进程内的 `InMemorySaver`，多 worker 会让同一用户的历史分裂；2 核 2G 也不需要更多进程。
- **不发布宿主机端口。** Docker 发布端口会直接写 iptables、绕过 `ufw`；只靠隧道出站，应用不会在 `:8000` 上裸奔。

### 步骤

1. **把域名接入 Cloudflare。** 控制台 Add a site → `ghtrending.cyou` → 免费计划，让它自动扫描导入现有记录，**逐条核对后再改 NS**。至少要保留 apex 的 A 记录（当前指向 Vercel）和任何 `_vercel` 之类的 TXT 验证记录。
2. **在注册商处改 NS** 为 Cloudflare 给的两个地址，用 `nslookup -type=NS ghtrending.cyou` 确认生效。
3. **建 Tunnel。** 两种方式二选一，细节见 [deploy/cloudflare/README.md](../deploy/cloudflare/README.md)：

   - **远程管理**：配置放在控制台。Zero Trust → Networks → Tunnels → Create a tunnel → 选 **Cloudflared** → 命名 `whodunit-ops` → 复制 token；再加一条 Public Hostname：`demo` + `ghtrending.cyou`，源站 `HTTP` → `web:8000`（`web` 是 compose 服务名，`cloudflared` 在同一个 Docker 网络里能解析它）。这层配置只存在于控制台，重建隧道会丢，变更请同步 README。
   - **本地管理**：配置放在仓库。在 ECS 上用一次性容器跑 `cloudflared tunnel login / create / route dns`，全程不经过 Zero Trust。**创建 Zero Trust 组织要求绑定支付方式**（免费版不扣费，但没有可用的卡就过不去），遇到这一步时走这条；代价是 `cert.pem` 与 `<UUID>.json` 两份凭据得自己保管。

4. **在 ECS 上部署**：

   ```bash
   git clone https://github.com/ningbojian/whodunit-ops.git
   cd whodunit-ops/deploy/cloudflare
   # 写好 .env
   docker compose up -d --build                              # 远程管理
   docker compose -f docker-compose.local.yml up -d --build  # 本地管理
   docker compose logs -f cloudflared   # 应出现 4 条 Registered tunnel connection
   ```

   本地管理模式下**每个 `docker compose` 子命令都要带 `-f docker-compose.local.yml`**，否则会退回默认的远程管理文件并报 `TUNNEL_TOKEN is missing`。

   `.env` 的变量与 Render 一节相同：`WEB_ENV=production`、`EMAIL_BACKEND=resend`、`EMAIL_FROM=onboarding@resend.dev`、`WEB_STORAGE_BACKEND=database`、`OWNER_EMAIL`、`PUBLIC_BASE_URL=https://demo.ghtrending.cyou`、`DEEPSEEK_API_KEY`、`RESEND_API_KEY`、`WEB_SESSION_SECRET`、`DATABASE_URL`（Neon **池化**串）。远程管理另加 `TUNNEL_TOKEN`，本地管理不需要。大陆机器构建时加 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，否则 `pip install` 容易超时。`.env` 已在根 `.gitignore` 排除，任何层级都不会入库。
5. 云厂商安全组**不需要**新增任何入站端口，只留 22。
6. 访问 `https://demo.ghtrending.cyou/healthz`，`db`、`schema`、`model` 三项必须都是 `true`。

### 更新

```bash
cd whodunit-ops && git pull && cd deploy/cloudflare && docker compose up -d --build
# 本地管理：docker compose -f docker-compose.local.yml up -d --build
```

### 说明

- **`--protocol http2` 是默认值。** 大陆网络对 QUIC（UDP 7844）经常不稳，表现为隧道反复重连；网络确认稳定后可删掉该参数换回 QUIC。
- 客户端 IP 由 Cloudflare 通过 `X-Forwarded-For` 传入，配合 `Dockerfile` 的 `--proxy-headers --forwarded-allow-ips='*'`，按 IP 的限流仍然有效。端口不发布，因此这条设置不会被外部伪造。
- Cloudflare 免费版**不经中国大陆节点**，延迟约等于香港边缘；首访还要叠加 Neon 冷启动 1–2 秒。
- 容器磁盘是临时的，业务数据在 Neon；重新部署只丢进程内的对话记忆。
- **本地管理生成的凭据不能入库。** `cloudflared tunnel login` 写出 `cert.pem`（账户级 API 证书），`create` 写出 `<UUID>.json`（隧道密钥）；`.gitignore` 已把 `deploy/cloudflare/cloudflared/` 整体排除，只放行 `config.yml.example`。
- **凭据属主要改成 65532。** 准备凭据的一次性容器用 `--user root` 运行，写出的文件是 `root:root 0600`；运行时容器以 nonroot（uid 65532）启动，读不到就会刷 `permission denied`。`chown 65532:65532` 之后保持 `0600` 即可。

## 备选：香港轻量 + Caddy

如果不用隧道，或者以后搬离大陆地域，可以换成一台香港或境外的轻量云服务器：**不需要外币信用卡**（支付宝 / 微信付款即可），**香港节点不需要 ICP 备案**，域名可以直接解析过来。

### 选机器

- 腾讯云轻量应用服务器或阿里云轻量应用服务器，地域选**香港**。不要选大陆地域，大陆地域绑定域名需要备案。
- 规格 **1 核 1 GB 起**，2 核 2 GB 更稳：应用空闲约 220 MB，pandas 和模型调用会有峰值。
- 系统用 Ubuntu 24.04 或 Debian 12。
- 新用户通常有 1–3 个月免费试用，可以先试用再决定是否续费。

### 部署

1. **域名加 A 记录**指向服务器公网 IP（不是 CNAME）。生效后用 `ping demo.你的域名` 确认解析。
2. **装 Docker**：

   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER   # 重新登录后生效
   ```

3. **拉代码**：

   ```bash
   git clone https://github.com/ningbojian/whodunit-ops.git
   cd whodunit-ops/deploy/vps
   ```

4. **写 `deploy/vps/.env`**（`.env` 已在 `.gitignore` 中，不会提交）：

   ```ini
   SITE_ADDRESS=demo.你的域名
   WEB_ENV=production
   EMAIL_BACKEND=resend
   EMAIL_FROM=onboarding@resend.dev
   WEB_STORAGE_BACKEND=database
   OWNER_EMAIL=Resend 账号注册邮箱
   PUBLIC_BASE_URL=https://demo.你的域名
   DEEPSEEK_API_KEY=...
   RESEND_API_KEY=...
   WEB_SESSION_SECRET=...   # python -c "import secrets; print(secrets.token_urlsafe(48))"
   DATABASE_URL=postgresql://...-pooler.../neondb?sslmode=require
   ```

   `PUBLIC_BASE_URL` 必须是最终 HTTPS 地址，它决定审批邮件里的链接。

5. **启动**：

   ```bash
   docker compose up -d --build
   docker compose logs -f web
   ```

   Caddy 会自动申请 Let's Encrypt 证书，首次启动要等 80 端口的验证通过。

6. **防火墙只放行 22 / 80 / 443**。云厂商控制台的安全组和服务器上的 `ufw` 都要改，漏一个都会导致证书签发失败。

7. 访问 `https://demo.你的域名/healthz`，`db`、`schema`、`model` 三项必须都是 `true`。

### 更新

```bash
cd whodunit-ops && git pull && cd deploy/vps && docker compose up -d --build
```

### 说明

- 应用端口不对公网开放，只有 Caddy 能访问；Caddy **不记录访问日志**，因为审批和备用链接的 token 在 URL 里。
- 容器磁盘是临时的，业务数据在 Neon；重新部署只会丢进程内的对话记忆。
- 机器重启后容器自动拉起（`restart: unless-stopped`），不需要额外守护进程。
- 单 worker 约束不变，不要改 `--workers`。

## Hugging Face Spaces（已排除）

**中国大陆无法直接访问，不作为公开演示地址。** 下面的配置保留在仓库里（`deploy/huggingface/`、`scripts/publish_hf_space.py`），供在能访问 HF 的网络环境下复用；Dockerfile 也已满足 HF 的 UID 1000 约束。

如果以后在可访问的环境里部署，流程是：建 Docker Space → 配环境变量 → `python scripts/publish_hf_space.py`。

当时选它的原因只有一个：**免费层不需要信用卡**。CPU Basic 是 2 vCPU / 16 GB 内存 / 50 GB 临时磁盘，跑这个应用绰绰有余。但它必须公开、免费层不能绑自定义域名、空闲 48 小时后休眠。

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
- **中国大陆无法直接访问 `huggingface.co` 与 `*.hf.space`**（已实测），所以不作为公开演示地址。

## Render（备选）

Render 免费层现在会要求绑定银行卡做验证（$1 为**临时预授权**，验证卡有效性，不会真的扣款并会自动释放）；但触发与否取决于风控，且没有可用的外币卡时走不通。下面的步骤保留给有卡的情况。2026-10 起本项目以**大陆 ECS + Cloudflare Tunnel**为主。

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
- 容器以非 root 的 `user` 用户（UID 1000）运行，`$PORT` 由平台注入（缺省 8000）。**探针用 `/livez`，它不碰数据库**；`/healthz` 才是依赖检查，会看数据库连通性、核心表是否存在以及生产环境的模型 key，任一不满足返回 503，不会静默回退 Excel。把探针挂在 `/healthz` 上会让 Neon 永不休眠，免费额度的计算时长几天内被烧光。

## 本轮不做

玩家直连对话、微信自动抓取或自动发消息、支付与订单履约、管理员后台、多 worker、token 级流式输出、Markdown / HTML 富文本渲染。网页版仍是店长端私域运营辅助工具。
