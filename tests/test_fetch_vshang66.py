"""scripts/fetch_vshang66.py 的离线测试：robots 前缀匹配与失败页处理。

全部离线：httpx.Client 被替换为假客户端，sleep 被打桩；
不发任何真实网络请求。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_vshang66.py"
_spec = importlib.util.spec_from_file_location("fetch_vshang66", _SCRIPT)
fetch = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("fetch_vshang66", fetch)
_spec.loader.exec_module(fetch)


def _robots(text: str, status: int = 200):
    """check_robots 用的假客户端：只服务 robots.txt。"""
    class _Client:
        def get(self, url, timeout=None):
            return SimpleNamespace(status_code=status, text=text)

    return _Client()


def _book_block(name: str, url: str, date: str) -> str:
    return (
        '<div class="book"><dl>'
        f'<dt><a href="{url}">{name}</a></dt>'
        '<dd class="zz">作者：测试工作室</dd>'
        '<dd class="zb">状态：现代/情感</dd>'
        '<dd class="lx">时长：4-5小时</dd>'
        '<dd class="lx">角色：6人（3男3女）</dd>'
        f'<dd class="sj">收录时间：{date}</dd>'
        "</dl></div>"
    )


def _list_page(*books: str) -> str:
    return "<html><body>" + "".join(books) + "</body></html>"


class TestCheckRobots:
    def test_整站_disallow_根路径即停(self):
        allowed, note = fetch.check_robots(_robots("User-agent: *\nDisallow: /\n"))
        assert allowed is False
        assert "停止采集" in note

    def test_disallow_juben_带尾斜杠即停(self):
        allowed, _ = fetch.check_robots(_robots("User-agent: *\nDisallow: /juben/\n"))
        assert allowed is False

    def test_disallow_juben_不带尾斜杠即停(self):
        allowed, _ = fetch.check_robots(_robots("User-agent: *\nDisallow: /juben\n"))
        assert allowed is False

    def test_disallow_列表页前缀即停(self):
        allowed, _ = fetch.check_robots(_robots("User-agent: *\nDisallow: /juben/list_\n"))
        assert allowed is False

    def test_无关路径放行(self):
        allowed, note = fetch.check_robots(_robots("User-agent: *\nDisallow: /admin/\n"))
        assert allowed is True
        assert "未屏蔽" in note

    def test_相似前缀不误伤(self):
        # /jubenx 不是列表页路径的前缀，不应误停。
        allowed, _ = fetch.check_robots(_robots("User-agent: *\nDisallow: /jubenx\n"))
        assert allowed is True

    def test_空_disallow_放行(self):
        allowed, _ = fetch.check_robots(_robots("User-agent: *\nDisallow:\nDisallow: /tmp/\n"))
        assert allowed is True

    def test_无规则放行(self):
        allowed, _ = fetch.check_robots(_robots("User-agent: *\nAllow: /\n"))
        assert allowed is True


def _install_fake(fetch_mod, monkeypatch, tmp_path, pages, *, robots="User-agent: *\nDisallow:\n"):
    """pages: {页码: 记录列表[(name, url, date)] | "404" | "500"}。"""
    def _get(url, timeout=None):
        if url == fetch_mod.ROBOTS_URL:
            return SimpleNamespace(status_code=200, text=robots)
        match = re.search(r"list_10_(\d+)\.html", url)
        page = int(match.group(1))
        spec = pages.get(page)
        if spec == "404":
            return SimpleNamespace(status_code=404, text="")
        if spec == "500":
            return SimpleNamespace(status_code=500, text="")
        html = _list_page(
            *(_book_block(name, item_url, date) for name, item_url, date in spec)
        )
        return SimpleNamespace(status_code=200, text=html)

    class _Client:
        def __init__(self, **kwargs):
            pass

        def get(self, url, timeout=None):
            return _get(url, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(fetch_mod.httpx, "Client", _Client)
    monkeypatch.setattr(fetch_mod, "STATE_PATH", tmp_path / "vshang66_state.json")
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda *_: None)


def _state(tmp_path) -> dict:
    return json.loads((tmp_path / "vshang66_state.json").read_text(encoding="utf-8"))


class TestMain失败页与水位线:
    def test_5xx页真跳过_水位线不推进_非零退出(self, tmp_path, monkeypatch):
        _install_fake(fetch, monkeypatch, tmp_path, {
            1: [("本一", "/6p/101.html", "2026-10-21")],
            2: "500",
            3: [("本三", "/6p/103.html", "2026-10-22")],
            4: "404",  # 列表尽头
        })
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--max-pages", "10",
            "--delay", "0.01",
        ])
        assert rc == 1  # 有失败页 → 非零码，bat / schtasks 日志可见
        state = _state(tmp_path)
        assert state["last_collected_date"] == ""  # 水位线不推进
        assert state["failed_pages"] == [2]
        out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert [item["script_name"] for item in out] == ["本一", "本三"]

    def test_无失败页_水位线推进_零退出(self, tmp_path, monkeypatch):
        _install_fake(fetch, monkeypatch, tmp_path, {
            1: [("本一", "/6p/101.html", "2026-10-21")],
            2: [("本二", "/6p/102.html", "2026-10-22")],
            3: "404",
        })
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--max-pages", "10",
            "--delay", "0.01",
        ])
        assert rc == 0
        state = _state(tmp_path)
        assert state["last_collected_date"] == "2026-10-22"
        assert state["failed_pages"] == []

    def test_refresh_失败页保持旧水位线(self, tmp_path, monkeypatch):
        (tmp_path / "vshang66_state.json").write_text(
            json.dumps({"last_collected_date": "2026-10-20"}), encoding="utf-8"
        )
        _install_fake(fetch, monkeypatch, tmp_path, {
            1: [("新本", "/6p/201.html", "2026-10-22")],
            2: "500",
            3: [("旧本", "/6p/199.html", "2026-10-19")],  # ≤ 水位线 → 增量停
            4: "404",
        })
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--refresh",
            "--delay", "0.01",
        ])
        assert rc == 1
        state = _state(tmp_path)
        # 失败页存在：即使抓到了更新的收录时间，水位线也保持旧值，
        # 下次 refresh 重走补抓（导入端幂等 upsert）。
        assert state["last_collected_date"] == "2026-10-20"
        assert state["failed_pages"] == [2]

    def test_robots_屏蔽即停不请求列表页(self, tmp_path, monkeypatch):
        hits: list[str] = []

        def _get(url, timeout=None):
            hits.append(url)
            return SimpleNamespace(
                status_code=200, text="User-agent: *\nDisallow: /\n"
            )

        class _Client:
            def __init__(self, **kwargs):
                pass

            def get(self, url, timeout=None):
                return _get(url, timeout=timeout)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(fetch.httpx, "Client", _Client)
        monkeypatch.setattr(fetch, "STATE_PATH", tmp_path / "vshang66_state.json")
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--max-pages", "10",
            "--delay", "0.01",
        ])
        assert rc == 2
        assert hits == [fetch.ROBOTS_URL]  # 只碰了 robots，列表页零请求

    def test_网络错误页与5xx对称跳过_已采数据照常写出(self, tmp_path, monkeypatch):
        """网络错误重试用尽不再 SystemExit 丢整轮：跳页、写数据、非零退出。"""
        def _get(url, timeout=None):
            if url == fetch.ROBOTS_URL:
                return SimpleNamespace(status_code=200, text="User-agent: *\nDisallow:\n")
            page = int(re.search(r"list_10_(\d+)\.html", url).group(1))
            if page == 1:
                html = _list_page(_book_block("本一", "/6p/101.html", "2026-10-22"))
                return SimpleNamespace(status_code=200, text=html)
            if page == 2:
                raise httpx.ConnectError("boom")
            return SimpleNamespace(status_code=404, text="")

        class _Client:
            def __init__(self, **kwargs):
                pass

            def get(self, url, timeout=None):
                return _get(url, timeout=timeout)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(fetch.httpx, "Client", _Client)
        monkeypatch.setattr(fetch, "STATE_PATH", tmp_path / "vshang66_state.json")
        monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--max-pages", "10",
            "--delay", "0.01",
        ])
        assert rc == 1
        out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert [item["script_name"] for item in out] == ["本一"]  # 已采数据未丢
        state = _state(tmp_path)
        assert state["failed_pages"] == [2]
        # 有失败页 → 水位线保持旧值（空），不推进：
        assert state["last_collected_date"] == ""

    def test_首200页解析不出条目_保留旧归档非零退出(self, tmp_path, monkeypatch):
        """维护页/结构变化不得把已有全量归档静默清空。"""
        out = tmp_path / "out.json"
        out.write_text(json.dumps([{"script_name": "旧档"}]), encoding="utf-8")
        _install_fake(fetch, monkeypatch, tmp_path, {
            1: [],  # 200 但无 .book 块
        })
        rc = fetch.main([
            "--out", str(out),
            "--max-pages", "10",
            "--delay", "0.01",
        ])
        assert rc == 1
        kept = json.loads(out.read_text(encoding="utf-8"))
        assert kept == [{"script_name": "旧档"}]  # 归档未被 [] 覆盖

    def test_连续3页失败提前熔断(self, tmp_path, monkeypatch):
        _install_fake(fetch, monkeypatch, tmp_path, {
            1: "500", 2: "500", 3: "500", 4: [("本四", "/6p/104.html", "2026-10-22")],
        })
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--max-pages", "10",
            "--delay", "0.01",
        ])
        assert rc == 1
        state = _state(tmp_path)
        assert state["failed_pages"] == [1, 2, 3]
        assert state["pages_fetched"] == 3  # 熔断后不再请求第 4 页

    def test_refresh_边界日条目不再永久漏采(self, tmp_path, monkeypatch):
        """key == 水位线的条目继续收集（站方当日新增），key < 水位线才停。"""
        (tmp_path / "vshang66_state.json").write_text(
            json.dumps({"last_collected_date": "2026-10-20"}), encoding="utf-8"
        )
        _install_fake(fetch, monkeypatch, tmp_path, {
            1: [
                ("当日新上", "/6p/201.html", "2026-10-22"),
                ("边界日补录", "/6p/202.html", "2026-10-20"),  # == 水位线，须收
            ],
            2: [("旧本", "/6p/199.html", "2026-10-19")],  # < 水位线 → 停
            3: "404",
        })
        rc = fetch.main([
            "--out", str(tmp_path / "out.json"),
            "--refresh",
            "--delay", "0.01",
        ])
        assert rc == 0
        out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        names = [item["script_name"] for item in out]
        assert "边界日补录" in names  # 漏采修复的直接锚点
        state = _state(tmp_path)
        assert state["last_collected_date"] == "2026-10-22"
