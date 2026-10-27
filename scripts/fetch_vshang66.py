#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""vshang66.com 列表页元数据采集脚本（离线开发工具，不进应用运行时/Docker）。

只抓 /juben/list_10_N.html 列表页的事实元数据（剧名/作者/标签/时长/人数/
收录时间/详情 URL），**绝不请求详情页**（详情为解析/真相/复盘剧透文，有版权）。
输出 JSON 数组，字段与 `python -m app.store.import_public_scripts` 对齐；
origin=crawler:vshang66:<详情URL> 逐条溯源。

合规定位（运营方决策，代码承载边界）：单店内部运营参考、事实元数据、
非商用转售、不对外提供数据服务。礼貌与反封锁硬规则：
  - 每轮运行开头重新拉取 robots.txt；/juben/ 一旦被 Disallow 立即停；
  - 收到 403/429/503 当场停止并如实报告，不换 UA、不换 IP、不绕过；
  - 5xx/网络错误单页退避重试最多 2 次，仍失败跳过该页记 warning，本轮不推进
    水位线；连续 3 页失败提前终止（已采数据照常写出、非零退出）；
  - 首个 200 页解析不出条目视为结构异常，终止且不覆盖已有归档；
  - 串行 GET，默认间隔 1.0-1.5 秒（可调）；robots.txt 未声明 Crawl-delay，未做解析。

用法：
  python scripts/fetch_vshang66.py                     # 全量发现（直到空页）
  python scripts/fetch_vshang66.py --refresh           # 增量：前几页 + 水位线停
  python scripts/fetch_vshang66.py --max-pages 3 --delay 1.5 --out data/x.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx

BASE = "http://www.vshang66.com"
LIST_URL = BASE + "/juben/list_10_{page}.html"
ROBOTS_URL = BASE + "/robots.txt"
# 诚实 UA：说明用途与来源类型，不伪装成浏览器爬虫。
USER_AGENT = (
    "Mozilla/5.0 (compatible; JubenStorePublicLib/1.0; "
    "internal ops metadata reference; +contact via site owner)"
)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "data" / "public_scripts_vshang66.json"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "vshang66_state.json"
STOP_STATUS = {403, 429, 503}  # 被拒即停，不绕过
# 块内无嵌套 div，第一个 </div> 即块结束（</dl> 后紧跟 </div><div class="book">）。
BOOK_RE = re.compile(r'<div class="book">(.*?)</div>', re.S)

# 站方标签混有世界观（现代/古风/架空）与玩法流派；script_type 优先取玩法流派，
# 没有流派标签时退回第一个标签。全部标签原样并入 notes。
GENRE_TAGS = {
    "推理", "情感", "欢乐", "机制", "恐怖", "阵营", "悬疑", "惊悚",
    "还原", "硬核", "本格", "变格", "新本格", "博弈", "开放", "沉浸",
}


def pick_script_type(tags: list[str]) -> str:
    for tag in tags:
        if tag in GENRE_TAGS:
            return tag
    return tags[0] if tags else ""


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ").strip()


def _dd(block: str, css_class: str) -> str:
    m = re.search(rf'<dd class="{css_class}">(.*?)</dd>', block, re.S)
    return _strip_tags(m.group(1)) if m else ""


def parse_book_block(block: str) -> dict | None:
    """单个 .book 块 → 导入 CLI schema 对齐的记录；解析失败返回 None。"""
    name_m = re.search(r"<dt><a[^>]*>(.*?)</a></dt>", block, re.S)
    url_m = re.search(r'<dt><a href="([^"]+)"', block)
    if not name_m:
        return None
    name = _strip_tags(name_m.group(1))
    detail_url = urljoin(BASE, url_m.group(1)) if url_m else ""

    author = _dd(block, "zz").removeprefix("作者：").strip()
    tags_raw = _dd(block, "zb").removeprefix("状态：").strip()
    # lx 出现两次（时长/角色），需分别取出。
    lx_parts = re.findall(r'<dd class="lx">(.*?)</dd>', block, re.S)
    lx_texts = [_strip_tags(x) for x in lx_parts]
    duration = next((t.removeprefix("时长：").strip() for t in lx_texts if t.startswith("时长")), "")
    seats = next((t.removeprefix("角色：").strip() for t in lx_texts if t.startswith("角色")), "")
    collected = _dd(block, "sj").removeprefix("收录时间：").strip()

    tags = [t for t in re.split(r"[\s/、]+", tags_raw) if t]
    notes_bits = []
    if tags:
        notes_bits.append("标签：" + "/".join(tags))
    if author:
        notes_bits.append("作者：" + author)

    return {
        "script_name": name,
        "script_type": pick_script_type(tags),
        "duration": duration,          # 原文如 "7-8小时"，导入 CLI 容错解析
        "seats": seats,                # 原文如 "6人（3男3女）"，导入 CLI 容错解析
        "notes": "；".join(notes_bits),
        "collected_at": collected,
        "origin": f"crawler:vshang66:{detail_url}",
    }


def parse_list_page(html: str) -> list[dict]:
    records = []
    for block in BOOK_RE.findall(html):
        record = parse_book_block(block)
        if record and record["script_name"]:
            records.append(record)
    return records


def check_robots(client: httpx.Client) -> tuple[bool, str]:
    """返回 (是否允许抓 /juben/, 说明)。robots 拉取失败时按允许处理并告警。

    robots 标准是路径前缀匹配：`Disallow: /` 即整站禁止，自然涵盖 /juben/。
    以真实要请求的列表页路径为判定目标；空 Disallow 表示不限制。
    """
    try:
        resp = client.get(ROBOTS_URL, timeout=15.0)
    except httpx.HTTPError as exc:
        return True, f"robots.txt 拉取失败（{type(exc).__name__}），按允许继续并告警"
    if resp.status_code != 200:
        return True, f"robots.txt 返回 {resp.status_code}，按允许继续并告警"
    target = "/juben/list_10_1.html"  # 代表我们真正请求的列表页路径
    for line in resp.text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("disallow:"):
            continue
        path = stripped.split(":", 1)[1].strip()
        if not path:
            continue  # 空 Disallow = 不禁止任何路径
        prefix = path.rstrip("*$")  # 粗略去尾部通配，按字面前缀匹配
        if target.startswith(prefix):
            return False, f"robots.txt 已 Disallow {path}，停止采集"
    return True, "robots.txt 未屏蔽 /juben/"


def fetch_page(client: httpx.Client, page: int) -> tuple[int, str]:
    """抓一页；403/429/503 直接抛 SystemExit，5xx/网络错误退避重试 2 次后返回空页。

    返回 (status, html)：网络错误重试用尽时 status=0、html 空——与 5xx 一致
    由调用方跳页处理，不再 SystemExit 丢弃整轮已采数据。
    """
    url = LIST_URL.format(page=page)
    last_status = 0
    for attempt in range(3):
        try:
            resp = client.get(url, timeout=20.0)
        except httpx.HTTPError as exc:
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            print(f"! 第 {page} 页网络错误（{type(exc).__name__}），重试已用尽，跳过该页。")
            return 0, ""
        last_status = resp.status_code
        if resp.status_code in STOP_STATUS:
            print(
                f"! 第 {page} 页返回 HTTP {resp.status_code}——被站点拒绝，当场停止。"
                "不换 UA、不换 IP、不绕过；请人工评估后再恢复。"
            )
            raise SystemExit(2)
        if 200 <= resp.status_code < 300:
            return resp.status_code, resp.text
        if resp.status_code >= 500 and attempt < 2:
            backoff = 2.0 * (attempt + 1)
            print(f"~ 第 {page} 页 HTTP {resp.status_code}，{backoff:.0f}s 后重试（{attempt + 1}/2）")
            time.sleep(backoff)
            continue
        break
    print(f"! 第 {page} 页 HTTP {last_status}，重试后仍失败，跳过该页。")
    return last_status, ""


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _date_key(record: dict) -> str:
    """收录时间 YYYY-MM-DD；解析失败返回空串（不参与水位线比较）。"""
    text = (record.get("collected_at") or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vshang66 列表页元数据采集（离线工具）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 JSON 路径")
    parser.add_argument("--max-pages", type=int, default=0, help="页数上限（0=不限；refresh 默认 5）")
    parser.add_argument("--delay", type=float, default=0.0, help="请求间隔秒（0=随机 1.0-1.5）")
    parser.add_argument("--refresh", action="store_true", help="增量模式：只抓前几页，比对水位线")
    parser.add_argument("--since-page", type=int, default=1, help="从第 N 页开始（续页）")
    args = parser.parse_args(argv)

    max_pages = args.max_pages or (5 if args.refresh else 0)
    state = load_state()
    watermark = (state.get("last_collected_date") or "").strip()
    if args.refresh and not watermark:
        print("~ 无水位线，refresh 按全量前 5 页执行；之后水位线会建立。")

    collected: list[dict] = []
    seen_urls: set[str] = set()
    failed_pages: list[int] = []
    consecutive_failed = 0
    page = max(1, args.since_page)
    pages_fetched = 0
    max_date = watermark

    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        allowed, robots_note = check_robots(client)
        print(robots_note)
        if not allowed:
            return 2

        while True:
            if max_pages and pages_fetched >= max_pages:
                print(f"~ 已达 --max-pages={max_pages}，停止。")
                break
            status, html = fetch_page(client, page)
            pages_fetched += 1
            if not html:
                if status >= 500 or status == 0:
                    # 5xx / 网络错误重试用尽：真·跳过该页继续，不静默终止整轮；
                    # 记入 failed_pages，本轮末尾不推进水位线（见下）。
                    failed_pages.append(page)
                    consecutive_failed += 1
                    label = "网络错误" if status == 0 else f"HTTP {status}"
                    print(
                        f"~ 第 {page} 页 {label}，跳过该页继续"
                        "（本轮不推进水位线，稍后重跑会补抓）。"
                    )
                    if consecutive_failed >= 3:
                        print("! 连续 3 页失败，判定本轮不可用，提前终止（已采数据照常写出）。")
                        break
                    page += 1
                    pause = args.delay if args.delay > 0 else random.uniform(1.0, 1.5)
                    time.sleep(pause)
                    continue
                # 非 5xx 空响应（如 404）：列表尽头，正常结束。
                break
            records = parse_list_page(html)
            if not records:
                if not collected:
                    # 一条都没解析出来就遇到 200 空页：站点结构变化或维护页，
                    # 不能当作「发现结束」——记失败，写出守卫会保留旧归档。
                    failed_pages.append(page)
                    print(
                        f"! 第 {page} 页 200 但解析不出条目（结构变化或维护页？），"
                        "终止本轮；请人工检查站点页面结构。"
                    )
                    break
                print(f"第 {page} 页无条目（{status}），发现结束。")
                break
            consecutive_failed = 0

            stop_here = False
            for record in records:
                url = record["origin"]
                if url in seen_urls:
                    continue
                if args.refresh and watermark:
                    key = _date_key(record)
                    # 列表按收录时间倒序：严格早于水位线才停。边界日
                    # （== 水位线）条目继续收——站方当日新增会晚于上一轮
                    # 采集；导入端幂等 upsert，重复收集当日条目无害。
                    if key and key < watermark:
                        stop_here = True
                        break
                seen_urls.add(url)
                collected.append(record)
                key = _date_key(record)
                if key and key > max_date:
                    max_date = key
            print(f"第 {page} 页：{len(records)} 条，累计新增 {len(collected)} 条。")
            if stop_here:
                print(f"~ 增量命中水位线（{watermark}），停止。")
                break
            page += 1
            pause = args.delay if args.delay > 0 else random.uniform(1.0, 1.5)
            time.sleep(pause)

    if collected or not failed_pages:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_name(args.out.name + ".tmp")
        tmp.write_text(json.dumps(collected, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(args.out)
    else:
        # 失败轮且 0 条：保留已有归档不覆盖（否则维护页/结构变化会把
        # 上千条的全量产物静默清空）；报警靠下方非零退出码。
        print(f"~ 本轮失败且 0 条新记录，保留已有输出文件不覆盖：{args.out}")

    # 水位线只在本轮成功写出且无失败页时推进；有失败页则保持原值，
    # 下次 --refresh 会重走（导入端 name_key 幂等 upsert，重复无害）。
    if failed_pages:
        new_watermark = watermark
        print(
            f"! 本轮有 {len(failed_pages)} 页失败（页码 {','.join(map(str, failed_pages))}），"
            f"水位线保持 {new_watermark or '(空)'}；请重跑本轮补抓。"
        )
    else:
        new_watermark = max_date or watermark
    save_state(
        {
            "last_collected_date": new_watermark,
            "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pages_fetched": pages_fetched,
            "items_written": len(collected),
            "failed_pages": failed_pages,
            "output": str(args.out),
        }
    )
    print(
        f"完成：{pages_fetched} 页（失败 {len(failed_pages)} 页），写出 {len(collected)} 条 → {args.out}；"
        f"水位线 {new_watermark or '(空)'}。"
    )
    print("下一步：python -m app.store.import_public_scripts <该 JSON> --dry-run 预览后正式导入。")
    # 有失败页以非零码退出，让 refresh_public_library.bat / schtasks 日志能看到；
    # 已写出的 JSON 只含成功页，重跑会整份覆盖（水位线未推进，数据不丢）。
    return 1 if failed_pages else 0


if __name__ == "__main__":
    sys.exit(main())
