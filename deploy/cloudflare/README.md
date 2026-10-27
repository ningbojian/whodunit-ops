# Cloudflare Tunnel 部署（当前方案）

应用跑在大陆地域的云服务器上，宿主机**不开放任何入站端口**。`cloudflared` 容器主动向
Cloudflare 边缘建立出站隧道，公网请求经 Cloudflare 转发进来。这样绕开了「大陆服务器绑定
域名必须备案」的限制，也不需要外币信用卡。

> 这是技术上的可行路径，不是合规上的豁免：隧道不改变「大陆服务器对外提供 Web 服务」这一事实。
> 需要完全合规时，正解是完成 ICP 备案或改用香港 / 境外机器（见 `deploy/vps/`）。

## Cloudflare 侧的状态（不在仓库里，必须手工维护）

以下配置存在于 Cloudflare 控制台，**重建隧道后需要重新配置**，所以记录在这里。

| 项 | 值 |
|---|---|
| Tunnel 名称 | `whodunit-ops` |
| 类型 | Cloudflared（token 托管，配置在控制台） |
| Public Hostname | `demo.ghtrending.cyou` |
| 源站 | `http://web:8000` |

源站写 `web:8000` 而不是 `127.0.0.1:8000`：`cloudflared` 和 `web` 在同一个 compose 网络里，
`web` 是服务名，由 Docker 内置 DNS 解析。宿主机上没有任何进程监听 8000。

## 本机侧

1. `cp ../vps/.env .`，补一行 `TUNNEL_TOKEN`（在 Zero Trust → Networks → Tunnels 里复制）。
2. `docker compose up -d --build`。
3. `docker compose logs cloudflared` 应看到 4 条 `Registered tunnel connection`。
4. `curl https://demo.ghtrending.cyou/healthz`，`db`、`schema`、`model` 三项都应为 `true`。

`.env` 已在仓库根 `.gitignore` 中排除，任何层级的 `.env` 都不会入库；`TUNNEL_TOKEN` 也不要
贴进 issue、聊天或日志。

## 已知约束

- **不要给 `web` 加端口发布。** Docker 发布端口会直接写 iptables、绕过 `ufw`，应用会在
  `:8000` 上裸奔，隧道也就没有意义了。
- **不要起多个 uvicorn worker。** 对话记忆是进程内的 `InMemorySaver`，多 worker 会让同一用户的
  历史分裂。`Dockerfile` 已固定 `--workers 1`。
- **`--protocol http2` 是默认值。** 大陆网络对 QUIC（UDP 7844）经常不稳定，表现为隧道反复重连。
  确认网络稳定后可以删掉这个参数换回 QUIC。
- 客户端 IP 由 Cloudflare 通过 `X-Forwarded-For` 传入，`Dockerfile` 的 `--proxy-headers
  --forwarded-allow-ips='*'` 会让 uvicorn 采信它，所以按 IP 的限流仍然有效。因为端口不发布，
  这条设置不会被外部伪造。
