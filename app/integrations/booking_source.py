"""渠道预约来源适配器。

当前只实现“人工导出的点评订单文件”这一种离线来源。未来若接入官方
webhook/API，只需把外部事件转成 `ExternalBooking`，下游仍复用
`intake.plan_import` / `intake.commit_import`，不复制容量与幂等逻辑。

本模块不做抓取、不登录商家后台、不发网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

import pandas as pd

from app.tools.intake import (
    detect_dianping_mapping,
    parse_upload,
    validate_dianping_rows,
)


@dataclass(frozen=True)
class ExternalBooking:
    source: str
    external_id: str
    session_id: str | None
    seats: int
    player_name: str | None = None
    status: str = "active"
    data_as_of: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "session_id": self.session_id,
            "seats": self.seats,
            "player_name": self.player_name,
            "status": self.status,
            "data_as_of": self.data_as_of,
        }


class BookingSourceAdapter(Protocol):
    name: str

    def load(self, payload: Any) -> Iterable[ExternalBooking]: ...


class CsvDianpingAdapter:
    """把上传的 CSV/XLSX 转成 ExternalBooking；匹配与容量检查仍由 intake 做。"""

    name = "dianping_csv"

    def load(self, payload: Any) -> Iterable[ExternalBooking]:
        if isinstance(payload, dict):
            frame = payload.get("frame")
            mapping = payload.get("mapping")
            data_as_of = payload.get("data_as_of")
        else:
            frame, mapping, data_as_of = payload, None, None
        if frame is None:
            raise ValueError("payload 必须包含 DataFrame。")
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("payload.frame 必须是 DataFrame。")
        mapping = mapping or detect_dianping_mapping(frame.columns)
        # 先走一遍纯函数校验，避免适配器自己复制字段解释。
        preview = validate_dianping_rows(
            frame,
            mapping,
            pd.DataFrame(columns=["session_id", "script_name", "start_time"]),
            data_as_of=data_as_of,
        )
        for row in preview.rows:
            yield ExternalBooking(
                source=self.name,
                external_id=str(row.get("external_id") or ""),
                session_id=row.get("session_id"),
                seats=int(row.get("seats") or 0),
                player_name=row.get("player_name"),
                status=row.get("status") or "active",
                data_as_of=preview.data_as_of,
            )


def read_upload(filename: str, content: bytes) -> tuple[pd.DataFrame, dict[str, str]]:
    frame = parse_upload(filename, content)
    return frame, detect_dianping_mapping(frame.columns)


__all__ = [
    "BookingSourceAdapter",
    "CsvDianpingAdapter",
    "ExternalBooking",
    "read_upload",
]
