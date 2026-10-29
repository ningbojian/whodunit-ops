"""Web 数据库后端集成：登录、审批、聊天、快照、隔离与重启持久化。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.web.main import create_app
from app.web.mailer import RecordingEmailSender
from app.web.models import StaticModelProvider
from tests.test_agent import ScriptedChatModel
from tests.web_helpers import (
    MutableClock,
    csrf_from,
    fully_authorize,
    make_settings,
)


def build_database_app(tmp_path, clock, responses, **overrides):
    settings = make_settings(
        tmp_path,
        storage_backend="database",
        database_sqlite_path=tmp_path / "web.sqlite3",
        **overrides,
    )
    model = ScriptedChatModel(responses=responses)
    app = create_app(
        settings=settings,
        mailer=RecordingEmailSender(),
        model_provider=StaticModelProvider(model),
        now_fn=clock.now,
    )
    return app, model


def _csrf(client) -> str:
    return csrf_from(client.get("/").text)


def test_database_chat_and_snapshot_without_excel(tmp_path):
    clock = MutableClock()
    app, _ = build_database_app(
        tmp_path, clock, [AIMessage(content="数据库版回复")]
    )

    with TestClient(app) as client:
        assert app.state.storage_backend == "database"
        assert client.get("/healthz").status_code == 200
        fully_authorize(client, app.state.mailer)

        response = client.post(
            "/api/chat",
            json={"message": "你好"},
            headers={
                "X-CSRF-Token": _csrf(client),
                "Accept": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["answer"] == "数据库版回复"

        page = client.get("/")
        assert page.status_code == 200
        assert "雾隐山庄" in page.text

    assert not list((tmp_path / "web_users").rglob("*.xlsx"))


def test_healthz_uses_a_single_database_round_trip(tmp_path):
    """健康检查不能把同一个引擎连续 ping 四遍。

    跨境部署时每条 SQL 都是一次 55ms 量级的往返，而且每次 ``engine.connect()``
    还要多付 pre-ping 和归还时的 ROLLBACK。原先 /healthz 在同一引擎上做了
    usage/access/ping/schema 四次探测，实测耗时 0.82 秒。合并成一条查询后
    只应剩 pre-ping 和这条查询本身。
    """
    from sqlalchemy import event

    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [])
    engine = app.state.engine

    statements: list[str] = []
    event.listen(
        engine, "before_cursor_execute", lambda *args: statements.append(args[2])
    )

    with TestClient(app) as client:
        statements.clear()
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["db"] is True
    assert body["schema"] is True
    # pre-ping 一条 + 真正的 schema 查询一条。超过两条就说明又在重复探测。
    assert len(statements) <= 2, statements


def test_runs_page_loads_each_table_once(tmp_path):
    """场控看板不能对每条 run 重读整张表。

    ``run_view`` 原先对每条 run 调 ``load_sessions_with_counts`` 和
    ``load_rooms``，一天 N 场就多出 4N 次串行查询；跨境部署每条 55ms。
    上游 ``list_runs_today`` 现在传入共享的 ``RunLookups``，每张表只读一次。
    """
    from sqlalchemy import event

    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [])

    statements: list[str] = []
    event.listen(
        app.state.engine,
        "before_cursor_execute",
        lambda *args: statements.append(args[2]),
    )

    def count(query_target: str) -> int:
        return sum(
            1
            for sql in statements
            if query_target in sql and sql.lstrip().upper().startswith("SELECT")
        )

    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        client.get("/runs")  # 首访会灌演示数据，不计入
        statements.clear()
        assert client.get("/runs").status_code == 200

    # 每张表最多读一次；超过就说明 N+1 回来了。
    assert count("FROM sessions") <= 1, statements
    assert count("FROM rooms") <= 1, statements
    assert count("FROM session_assignments") <= 1, statements
    assert count("FROM scripts") <= 1, statements


def test_schedule_page_loads_each_table_once(tmp_path):
    """排期首屏不能对同一张表重复读，也不能让浏览器再补一次请求。

    ``schedule_view`` 原先在同一个请求里把 rooms/dms/scripts/sessions 各读
    两到三遍（``list_schedule`` 自己读一遍，调用方再读一遍），实测 17 条。
    共享 ``ScheduleContext`` 后每张表只读一次。首屏数据已经随 HTML 下发，
    所以这里统计的就是首屏的全部查询。
    """
    from sqlalchemy import event

    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [])

    statements: list[str] = []
    event.listen(
        app.state.engine,
        "before_cursor_execute",
        lambda *args: statements.append(args[2]),
    )

    def count(query_target: str) -> int:
        return sum(
            1
            for sql in statements
            if query_target in sql and sql.lstrip().upper().startswith("SELECT")
        )

    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        client.get("/schedule")  # 首访会灌演示数据，不计入
        statements.clear()
        assert client.get("/schedule").status_code == 200

    for table in (
        "FROM sessions",
        "FROM rooms",
        "FROM dms",
        "FROM session_assignments",
        "FROM scripts",
        "FROM dm_leaves",
        "FROM dm_runs",
    ):
        assert count(table) <= 1, (table, statements)
    # 登录态两条（access_requests、tenants）加上面七条，再加 bookings、
    # dm_shifts、public_scripts 三条，一共 12 条；多出来就是重复读回来了。
    assert len(statements) <= 12, statements


def test_public_scripts_are_read_once_across_requests(tmp_path):
    """公共库帧要跨请求缓存，不能每个页面都整表重读。

    公共库是只读、全局共享的数据，``load()`` 每次都会 ``SELECT`` 全部
    5800+ 行并重建 DataFrame。原先 ``_public_scripts_frame`` 每个请求都
    调一次，``_merged_scripts`` 在多个视图里都会走到。OperationsService
    与 ChatRuntime 现在共用一个 ``PublicScriptsCache``。
    """
    from sqlalchemy import event

    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [])

    statements: list[str] = []
    event.listen(
        app.state.engine,
        "before_cursor_execute",
        lambda *args: statements.append(args[2]),
    )

    def public_reads() -> int:
        return sum(
            1
            for sql in statements
            if "FROM public_scripts" in sql and sql.lstrip().upper().startswith("SELECT")
        )

    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        client.get("/resources")  # 首访可能触发种子，不计入
        statements.clear()
        # 连续两个请求都读公共库的页面。
        assert client.get("/resources").status_code == 200
        assert client.get("/schedule").status_code == 200
        reads = public_reads()

    # 第二次请求应当全部命中缓存；允许首访那一次没被清干净，所以上限是 1。
    assert reads <= 1, statements


def test_approval_email_does_not_block_the_event_loop(tmp_path):
    """async 路由里的同步发信必须丢进线程池。

    ``auth_request`` 是 ``async def``，而 ``mailer.send`` 内部是同步
    ``httpx.post``（超时 10 秒）。直接调用会卡住事件循环，单 worker 下
    所有并发请求一起排队——两封邮件就要 2 秒，而不是 1 秒。

    注意必须用同一个事件循环并发：每个线程各起一个 ``TestClient`` 会
    各自跑在独立循环里，阻塞不会互相影响，那样测不出问题。
    """
    import asyncio
    import time

    import httpx

    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [])

    class SlowMailer:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, message) -> None:
            self.calls += 1
            time.sleep(1.0)

    app.state.mailer = SlowMailer()

    async def scenario() -> tuple[list[int], float]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            async def request_one(index: int) -> int:
                response = await client.post(
                    "/api/app/auth/request",
                    json={
                        "email": f"concurrent{index}@example.com",
                        "display_name": "并发测试",
                    },
                )
                return response.status_code

            started = time.monotonic()
            codes = await asyncio.gather(request_one(0), request_one(1))
            return list(codes), time.monotonic() - started

    codes, elapsed = asyncio.run(scenario())

    assert all(code in {201, 400, 409, 429} for code in codes), codes
    if all(code == 201 for code in codes):
        assert app.state.mailer.calls == 2
        # 两封邮件各睡 1 秒。串行（阻塞事件循环）至少 2 秒；
        # 丢进线程池后重叠，约 1 秒。
        assert elapsed < 1.8, f"发信阻塞了事件循环：{elapsed:.2f}s"


def test_two_emails_are_isolated_in_database_mode(tmp_path):
    clock = MutableClock()
    app, _ = build_database_app(
        tmp_path,
        clock,
        [AIMessage(content="甲的回复"), AIMessage(content="乙的回复")],
    )

    with TestClient(app) as client:
        first_mailer = app.state.mailer
        fully_authorize(client, first_mailer, email="first@example.com")
        first_page = client.get("/").text

        # 换第二个邮箱：清掉第一个会话的 cookie，再走完整审批。
        client.cookies.clear()
        fully_authorize(client, app.state.mailer, email="second@example.com")
        second_page = client.get("/").text

        assert "first@example.com" not in second_page
        assert "second@example.com" not in first_page
        assert first_page.count("雾隐山庄") >= 1
        assert second_page.count("雾隐山庄") >= 1


def test_database_state_survives_app_restart(tmp_path):
    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [AIMessage(content="第一次")])

    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        cookies = dict(client.cookies)
        assert client.get("/healthz").status_code == 200

    clock2 = MutableClock(clock.current)
    app2, _ = build_database_app(tmp_path, clock2, [AIMessage(content="重启后")])
    client2 = TestClient(app2)
    client2.cookies.update(cookies)

    with client2:
        page = client2.get("/")
        assert page.status_code == 200
        response = client2.post(
            "/api/chat",
            json={"message": "还在吗"},
            headers={
                "X-CSRF-Token": csrf_from(page.text),
                "Accept": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["answer"] == "重启后"


def _json_headers(client) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf_from(client.get("/schedule").text),
        "Accept": "application/json",
        "Origin": "http://testserver",
    }


def test_resources_session_stages_and_runs_survive_database_restart(tmp_path):
    clock = MutableClock()
    app, _ = build_database_app(tmp_path, clock, [AIMessage(content="第一次")])

    with TestClient(app) as client:
        fully_authorize(client, app.state.mailer)
        cookies = dict(client.cookies)
        headers = _json_headers(client)

        room = client.post(
            "/api/resources/rooms",
            json={"name": "持久房", "capacity": 8},
            headers=headers,
        )
        assert room.status_code == 200
        room_id = room.json()["room"]["room_id"]

        # 建场：硬必填（剧本全名 + 开场时间）齐全即 promoted，不走草稿。
        created = client.post(
            "/api/sessions",
            json={"script_name": "持久测试本", "start_time": "2026-10-23 19:30"},
            headers=headers,
        )
        assert created.status_code == 200
        assert created.json()["status"] == "promoted"
        session_id = created.json()["session"]["session_id"]

        stages = client.get("/api/stages").json()
        selected = stages["selected"]
        script_id = selected["script"]["script_id"]
        payload = [
            {
                "name": "持久第一幕" if index == 0 else stage["name"],
                "stage_type": stage.get("stage_type"),
                "duration_minutes": stage["duration_minutes"],
                "warn_before_seconds": stage.get("warn_before_seconds", 60),
                "cue_text": stage.get("cue_text"),
                "prompt_text": stage.get("prompt_text"),
                "active": True,
            }
            for index, stage in enumerate(selected["stages"])
        ]
        saved = client.post(
            "/api/stages",
            json={"script_id": script_id, "stages": payload},
            headers=headers,
        )
        assert saved.status_code == 200
        version = saved.json()["version"]

        schedule = client.get("/api/schedule").json()["sessions"]
        target = next(
            session
            for session in schedule
            if session["script_name"] == selected["script"]["script_name"]
        )
        run = client.post(
            "/api/runs",
            json={"session_id": target["session_id"], "mode": "software"},
            headers=headers,
        )
        assert run.status_code == 201
        run_id = run.json()["run"]["run_id"]

    clock2 = MutableClock(clock.current)
    app2, _ = build_database_app(tmp_path, clock2, [AIMessage(content="重启后")])
    client2 = TestClient(app2)
    client2.cookies.update(cookies)

    with client2:
        resources = client2.get("/api/resources").json()
        assert any(item["room_id"] == room_id for item in resources["rooms"])

        schedule = client2.get("/api/schedule").json()["sessions"]
        hits = [item for item in schedule if item["session_id"] == session_id]
        assert len(hits) == 1
        assert hits[0]["script_name"] == "持久测试本"

        restored = client2.get(f"/api/stages?script_id={script_id}").json()
        assert restored["selected"]["version"] == version
        assert restored["selected"]["stages"][0]["name"] == "持久第一幕"

        view = client2.get(f"/api/runs/{run_id}")
        assert view.status_code == 200
        assert view.json()["run"]["run_id"] == run_id
