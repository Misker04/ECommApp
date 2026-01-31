from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.common.api import err, ok, get_req_id, norm_action, require_fields
from src.common.models import Buyer, Seller, Feedback
from src.common.protocol import read_message, send_message, ProtocolError


@dataclass
class _Session:
    role: str  # "buyer" | "seller"
    user_id: int
    created_at: float
    last_activity: float


class CustomerDB:
    def __init__(self, timeout_seconds: int = 300):
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._lock = asyncio.Lock()

        self._next_seller_id = 1
        self._next_buyer_id = 1

        self.sellers: Dict[int, Seller] = {}
        self.buyers: Dict[int, Buyer] = {}

        # Map name -> list of ids (names may not be unique)
        self.seller_name_index: Dict[str, List[int]] = {}
        self.buyer_name_index: Dict[str, List[int]] = {}

        # Active sessions: token -> _Session
        self.sessions: Dict[str, _Session] = {}

        # Per-session cart (active cart) stored in backend (stateless frontend requirement)
        self.session_carts: Dict[str, Dict[str, int]] = {}
        # Saved cart persists across sessions for each buyer
        self.saved_carts: Dict[int, Dict[str, int]] = {}

        # Buyer purchase history (item_id keys) - MakePurchase not required in A1, but kept for future
        self.buyer_purchases: Dict[int, List[str]] = {}

    # ---------- helpers ----------

    def _hash_pw(self, pw: str) -> str:
        return str(pw)

    def _gen_token(self) -> str:
        return uuid.uuid4().hex

    def _now(self) -> float:
        return time.time()

    def _touch_or_expire(self, token: str) -> Optional[_Session]:
        s = self.sessions.get(token)
        if s is None:
            return None
        now = self._now()
        if now - s.last_activity >= self.timeout_seconds:
            # Auto logout
            self.sessions.pop(token, None)
            self.session_carts.pop(token, None)
            return None
        s.last_activity = now
        return s

    # ---------- account ops ----------

    async def create_account(self, role: str, name: str, password: str) -> Dict[str, Any]:
        role = role.lower().strip()
        if role not in ("buyer", "seller"):
            raise ValueError("invalid role")
        name = str(name)
        if len(name) == 0 or len(name) > 32:
            raise ValueError("name must be 1..32 characters")
        pw = self._hash_pw(password)

        async with self._lock:
            if role == "seller":
                sid = self._next_seller_id
                self._next_seller_id += 1
                s = Seller(seller_id=sid, name=name, password_hash=pw)
                self.sellers[sid] = s
                self.seller_name_index.setdefault(name, []).append(sid)
                return {"seller_id": sid}
            else:
                bid = self._next_buyer_id
                self._next_buyer_id += 1
                b = Buyer(buyer_id=bid, name=name, password_hash=pw)
                self.buyers[bid] = b
                self.buyer_name_index.setdefault(name, []).append(bid)
                self.buyer_purchases.setdefault(bid, [])
                return {"buyer_id": bid}

    async def login(self, role: str, username: Any, password: str) -> Dict[str, Any]:
        role = role.lower().strip()
        pw = self._hash_pw(password)
        # username can be name or int id

        async with self._lock:
            if role == "seller":
                sid = self._resolve_user_id(role, username, pw)
                if sid is None:
                    raise ValueError("invalid credentials")
                token = self._gen_token()
                now = self._now()
                self.sessions[token] = _Session(role=role, user_id=sid, created_at=now, last_activity=now)
                self.session_carts[token] = {}  # sellers don't use carts
                return {"seller_id": sid, "session_token": token}

            if role == "buyer":
                bid = self._resolve_user_id(role, username, pw)
                if bid is None:
                    raise ValueError("invalid credentials")
                token = self._gen_token()
                now = self._now()
                self.sessions[token] = _Session(role=role, user_id=bid, created_at=now, last_activity=now)
                # Load saved cart into new session if exists
                saved = self.saved_carts.get(bid)
                self.session_carts[token] = dict(saved) if saved else {}
                return {"buyer_id": bid, "session_token": token}

            raise ValueError("invalid role")

    def _resolve_user_id(self, role: str, username: Any, pw: str) -> Optional[int]:
        # If username is numeric -> treat as id
        if isinstance(username, int) or (isinstance(username, str) and username.isdigit()):
            uid = int(username)
            if role == "seller":
                s = self.sellers.get(uid)
                return uid if s and s.password_hash == pw else None
            b = self.buyers.get(uid)
            return uid if b and b.password_hash == pw else None

        name = str(username)
        if role == "seller":
            candidates = self.seller_name_index.get(name, [])
            matches = [sid for sid in candidates if self.sellers[sid].password_hash == pw]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError("ambiguous username; please login using seller_id")
            return None

        candidates = self.buyer_name_index.get(name, [])
        matches = [bid for bid in candidates if self.buyers[bid].password_hash == pw]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("ambiguous username; please login using buyer_id")
        return None

    async def logout(self, token: str) -> Dict[str, Any]:
        async with self._lock:
            self.sessions.pop(token, None)
            self.session_carts.pop(token, None)
        return {"logged_out": True}

    async def validate_session(self, role: str, token: str) -> Dict[str, Any]:
        role = role.lower().strip()
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None:
                raise ValueError("invalid or expired session")
            if s.role != role:
                raise ValueError("session role mismatch")
            return {"user_id": int(s.user_id)}

    # ---------- ratings / purchases ----------

    async def get_seller_rating_by_id(self, seller_id: int) -> Dict[str, Any]:
        async with self._lock:
            s = self.sellers.get(int(seller_id))
            if not s:
                raise ValueError("unknown seller_id")
            return {"seller_id": int(seller_id), "seller_feedback": s.feedback.to_dict(), "items_sold": int(s.items_sold)}

    async def get_seller_rating_by_session(self, token: str) -> Dict[str, Any]:
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None:
                raise ValueError("invalid or expired session")
            if s.role != "seller":
                raise ValueError("not a seller session")
            seller = self.sellers.get(s.user_id)
            if not seller:
                raise ValueError("unknown seller")
            return {"seller_id": int(s.user_id), "seller_feedback": seller.feedback.to_dict(), "items_sold": int(seller.items_sold)}

    async def get_buyer_purchases(self, token: str) -> Dict[str, Any]:
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None:
                raise ValueError("invalid or expired session")
            if s.role != "buyer":
                raise ValueError("not a buyer session")
            hist = self.buyer_purchases.get(s.user_id, [])
            return {"buyer_id": int(s.user_id), "purchases": list(hist)}

    # ---------- carts (stored here, not in frontend) ----------

    async def cart_get(self, token: str) -> Dict[str, Any]:
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None or s.role != "buyer":
                raise ValueError("invalid buyer session")
            cart = self.session_carts.get(token, {})
            return {"cart": dict(cart)}

    async def cart_add(self, token: str, item_key: str, qty: int) -> Dict[str, Any]:
        qty = int(qty)
        if qty <= 0:
            raise ValueError("quantity must be positive")
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None or s.role != "buyer":
                raise ValueError("invalid buyer session")
            cart = self.session_carts.setdefault(token, {})
            cart[item_key] = int(cart.get(item_key, 0) + qty)
            return {"cart": dict(cart)}

    async def cart_remove(self, token: str, item_key: str, qty: int) -> Dict[str, Any]:
        qty = int(qty)
        if qty <= 0:
            raise ValueError("quantity must be positive")
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None or s.role != "buyer":
                raise ValueError("invalid buyer session")
            cart = self.session_carts.setdefault(token, {})
            cur = int(cart.get(item_key, 0))
            if cur <= 0:
                raise ValueError("item not in cart")
            if qty > cur:
                raise ValueError("cannot remove more than in cart")
            nxt = cur - qty
            if nxt == 0:
                cart.pop(item_key, None)
            else:
                cart[item_key] = nxt
            return {"cart": dict(cart)}

    async def cart_clear(self, token: str) -> Dict[str, Any]:
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None or s.role != "buyer":
                raise ValueError("invalid buyer session")
            self.session_carts[token] = {}
            return {"cleared": True}

    async def cart_save(self, token: str) -> Dict[str, Any]:
        async with self._lock:
            s = self._touch_or_expire(token)
            if s is None or s.role != "buyer":
                raise ValueError("invalid buyer session")
            cart = self.session_carts.get(token, {})
            self.saved_carts[s.user_id] = dict(cart)
            return {"saved": True, "saved_cart": dict(cart)}


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, db: CustomerDB) -> None:
    try:
        while True:
            msg = await read_message(reader)
            req_id = get_req_id(msg)
            action = norm_action(msg.get("action"))
            data = msg.get("data") or {}
            try:
                if action in ("create_account", "createaccount"):
                    require_fields(data, ["role", "username", "password"])
                    out = await db.create_account(data["role"], data["username"], data["password"])
                    resp = ok(req_id, out)
                elif action == "login":
                    require_fields(data, ["role", "username", "password"])
                    out = await db.login(data["role"], data["username"], data["password"])
                    resp = ok(req_id, out)
                elif action == "logout":
                    require_fields(data, ["session_token"])
                    out = await db.logout(str(data["session_token"]))
                    resp = ok(req_id, out)
                elif action == "validate_session":
                    require_fields(data, ["role", "session_token"])
                    out = await db.validate_session(data["role"], str(data["session_token"]))
                    resp = ok(req_id, out)
                elif action in ("get_seller_rating_by_id", "get_sellerrating_by_id"):
                    require_fields(data, ["seller_id"])
                    out = await db.get_seller_rating_by_id(int(data["seller_id"]))
                    resp = ok(req_id, out)
                elif action in ("get_seller_rating_by_session", "get_sellerrating_by_session"):
                    require_fields(data, ["session_token"])
                    out = await db.get_seller_rating_by_session(str(data["session_token"]))
                    resp = ok(req_id, out)
                elif action in ("get_buyer_purchases", "getbuyer_purchases"):
                    require_fields(data, ["session_token"])
                    out = await db.get_buyer_purchases(str(data["session_token"]))
                    resp = ok(req_id, out)
                elif action == "cart_get":
                    require_fields(data, ["session_token"])
                    out = await db.cart_get(str(data["session_token"]))
                    resp = ok(req_id, out)
                elif action == "cart_add":
                    require_fields(data, ["session_token", "item_key", "qty"])
                    out = await db.cart_add(str(data["session_token"]), str(data["item_key"]), int(data["qty"]))
                    resp = ok(req_id, out)
                elif action == "cart_remove":
                    require_fields(data, ["session_token", "item_key", "qty"])
                    out = await db.cart_remove(str(data["session_token"]), str(data["item_key"]), int(data["qty"]))
                    resp = ok(req_id, out)
                elif action == "cart_clear":
                    require_fields(data, ["session_token"])
                    out = await db.cart_clear(str(data["session_token"]))
                    resp = ok(req_id, out)
                elif action == "cart_save":
                    require_fields(data, ["session_token"])
                    out = await db.cart_save(str(data["session_token"]))
                    resp = ok(req_id, out)
                else:
                    resp = err(req_id, f"unknown action: {action}", code="unknown_action")
            except Exception as e:
                resp = err(req_id, str(e), code="bad_request")

            await send_message(writer, resp)
    except (ProtocolError, asyncio.IncompleteReadError):
        pass
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_customer_db(host: str, port: int, timeout_seconds: int) -> None:
    db = CustomerDB(timeout_seconds=timeout_seconds)
    server = await asyncio.start_server(lambda r, w: handle_client(r, w, db), host, port)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(f"CustomerDB listening on {addrs}")
    async with server:
        await server.serve_forever()


def main() -> None:
    import argparse
    from src.common.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_config(args.config)
    asyncio.run(run_customer_db(cfg.backend_customer_db.host, cfg.backend_customer_db.port, cfg.session.timeout_seconds))


if __name__ == "__main__":
    main()
