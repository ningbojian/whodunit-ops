# 公共剧本库

所有商家共享一份只读公共库（`data/public_scripts.xlsx`；数据库模式为 `public_scripts` 表，无 `tenant_id`），商家各自维护私有库；同名时私有行遮蔽公共行。

## 读路径

公共库已接入三处匹配点：

1. `script_library` 的 `search` / `get` / `list`；
2. `session_intake` 的类型 / 人数 / 价格预填；
3. `scheduling` 的时长来源。

## 写路径

写路径不变——`upsert` 永远只进私有库，`pub-` 前缀 id 直接拒绝（`public_readonly`）。**场次永不指向公共行**：命中公共库时预填类型 / 人数 / 价格，但不写 `script_id`，结果标 `script_source=公共库`。

## 策展种子

`data/public_scripts_seed.csv` 约 10 条真实剧本的事实元数据（`origin=策展`，与演示剧本零重名），空库首次启动自动灌入；清库后重灌是期望行为（守卫就是库本身的空 / 非空，无额外标记文件）。

## 数据来源：运营方离线动作，应用运行时不带爬虫

采集脚本 `scripts/fetch_vshang66.py` 是**开发工具**，不进 Docker 镜像、不进服务端定时任务。它只抓 vshang66.com 列表页的事实元数据：

- 抓什么：剧名 / 作者工作室 / 状态标签 / 时长 / 人数 / 收录时间 / 详情页 URL（作 `origin` 溯源）。
- 不抓什么：**不抓剧透详情页、不绕过访问控制**。
- 礼貌约束：收到 403/429/503 当场停止、robots.txt 变更即停、串行限速（默认 1.0–1.5 秒）。

首次全量约 5800+ 条已导入（2026-10-22）。定位：单店内部运营参考、事实元数据、逐条溯源、非商用转售——最终合规判断是运营方决策，代码只承载边界。

## 导入通道

```bash
python -m app.store.import_public_scripts <file.json|.csv> [--dry-run]
```

按 `name_key` 幂等合并（空值不覆盖、origin 变更追加保留），缺名称 / origin 的行进错误清单，不静默丢弃。导入后新开对话才生效（公共帧随 Agent 重建刷新）。

## 定时增量刷新（本机任务计划，可选）

`scripts/refresh_public_library.bat` 跑增量采集（只翻列表前几页、按收录时间水位线比对）→ 幂等导入 → 追加摘要到 `data/refresh_log.txt`（不含 key / `DATABASE_URL`）。

```bat
:: 每 3 天 03:30 跑一次；把 <仓库根目录> 换成你的实际路径
schtasks /create /tn "jbs_public_library_refresh" /sc DAILY /mo 3 /st 03:30 /tr "<仓库根目录>\scripts\refresh_public_library.bat"
schtasks /delete /tn "jbs_public_library_refresh" /f
```

定时任务只在本机启用期间有效——本机没开就不会刷新。它**不做成服务端常驻任务**：部署到 Render 的公网实例不带爬虫、不带定时采集。

## 合规边界

- 公共库对商家只读，商家不能改、不能删。
- 采集是运营方的离线动作，不是产品运行时能力。
- 搜索工具（`web_search`）的结果**绝不根据搜索结果自动写入剧本库**，必须由店长确认全名后走 `script_library(upsert)`。
