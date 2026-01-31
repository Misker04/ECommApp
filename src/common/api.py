from __future__ import annotations

from typing import Any, Dict, Iterable


def ok(req_id: str | None, data: Any = None) -> Dict[str, Any]:
    return {
        "req_id": req_id or "",
        "ok": True,
        "error": None,
        "data": data if data is not None else {},
    }


def err(req_id: str | None, message: str, code: str = "error", data: Any = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {
        "req_id": req_id or "",
        "ok": False,
        "error": payload,
        "data": {},
    }


def get_req_id(msg: Dict[str, Any]) -> str:
    v = msg.get("req_id", "")
    return str(v) if v is not None else ""


def norm_action(action: Any) -> str:
    if action is None:
        return ""
    return str(action).strip().lower()


def require_fields(data: Dict[str, Any], fields: Iterable[str]) -> None:
    missing = [f for f in fields if f not in data]
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")
