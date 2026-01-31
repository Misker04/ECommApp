from __future__ import annotations

from typing import Any, Dict

from src.server.state import MarketState
from src.server.handlers import buyer as buyer_handler
from src.server.handlers import seller as seller_handler
from src.server.handlers.utils import ok, err


async def dispatch(state: MarketState, req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = str(req.get("req_id", ""))
    action = req.get("action")

    if action == "ping":
        return ok(req_id, {"pong": True})

    role = req.get("role")
    if role == "buyer":
        return await buyer_handler.handle(state, req)
    if role == "seller":
        return await seller_handler.handle(state, req)
    return err(req_id, "missing or invalid role (buyer/seller)")
