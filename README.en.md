# Whodunit Ops · Session-Filling Operations Agent for Script-Murder Venues

🇨🇳 [中文](README.md) · 🇺🇸 [English](README.en.md)

**whodunit** = the murder-mystery genre, **ops** = venue operations. A private-traffic operations assistant for script-murder / escape-room owners: a ReAct Agent plus 12 deterministic business tools, shipped as a CLI console, a FastAPI web back office, and a Flutter dual-role client.

It solves exactly one problem — **whether a session can actually run**: filling progress, scheduling and DM assignment, show control timing, and factual script metadata. It is not a consumer matchmaking platform, nor a POS / payment SaaS; it is **complementary to, not a replacement for**, venue SaaS such as Qingtu or Mousha Zhi Mi Mishu.

| | |
|---|---|
| Repository | <https://github.com/ningbojian/whodunit-ops> |
| Stack | Python 3.13 · LangChain 1.x / LangGraph · FastAPI · pandas · Neon Postgres · Flutter |
| Offline | Running tests and most demo scripts needs no API key, no network, and does not touch the real `data/` |

---

## Current status

| Module | Status | Notes |
|---|---|---|
| 12 business tools + ReAct Agent | ✅ Implemented | Scenarios A–E run offline under a **scripted model** (this does not mean a real model would pick the same tools); 4 one-shot validations against real DeepSeek, see [verification notes](docs/verification.md) |
| Owner CLI console | ✅ Implemented | `python -m app.cli`, multi-turn memory / `--read-only` / `--trace` |
| Web app (approval + time-limited access + per-email data isolation) | ✅ Deployed | Demo at `https://demo.ghtrending.cyou`; mainland ECS + Cloudflare Tunnel, see [deployment](docs/deployment.md) |
| Resources / scheduling / incremental intake / channel orders / DM show control | ✅ Implemented | Mostly on Web, partially covered by Flutter |
| Public script library | ✅ Implemented | 5800+ factual metadata rows, read-only shared + private shadowing |
| Shared script-name memory | ✅ Implemented | After the owner confirms a full name, records a "user phrasing → public script" mapping reused site-wide; **script names only** (the known cost of 2-character keys is documented in [public library](docs/public-library.md#共享剧名记忆script_aliases)), never chat text, quotes, schedules or customer data; in Excel mode it persists as a `script_aliases.json` sidecar in the shared data directory, and both the database and Excel agent-assembly branches inject the same memory |
| Flutter dual-role client | ⚠️ Partially verified | `dart analyze` 0 issues, `flutter test` **82 tests all passing**, Android debug APK built; **no release signing, no on-device testing, not published** |
| Hardware lighting / sound integration | ⚠️ Contract only | Generic HTTP adapter contract, **not wired to any specific lighting / sound vendor** |
| Public deployment | ⚠️ Deployed, high latency | **Live at `demo.ghtrending.cyou`**; Cloudflare's free plan routes mainland traffic to Los Angeles, and link-level packet loss pushes time-to-first-byte to 0.9–19 s — a network-path problem that cannot be fixed on the application side |
| Tests | ✅ | **1725 pytest tests passing**, plus 2 optional Postgres tests skipped by default |

**How to read this document**: only items marked ✅ are implemented; everything else is either a settled design or an explicitly labelled unverified item. **Three things we deliberately did not do** — Flutter on-device testing, threshold calibration, real-model stability — are not pretended to be finished here.

---

## Quick start

Environment: Python 3.10+, verified on Python 3.13.9. On Windows, prefix commands with `PYTHONUTF8=1` if Chinese output is garbled.

```bash
python -m pip install -r requirements.txt

# Offline tests: no API key, no network, no writes to the real data/
python -m pytest -q

# Owner interactive console (real DeepSeek)
python -m app.cli
python -m app.cli --data-dir data --store 迷雾剧场 --thread store-manager --trace
python -m app.cli --read-only   # the tool layer rejects session/booking/resource/schedule writes

# Web app (local): first fill WEB_SESSION_SECRET / OWNER_EMAIL in .env;
# with EMAIL_BACKEND=console the approval link is printed to the server console
python -m app.web.main         # http://127.0.0.1:8000/login
```

- **API key**: copy `.env.example` to `.env` and fill in `DEEPSEEK_API_KEY` locally. Keys are read from `.env` only, never from the command line; startup only says "detected", and error messages are redacted as `sk-***`.
- **Optional npm wrappers**: `npm run build` (compileall + full pytest), `npm start`, `npm run migrate -- --init`.
- **Data files**: `data/sessions.xlsx`, `data/bookings.xlsx` and `data/scripts.xlsx` are the defaults for the CLI and Excel compatibility mode; missing files are read as empty tables and **created automatically on first write**. The web app does not read or write these xlsx files when `WEB_STORAGE_BACKEND=database` (the production default): production writes to Neon Postgres, and locally without `DATABASE_URL` it writes `data/web.sqlite3`.
- **Demo scripts**: `scripts/01–09`; `02_storage_demo.py` ⚠️ deletes and rewrites two tables in the real `data/` without a backup. See [development notes](docs/development.md).

---

## What it can do

One ReAct Agent drives five scenarios. **The exact tool chain is chosen by the model from the system prompt, not fixed in code.**

| Scenario | What the owner does | Tool chain |
|---|---|---|
| **A** Parse a filling ad | Paste the WeChat carpool text | `extract_message → dedupe_sessions → store_data(upsert_sessions) → assess_risk + suggest_pricing` |
| **B** Record a player booking | Forward the booking message | `extract_message → store_data(get_session / query_sessions) → store_data(record_booking)` |
| **C** Player drops out | Forward the cancellation message | `extract_message → store_data(cancel_booking) → assess_risk + suggest_pricing` |
| **D** Generate private-traffic copy | Give an instruction | Calls only `write_marketing_copy` |
| **E** Ask about an abbreviation / add a script | "Which script is NL?" | `script_library(search) → (one web_search if not local) → (owner confirms) → script_library(upsert)` |

Three hard rules running through the whole system:

- **Look up before writing**: the `candidate_session_id` produced by extraction is only a candidate; the Agent must call `get_session` to confirm the session exists before writing.
- **Nothing is persisted before confirmation**: script abbreviation candidates, searched public information and model guesses all wait until the owner confirms the full name before entering the script library.
- **The system never changes start times or cancels sessions by itself**: every reschedule, room change or cancellation is an explicit, human-triggered action.

Two more behavioural constraints: **scenario A's operational advice is a summary the model composes from tool results**, not a 13th tool; **in scenario D the Agent only relays verbatim** — session selection, two-layer redaction and post-generation validation all happen inside the tool, and the Agent neither rewrites nor appends copy.

---

## The twelve business tools

All 12 tools are implemented and registered with the ReAct Agent. This is an overview; **full contracts, field lists, error codes and known limits are in the [tool reference](docs/tools.md)**.

| # | Tool | One-line responsibility | Access |
|---|---|---|---|
| 1 | [Text extraction](docs/tools.md#1-文本抽取) | Colloquial filling / booking messages → structured `sessions` + `bookings` | Read-only |
| 2 | [Session dedupe](docs/tools.md#2-场次去重) | Detect the same session forwarded twice; field-level merge, empties never overwrite | Read-only |
| 3 | [Expiry filter](docs/tools.md#3-过期过滤) | Drop already-started sessions by `start_time`, never trusting `status` | Read-only |
| 4 | [Empty-session risk](docs/tools.md#4-空场风险识别) | Two independent dimensions: will it fold (`fail_risk`) and which to rescue first (`priority_level`) | Read-only |
| 5 | [Dynamic discount pricing](docs/tools.md#5-动态折扣定价) | Four price tiers from the arrival-rate multiplier, never below the marginal floor | Read-only |
| 6 | [Table storage & retrieval](docs/tools.md#6-表格存储与读取) | Session table + booking detail table, one merge rule shared by three backends | Read/write |
| 7 | [Marketing copy](docs/tools.md#7-营销文案生成) | Select session → two-layer redaction → community short form + Moments long form | Read-only |
| 8 | [Script library & abbreviation aliases](docs/tools.md#8-剧本库与缩写别名) | Full name / aliases / genre / player count / duration, store-then-fill; records name mappings after confirmation | Read/write (**public library** read-only) |
| 9 | [Resource management](docs/tools.md#9-资源管理) | Rooms, DMs, shifts and a gap list | Read/write |
| 10 | [Resource scheduling](docs/tools.md#10-资源排期) | Assign room + lead DM at fixed session times, with reason codes when it cannot fit | Read/write |
| 11 | [DM show-control read-only query](docs/tools.md#11-dm-场控只读查询) | Query stage / remaining time / degraded state, **cannot advance** | Read-only |
| 12 | [Web search](docs/tools.md#12-联网搜索-web_search) | Query public information when the local library misses; three cost gates, returns `not_configured` without a key | Read-only (never writes) |

> **Shared invariant**: the venue's break-even floor is internal only and never enters any outbound copy. This is structural — internal finance columns are registered in `INTERNAL_COLUMNS`, so the DataFrame handed downstream simply does not contain them.

---

## Architecture

**Layering principle**: natural-language understanding goes to the LLM; deterministic rules, arithmetic and data access are all wrapped as separate code tools, structurally shrinking the room for hallucination.

**Loop**: Reason → Act → Observation until the task completes. **Memory**: a checkpointer (`InMemorySaver`) isolates conversations by `thread_id`; conversation memory only stores dialogue and **is never a data source** — session data lives in tables.

**Technology choices**: LangChain 1.x `create_agent` plus the LangGraph state machine; no home-grown ReAct dispatcher, LLM client or memory manager. The LLM backend is DeepSeek (OpenAI-compatible, so switching models changes one identifier). The 0.x-era `AgentExecutor` and `ConversationBufferMemory` are gone and not used here.

**Write boundary**: only `store_data` write actions, `script_library(upsert)`, the resource write actions of `manage_resources`, and `auto_assign` / `apply` / `resolve` in `schedule_sessions` write to storage; the other 8 tools (including `dm_control` and `web_search`) are read-only. Business failures return `ok=false`, unexpected exceptions fall back to `internal_error`, and neither throws into the ReAct loop. `--read-only` is enforced hard in the tool layer — a code boundary, not a prompt convention.

**Transaction boundary**: in Excel mode each table file is replaced atomically on its own, so writes spanning files have no transaction; in database mode each table write runs in a transaction, but cross-table operations are still not a single transaction. Keep backups of real data.

**No business conditionals in code**: the graph loops between "model emits a tool call → execute the tool → feed the result back", and the model decides whether to continue; the project does not encode branches such as "skip risk / pricing / copy because a session expired" — expired sessions are queried explicitly through `filter_expired`, and the risk and pricing tools exclude started / cancelled sessions internally.

**Portability**: script-murder is only the validation scenario. The tool chain does not depend on it and can move to escape rooms, party houses, beauty salons, restaurant private rooms and other local appointment businesses.

See [Agent assembly and console](docs/agent.md) for details.

---

## Web app and deployment status

**Deployed.** The web app runs the full flow "apply → developer email approval → 30 days of access → re-apply after expiry", with per-email data isolation (`tenant_id = email_hash[:16]`, different emails cannot see each other). Demo address **`https://demo.ghtrending.cyou`** (mainland ECS + Cloudflare Tunnel; the host opens no inbound ports).

- **Storage**: business data, approval records and daily usage can live in Neon Postgres (`WEB_STORAGE_BACKEND=database`); locally without `DATABASE_URL` it writes `data/web.sqlite3`. **Production rejects `WEB_STORAGE_BACKEND=excel`** — temporary disks lose everything on restart, so startup validation fails immediately.
- **Access records keep only hashed IPs**: the IP in approval / access records is written as a keyed hash (`hash_ip`, keyed by `ip_hash_secret` on `AccessStore`) as a 64-character hex string, never in plain text. A bare SHA-256 can be enumerated back to an address row by row, so a key is mandatory — **rotating the key rotates every historical IP key**. An empty address or empty secret raises `ValueError` rather than silently degrading to a bare hash.
- **Verified**: **the Neon project `shiyuan` (branch `production`) is created**, the schema is in place, and the apply / approve / claim / dashboard HTTP flow passed end-to-end acceptance against a local uvicorn; on 2026-10-20 a real Resend email to `OWNER_EMAIL` succeeded (only the Resend account owner's own inbox was verified). **Production business data has not been formally imported yet.**
- **Deployment target**: **mainland ECS + Cloudflare Tunnel**, deployment files in `deploy/cloudflare/`. The host opens no inbound ports and `cloudflared` only establishes an outbound tunnel, which sidesteps the rule that a mainland server binding a domain must have an ICP filing — **this is a technical grey area, not a compliance exemption**. The legitimate alternative is a Hong Kong lightweight server with Caddy (`deploy/vps/`, no filing needed). Hugging Face Spaces was ruled out because it is unreachable from mainland China; Render's free tier requires a card ($1 is a temporary pre-authorisation, not charged, and released automatically, but a usable card is still required). **Deployment was completed on 2026-10-28**; `deploy/cloudflare/.env` needs `TUNNEL_TOKEN`, the Neon pooled connection string, the API key created when **registering Resend**, the DeepSeek key and `WEB_SESSION_SECRET`, plus `EMAIL_BACKEND=resend`, `EMAIL_FROM=onboarding@resend.dev` and `WEB_STORAGE_BACKEND=database` (local Resend sending is verified; the cloud side is not configured yet).
- **Measured tunnel latency** (2026-10-28, deployed at `demo.ghtrending.cyou`): Cloudflare's free plan routes all mainland traffic to **Los Angeles (LAX)** — `cloudflared`'s 4 connections land on `lax01`, `lax05` and `lax09`, never Hong Kong. This path **drops packets**, so latency is unpredictable: the TCP handshake is a steady 0.2–0.3 s, but time-to-first-byte jumps randomly between **0.9 and 19 s** and occasionally times out. The decisive control is that **plain HTTP (no TLS at all) also takes 1–2.7 s**, so the slowness has nothing to do with TLS or HTTP/2; meanwhile `github.com` TLS takes only 0.24 s. The observed 1/4/8/16-second staircase matches TCP retransmission backoff. **This is not a configuration problem and cannot be removed by changing protocols or code**; low latency requires a Hong Kong or overseas lightweight server with a direct connection. The application side can only cut round trips: measured cross-border to Neon Singapore, DNS 120 ms, TCP 55 ms, warm query 55 ms, new connection about 240 ms; `/healthz` went from 12 SQL statements to 1 (0.82 s → about 0.1 s), `/runs` went from growing linearly with session count to reading each table once, `/schedule` ships first-screen data with the HTML (the browser no longer issues an extra `/api/schedule`), and a single page dropped from 24 queries to 12; the public-library frame is now cached across requests (it used to re-read all 5809 rows per request), script search no longer builds records row by row with `iterrows` (pure public-library path 309 ms → about 40 ms), and public-library merging went from row-by-row copying to vectorised filtering by `name_key` (5,809 rows 214 ms → about 47 ms; private + public end to end about 75 ms). All of these are local measurements and will differ on other machines.
- **Script-search fallback and shared script-name memory**: a normal match (exact / prefix / mutual containment) returns immediately; the fallback only runs when nothing matches, or when the only hits are weak matches where the query contains a short script name — first the global name memory, then character-bigram candidates (at most 3, tagged `match_quality=suggested`; queries shorter than 3 characters return empty, because 《青搂》 and 《青楼》 share no bigrams and guessing would only produce false candidates). Every result carries `match_quality` so the model can decide whether to ask the owner first. The global memory **stores only "user phrasing → public script_id"**, written through `script_library(action="remember")` only after the owner confirms a full name, and never stores chat text, quotes, schedules or customer data. Write validation is structural: long phrasings must align to a contiguous substring of the script name (at most one character off), 2–3 character phrasings must be character-by-character substrings (no wrong characters; when the target name itself is only 2–3 characters the rule relaxes to equal length with at most one change, otherwise misspellings of 《青楼》 could never be learned), digits are compared one by one using the Unicode digit class and the symbols and units around them must also match the script name (`￥1440` and `14:40` are not treated as spellings of 《1440》), phrasings containing invisible characters (including zero-width fillers) are rejected outright, and **phrasings that look like several script names at once are rejected** (3 characters and above) — under a series, whoever confirms first would bind everyone else, so it is better to forget; 2-character phrasings are exempt, otherwise every misspelling of a two-character title like "青X" would collide and never be learned. The global table also has a row cap. Reads happen as a single point lookup only when normal matching is empty or reduced to weak matches, with no cache, and **do not participate in scoring** — merging learned keys into the alias list would let them displace a genuine exact hit in the `(score, name_len, name_key)` ordering. In Excel mode this memory persists as a `script_aliases.json` sidecar in the **shared data directory**, and `create_app` really creates it; both the database and Excel agent-assembly branches inject the same memory into `AgentDeps.script_aliases` — in Excel mode `AgentDeps.store` is None, so memory cannot come from the store and must be injected explicitly, and missing either branch makes remember silently degrade to `alias_memory_unavailable`.
- **Limits**: **only one Uvicorn worker may run** — `InMemorySaver` is in-process storage, and multiple workers would split one user's conversation memory. **Conversation memory is not persistent**: chat history is empty after a process restart. Free hosting platforms' **disks are generally ephemeral** and the services sleep, so business data must live in Neon.
- **Not doing**: **this round does not do** direct player chat, automatic WeChat scraping or messaging, payment and order fulfilment, an admin back office, multiple workers, or rich-text rendering.
- **Migration and backup**: `python -m app.store.migrate` moves local Excel into Neon, and `python -m app.store.export` exports a tenant as **twelve xlsx files** (3 business tables + 9 sidecars) for backup / rollback.

Access flow, security quotas, local startup and the step-by-step deployment checklist are in [web app](docs/web.md) and [deployment](docs/deployment.md).

---

## Capability boundaries

| # | Boundary |
|---|---|
| 1 | **Input side**: no automatic WeChat group scraping; every input is pasted manually by the owner, avoiding WeChat bot risk controls |
| 2 | **Fulfilment side**: no payment, ordering or POS fulfilment; complementary to mature venue SaaS |
| 3 | **Interaction side**: players never talk to the Agent directly; information is relayed by the owner |
| 4 | **Modality side**: text input only, no OCR |
| 5 | **Script library side**: no pinyin matching; only when normal matching is empty (or reduced to weak matches where the query contains a short script name) does it offer character-bigram candidates (tagged `suggested`, the owner must confirm); after confirmation it records exactly one "phrasing → public script" mapping (script name only). **It never writes to the script library automatically from search results**; the public library is read-only and **sessions never point at public rows** |
| 6 | **Channel-order side**: Dianping has no self-service public API for a single venue, so **no public pages are scraped** and no merchant back office is logged into; only manual export files are imported, with **no promise of real-time sync** |
| 7 | **Show-control side**: **the DM holds the highest authority over stages**, **the countdown only reminds and never advances**; hardware exists only as a **generic HTTP adapter contract** |
| 8 | **Client side**: Flutter is **not tested on device (the DM-side 10-step checklist in `mobile_app/README.md` is still pending)** and not published; Web is the desktop back office and fallback, and **degrades honestly** — `/api/app/*` requires a Bearer token, so business buttons render as "（请在 App 内操作）" text in a browser |

**Product shapes we do not build**: **no WeChat mini program** (Flutter cannot compile to one, and maintaining a second UI is out of scope this round; the Web fallback page still opens inside WeChat). Public-library collection is an offline operator action, **not a server-side daemon**.

---

## Risks and constraints

**Business error risk**: LLMs hallucinate and may misjudge script names, times or headcounts, producing wrong vacancy counts or even overselling. **Mitigation**: players never interact with the Agent directly, every message is forwarded by the owner, and **the owner is the first line of validation**.

**Liability boundary**: without human review, an Agent that auto-confirms bookings and promises discounts shifts fulfilment disputes onto the developer. **Mitigation**: the Agent is only a back-office assistant for the owner, **all business decisions and external promises are confirmed by the owner**; offline fulfilment belongs to the script-murder venue, the developer only supplies software tools and **bears no responsibility for offline operations or booking fulfilment**. If a player-facing entry point is ever opened, it must have a **human final-approval gate owned by the store manager**.

**Compliance constraints**: no WeChat auto-scraping and no auto-messaging bots. Input is pasted manually by the owner, and marketing copy must be reviewed by a human before publishing, to stay clear of IM platform bot risk controls.

**Permission design**: the owner-side Agent has high privileges (read/write sessions, see the break-even floor, price, write copy); the player-side Agent would have low privileges (look up vacancies, collect bookings, temporary pre-registration only — **cannot see the floor, cannot confirm bookings, cannot promise discounts**). The player side is not implemented. DM-side permissions and automation boundaries are in [DM show control](docs/dm-control.md).

---

## Roadmap

Everything below is **unimplemented** and only a direction.

1. **Player interaction (with a human review gate)** — players submit booking enquiries, the Agent collects information and holds seats temporarily (30 minutes), and the booking only counts once the owner approves. Open design item: releasing the 30-minute hold needs a scheduled component that the current tool set does not have; the DM countdown deliberately never advances and cannot be reused.
2. **Multimodal expansion** — OCR for venue posters.
3. **Multi-agent split** — extraction / decisions / retrieval / copy as separate agents, easier to iterate and test individually.
4. **Third-party venue SaaS integration** — push cleaned structured session data back into the venue's existing system via API.
5. **Merchant mobile dashboard** — the Web timeline and status boards are implemented; **the mobile drag-and-drop board is not**, since rescheduling is a low-frequency management action with high touch error cost, so it is deferred.
6. **Operations review reports** — per-session summaries after show control are implemented; cross-session weekly empty-session rate, discount usage and fill success rate **are still not**.
7. **Tiered threshold calibration** — the thresholds and cost parameters in tools 4 and 5 are all placeholders and must be calibrated by each venue with its own data. `normal_arrival_rate` should be calibrated first, because risk ranking depends on it. The structure is arguable, the numbers are not derivable; see [parameter calibration](docs/tuning.md).

---

## Documentation index

| File | Content |
|---|---|
| [docs/tools.md](docs/tools.md) | Full contracts, fields, error codes and known limits for the 12 tools |
| [docs/agent.md](docs/agent.md) | Agent assembly, write boundaries, CLI console arguments |
| [docs/web.md](docs/web.md) | Web access flow, data isolation, security quotas, local startup |
| [docs/deployment.md](docs/deployment.md) | Neon, Cloudflare Tunnel / Hong Kong lightweight / Hugging Face Spaces / Render, Excel→database migration, free-tier limits |
| [docs/scheduling.md](docs/scheduling.md) | Resource scheduling, session creation, channel orders (including Dianping export import), boards, review-before-write |
| [docs/dm-control.md](docs/dm-control.md) | Stage templates, show-control state machine, hardware degradation, Flutter dual-role client |
| [docs/public-library.md](docs/public-library.md) | Read/write bounds, collection and compliance boundaries of the public script library |
| [docs/verification.md](docs/verification.md) | Real-model verification records, Flutter verification status, browser acceptance matrix |
| [docs/development.md](docs/development.md) | Demo scripts, project layout, test conventions |
| [docs/tuning.md](docs/tuning.md) | Threshold calibration order and known deviations |

## Project layout

```text
app/
  agent.py      12 @tool wrappers, SYSTEM_PROMPT, build_agent / run_turn
  cli.py        owner-side interactive console
  tools/        extract / dedupe / expiry / risk / pricing / storage / copywriter / scripts
                / public_library / resources / scheduling / session_intake / intake / stages / dm_control
  integrations/ Dianping channel import, hardware skill contract, web search
  store/        Store interface, Excel/SQLite/Postgres implementations, tenant workspace, migration and export
  web/          web app: approval, per-email workspace, operations pages/APIs, DM APIs
mobile_app/     Flutter dual-role client source
scripts/        demo scripts 01–09 (02 rewrites data/) + offline public-library collection
tests/          pytest offline tests
docs/           detailed documentation listed in the index above
```