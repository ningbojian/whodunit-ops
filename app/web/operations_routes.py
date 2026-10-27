"""商家侧页面与 JSON API（Cookie 会话）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.tools.dm_control import list_events
from app.web.auth import AccessContext, _render, require_access, require_csrf
from app.web.operations import OperationsError

router = APIRouter()


def _service(request: Request):
    return request.app.state.operations


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非法 JSON 统一 400
        raise OperationsError("bad_request", "请求体必须是 JSON。") from None
    if not isinstance(payload, dict):
        raise OperationsError("bad_request", "请求体必须是 JSON 对象。")
    return payload


def _ok(data: dict[str, Any] | None = None, *, status_code: int = 200) -> JSONResponse:
    payload: dict[str, Any] = {"ok": True}
    if data:
        payload.update(data)
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _remaining_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _page(
    request: Request,
    access: AccessContext,
    name: str,
    active: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    remaining = access.remaining_seconds(request.app.state.now_fn())
    return _render(
        request,
        name,
        {
            "access": access,
            "email_masked": access.email_masked,
            "active_page": active,
            "remaining_text": _remaining_text(remaining),
            "remaining_seconds": remaining,
            **context,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@router.get("/resources", response_class=HTMLResponse)
def resources_page(
    request: Request,
    access: AccessContext = Depends(require_access),
):
    data = _service(request).resources(access.email_hash)
    return _page(request, access, "resources.html", "resources", {"data": data})


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(
    request: Request,
    access: AccessContext = Depends(require_access),
    start: str | None = None,
    end: str | None = None,
):
    service = _service(request)
    data = service.schedule_view(access.email_hash, start, end)
    data["hardware_configured"] = bool(service._hardware().configured)
    return _page(request, access, "schedule.html", "schedule", {"data": data})


@router.get("/imports", response_class=HTMLResponse)
def imports_page(
    request: Request,
    access: AccessContext = Depends(require_access),
):
    data = _service(request).schedule_view(access.email_hash)
    return _page(request, access, "imports.html", "imports", {"data": data})


@router.get("/stages", response_class=HTMLResponse)
def stages_page(
    request: Request,
    access: AccessContext = Depends(require_access),
    script_id: str | None = None,
):
    data = _service(request).stages(access.email_hash, script_id)
    return _page(request, access, "stages.html", "stages", {"data": data})


@router.get("/runs", response_class=HTMLResponse)
def runs_page(
    request: Request,
    access: AccessContext = Depends(require_access),
    day: str | None = None,
):
    data = _service(request).runs_today(access.email_hash, day)
    return _page(request, access, "runs.html", "runs", {"data": data})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(
    request: Request,
    run_id: str,
    access: AccessContext = Depends(require_access),
):
    service = _service(request)
    view = service.run_view(access.email_hash, run_id)
    events = list_events(
        service._store(access.email_hash), run_id=run_id, include_payload=False
    )
    view["events"] = events[-20:]
    return _page(request, access, "run_detail.html", "runs", {"view": view})


@router.get("/resources/template/{kind}.csv")
def resource_template(
    request: Request,
    kind: str,
    access: AccessContext = Depends(require_access),
):
    filename, text = _service(request).resource_template(kind)
    return Response(
        content="﻿" + text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


@router.get("/api/resources")
def resources_api(
    request: Request,
    access: AccessContext = Depends(require_access),
):
    return _ok(_service(request).resources(access.email_hash))


@router.get("/api/scripts")
def list_scripts_api(
    request: Request,
    q: str = "",
    type: str = "",
    seats_min: str = "",
    seats_max: str = "",
    duration_min: str = "",
    duration_max: str = "",
    source: str = "",
    page: str = "1",
    page_size: str = "50",
    access: AccessContext = Depends(require_access),
):
    """资源页剧本筛选 + 分页（Web 专用；/api/app/resources 冻结不动）。"""
    return _ok(
        _service(request).list_scripts(
            access.email_hash,
            q=q,
            script_type=type,
            seats_min=seats_min,
            seats_max=seats_max,
            duration_min=duration_min,
            duration_max=duration_max,
            source=source,
            page=page,
            page_size=page_size,
        )
    )


@router.post("/api/resources/rooms")
async def save_room_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(_service(request).save_room(access.email_hash, payload))


@router.post("/api/resources/dms")
async def save_dm_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(_service(request).save_dm(access.email_hash, payload))


@router.post("/api/resources/dm-leaves")
async def save_dm_leave_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    """登记 DM 请假（幂等 by dm_id+day）。"""
    payload = await _body(request)
    return _ok(_service(request).save_dm_leave(access.email_hash, payload))


@router.delete("/api/resources/dm-leaves/{leave_id}")
def drop_dm_leave_api(
    request: Request,
    leave_id: str,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    return _ok(_service(request).drop_dm_leave(access.email_hash, leave_id))


@router.post("/api/resources/demo")
async def demo_resources_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(
        _service(request).seed_demo_resources(
            access.email_hash, confirm=bool(payload.get("confirm"))
        )
    )


@router.post("/api/resources/import")
async def import_resources_api(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    confirm: str = Form("false"),
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    limit = int(getattr(request.app.state.settings, "max_import_bytes", 2 * 1024 * 1024))
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise OperationsError("file_too_large", "文件超过上传大小限制。", 413)
    return _ok(
        _service(request).import_resources(
            access.email_hash,
            kind,
            file.filename or "upload.csv",
            content,
            confirm=str(confirm).casefold() in {"1", "true", "yes", "on"},
        )
    )


# ---------------------------------------------------------------------------
# 场次
# ---------------------------------------------------------------------------


@router.post("/api/sessions")
async def create_session_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(_service(request).create_session(access.email_hash, payload))


@router.patch("/api/sessions/{session_id}")
async def update_session_api(
    request: Request,
    session_id: str,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(
        _service(request).update_session(access.email_hash, session_id, payload)
    )


# ---------------------------------------------------------------------------
# 排期
# ---------------------------------------------------------------------------


@router.get("/api/schedule")
def schedule_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    start: str | None = None,
    end: str | None = None,
    include_scripts: str | None = None,
):
    # Web 看板刷新传 include_scripts=0：不序列化全量合并剧本库（scripts: []）。
    return _ok(
        _service(request).schedule_view(
            access.email_hash,
            start,
            end,
            include_scripts=include_scripts not in {"0", "false", "False", "no"},
        )
    )


@router.post("/api/schedule/plan")
async def schedule_plan_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(
        _service(request).plan_schedule(
            access.email_hash, payload.get("start"), payload.get("end")
        )
    )


def _preview_flag(payload: dict[str, Any]) -> bool:
    """preview 默认 True（保护未升级客户端）；显式 false 才进入写入模式。"""
    value = payload.get("preview", True)
    if isinstance(value, str):
        return value.casefold() not in {"0", "false", "no"}
    return bool(value)


@router.post("/api/schedule/auto-assign")
async def auto_assign_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    ids = payload.get("session_ids")
    if ids is not None and not isinstance(ids, list):
        raise OperationsError("bad_request", "session_ids 必须是数组。")
    preview = _preview_flag(payload)
    if not preview and not payload.get("confirm"):
        raise OperationsError(
            "confirm_required", "直接写入分配需要 confirm:true；建议先用预览审核。"
        )
    return _ok(
        _service(request).auto_assign(
            access.email_hash,
            [str(item) for item in ids] if ids else None,
            trigger=str(payload.get("trigger") or "web_auto"),
            preview=preview,
        )
    )


@router.post("/api/schedule/resolve")
async def resolve_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    session_id = str(payload.get("session_id") or "")
    choice = str(payload.get("choice") or "")
    if not session_id or not choice:
        raise OperationsError("bad_request", "需要 session_id 和 choice。")
    return _ok(
        _service(request).resolve_conflict(
            access.email_hash, session_id, choice, payload.get("target_id")
        )
    )


@router.post("/api/schedule/apply")
async def apply_schedule_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        raise OperationsError("bad_request", "assignments 必须是数组。")
    return _ok(
        _service(request).apply_schedule(
            access.email_hash,
            [item for item in assignments if isinstance(item, dict)],
            confirm=bool(payload.get("confirm")),
        )
    )


@router.post("/api/schedule/assign")
async def assign_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        raise OperationsError("bad_request", "需要 session_id。")
    return _ok(
        _service(request).assign_one(
            access.email_hash,
            session_id,
            room_id=payload.get("room_id"),
            dm_id=payload.get("dm_id"),
        )
    )


# ---------------------------------------------------------------------------
# 阶段模板
# ---------------------------------------------------------------------------


@router.get("/api/stages")
def stages_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    script_id: str | None = None,
):
    return _ok(_service(request).stages(access.email_hash, script_id))


@router.post("/api/stages")
async def save_stages_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    stages = payload.get("stages")
    if not isinstance(stages, list):
        raise OperationsError("bad_request", "stages 必须是数组。")
    script_id = str(payload.get("script_id") or "")
    if not script_id:
        raise OperationsError("bad_request", "需要 script_id。")
    return _ok(
        _service(request).save_stages(
            access.email_hash,
            script_id,
            [item for item in stages if isinstance(item, dict)],
            confirm=True,
        )
    )


# ---------------------------------------------------------------------------
# 场控（商家只读 + 发码）
# ---------------------------------------------------------------------------


@router.get("/api/runs")
def runs_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    day: str | None = None,
):
    return _ok(_service(request).runs_today(access.email_hash, day))


@router.post("/api/runs")
async def create_run_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        raise OperationsError("bad_request", "需要 session_id。")
    return _ok(
        _service(request).create_run(
            access.email_hash,
            session_id,
            dm_id=payload.get("dm_id") or None,
            mode=str(payload.get("mode") or "software"),
        ),
        status_code=201,
    )


@router.get("/api/runs/{run_id}")
def run_api(
    request: Request,
    run_id: str,
    access: AccessContext = Depends(require_access),
):
    service = _service(request)
    view = service.run_view(access.email_hash, run_id)
    view["events"] = list_events(
        service._store(access.email_hash), run_id=run_id, include_payload=False
    )[-50:]
    return _ok(view)


@router.post("/api/runs/{run_id}/code")
async def run_code_api(
    request: Request,
    run_id: str,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    payload = await _body(request)
    return _ok(
        _service(request).issue_run_code(
            access.email_hash,
            run_id,
            dm_id=payload.get("dm_id") or None,
            dm_name=payload.get("dm_name") or None,
        )
    )


@router.post("/api/runs/{run_id}/revoke")
async def run_revoke_api(
    request: Request,
    run_id: str,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    return _ok(_service(request).revoke_run_token(access.email_hash, run_id))


@router.post("/api/runs/{run_id}/abandon")
async def run_abandon_api(
    request: Request,
    run_id: str,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    return _ok(_service(request).abandon_run(access.email_hash, run_id))


@router.get("/api/runs/{run_id}/report")
def run_report_api(
    request: Request,
    run_id: str,
    access: AccessContext = Depends(require_access),
):
    return _ok(_service(request).run_report(access.email_hash, run_id))
