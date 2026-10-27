"""快速预约与渠道单导入 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.web.auth import AccessContext, require_access, require_csrf
from app.web.operations import OperationsError

router = APIRouter()


def _service(request: Request):
    return request.app.state.operations


def _ok(data: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"ok": True, **data},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def _read_upload(request: Request, file: UploadFile) -> tuple[str, bytes]:
    limit = int(getattr(request.app.state.settings, "max_import_bytes", 2 * 1024 * 1024))
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise OperationsError("file_too_large", "文件超过上传大小限制。", 413)
    return file.filename or "upload.csv", content


@router.post("/api/bookings/quick")
async def quick_booking_api(
    request: Request,
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise OperationsError("bad_request", "请求体必须是 JSON。") from None
    if not isinstance(payload, dict):
        raise OperationsError("bad_request", "请求体必须是 JSON 对象。")
    result = _service(request).quick_booking(access.email_hash, payload)
    if not result.get("ok"):
        status = 409 if result.get("code") == "capacity_exceeded" else 400
        return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})
    return _ok(result)


@router.post("/api/imports/dianping/preview")
async def preview_dianping_api(
    request: Request,
    file: UploadFile = File(...),
    data_as_of: str | None = Form(None),
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    filename, content = await _read_upload(request, file)
    data = _service(request).preview_dianping(
        access.email_hash, filename, content, data_as_of=data_as_of
    )
    return _ok(data)


@router.post("/api/imports/dianping/commit")
async def commit_dianping_api(
    request: Request,
    file: UploadFile = File(...),
    data_as_of: str | None = Form(None),
    confirm: str = Form("false"),
    access: AccessContext = Depends(require_access),
    _csrf: None = Depends(require_csrf),
):
    filename, content = await _read_upload(request, file)
    data = _service(request).commit_dianping(
        access.email_hash,
        filename,
        content,
        data_as_of=data_as_of,
        confirm=str(confirm).casefold() in {"1", "true", "yes", "on"},
    )
    return _ok(data)
