from __future__ import annotations

from typing import Any, Dict, Optional

def ok(req_id: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"req_id": req_id, "ok": True, "error": None, "data": data or {}}

def err(req_id: str, message: str) -> Dict[str, Any]:
    return {"req_id": req_id, "ok": False, "error": message, "data": {}}
