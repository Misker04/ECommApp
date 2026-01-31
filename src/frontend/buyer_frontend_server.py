from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from src.common.api import err, ok, get_req_id, norm_action
from src.common.config import load_config
from src.common.internal_client import Endpoint, TcpClientPool
from src.common.models import ItemId
from src.common.protocol import read_message, send_message, ProtocolError


def _parse_keywords(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip() for p in v.split(",") if p.strip()]
        return parts
    if isinstance(v, list):
        return [str(x) for x in v]
    raise ValueError("keywords must be a list of strings")


class BuyerFrontend:
    def __init__(self, customer_db: TcpClientPool, product_db: TcpClientPool, enable_make_purchase: bool):
        self.customer_db = customer_db
        self.product_db = product_db
        self.enable_make_purchase = enable_make_purchase

    async def _validate(self, session_token: str) -> int:
        resp = await self.customer_db.call(
            {"req_id": "", "action": "validate_session", "data": {"role": "buyer", "session_token": session_token}}
        )
        if not resp.get("ok"):
            raise ValueError(resp.get("error", {}).get("message", "invalid session"))
        return int(resp["data"]["user_id"])

    async def handle(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        req_id = get_req_id(msg)
        action = norm_action(msg.get("action"))
        data = msg.get("data") or {}

        try:
            # -------- account/session --------
            if action in ("createaccount", "create_account"):
                password = data.get("password")
                username = data.get("username") or data.get("buyer_name")
                if username is None or password is None:
                    raise ValueError("missing field(s): username/buyer_name and password")
                r = await self.customer_db.call(
                    {
                        "req_id": req_id,
                        "action": "create_account",
                        "data": {"role": "buyer", "username": username, "password": password},
                    }
                )
                return r

            if action == "login":
                password = data.get("password")
                username = data.get("username") or data.get("buyer_name") or data.get("buyer_id")
                if username is None or password is None:
                    raise ValueError("missing field(s): username/buyer_name/buyer_id and password")
                r = await self.customer_db.call(
                    {
                        "req_id": req_id,
                        "action": "login",
                        "data": {"role": "buyer", "username": username, "password": password},
                    }
                )
                return r

            if action == "logout":
                token = data.get("session_token")
                if token is None:
                    raise ValueError("missing field: session_token")
                r = await self.customer_db.call({"req_id": req_id, "action": "logout", "data": {"session_token": token}})
                return r

            # everything else requires buyer session
            token = data.get("session_token")
            if token is None:
                raise ValueError("missing field: session_token")
            token = str(token)
            _buyer_id = await self._validate(token)

            # -------- buyer ops --------
            if action in ("searchitemsforsale", "search_items_for_sale", "search"):
                category = data.get("item_category")
                if category is None:
                    raise ValueError("missing field: item_category")
                kws = _parse_keywords(data.get("keywords"))
                r = await self.product_db.call(
                    {"req_id": req_id, "action": "search", "data": {"category": int(category), "keywords": kws}}
                )
                return r

            if action in ("getitem", "get_item"):
                item_id = data.get("item_id")
                if item_id is None:
                    raise ValueError("missing field: item_id")
                r = await self.product_db.call({"req_id": req_id, "action": "get_item", "data": {"item_id": item_id}})
                return r

            if action in ("additemtocart", "add_item_to_cart"):
                item_id = data.get("item_id")
                qty = data.get("quantity")
                if qty is None:
                    qty = data.get("qty")
                if item_id is None or qty is None:
                    raise ValueError("missing field(s): item_id and quantity/qty")
                qty = int(qty)
                if qty <= 0:
                    raise ValueError("quantity must be positive")

                item_resp = await self.product_db.call({"req_id": req_id, "action": "get_item", "data": {"item_id": item_id}})
                if not item_resp.get("ok"):
                    return item_resp
                item = item_resp["data"]["item"]
                available = int(item["item_quantity"])

                cart_resp = await self.customer_db.call({"req_id": req_id, "action": "cart_get", "data": {"session_token": token}})
                if not cart_resp.get("ok"):
                    return cart_resp
                cart = cart_resp["data"].get("cart", {})

                key = ItemId.from_any(item_id).key()
                cur = int(cart.get(key, 0))
                if cur + qty > available:
                    return err(req_id, f"insufficient inventory: requested {cur + qty}, available {available}", code="insufficient_inventory")

                r = await self.customer_db.call(
                    {"req_id": req_id, "action": "cart_add", "data": {"session_token": token, "item_key": key, "qty": qty}}
                )
                return r

            if action in ("removeitemfromcart", "remove_item_from_cart"):
                item_id = data.get("item_id")
                qty = data.get("quantity")
                if qty is None:
                    qty = data.get("qty")
                if item_id is None or qty is None:
                    raise ValueError("missing field(s): item_id and quantity/qty")
                qty = int(qty)
                if qty <= 0:
                    raise ValueError("quantity must be positive")
                key = ItemId.from_any(item_id).key()
                r = await self.customer_db.call(
                    {"req_id": req_id, "action": "cart_remove", "data": {"session_token": token, "item_key": key, "qty": qty}}
                )
                return r

            if action in ("savecart", "save_cart"):
                return await self.customer_db.call({"req_id": req_id, "action": "cart_save", "data": {"session_token": token}})

            if action in ("clearcart", "clear_cart"):
                return await self.customer_db.call({"req_id": req_id, "action": "cart_clear", "data": {"session_token": token}})

            if action in ("displaycart", "display_cart"):
                r = await self.customer_db.call({"req_id": req_id, "action": "cart_get", "data": {"session_token": token}})
                if not r.get("ok"):
                    return r
                cart = r["data"].get("cart", {})
                items = []
                for k, q in cart.items():
                    iid = ItemId.from_any(k)
                    items.append({"item_id": iid.to_dict(), "quantity": int(q)})
                return ok(req_id, {"cart": items})

            if action in ("providefeedback", "provide_feedback"):
                item_id = data.get("item_id")
                vote = data.get("vote")
                if vote is None:
                    vote = data.get("feedback")
                if item_id is None or vote is None:
                    raise ValueError("missing field(s): item_id and vote/feedback")
                return await self.product_db.call({"req_id": req_id, "action": "provide_feedback", "data": {"item_id": item_id, "vote": vote}})

            if action in ("getsellerrating", "get_seller_rating"):
                seller_id = data.get("seller_id")
                if seller_id is None:
                    raise ValueError("missing field: seller_id")
                return await self.customer_db.call(
                    {"req_id": req_id, "action": "get_seller_rating_by_id", "data": {"seller_id": int(seller_id)}}
                )

            if action in ("getbuyerpurchases", "get_buyer_purchases"):
                return await self.customer_db.call({"req_id": req_id, "action": "get_buyer_purchases", "data": {"session_token": token}})

            if action in ("makepurchase", "make_purchase"):
                if not self.enable_make_purchase:
                    return err(req_id, "MakePurchase is not implemented in assignment 1", code="not_implemented")
                return err(req_id, "MakePurchase not yet wired", code="not_implemented")

            return err(req_id, f"unknown action: {action}", code="unknown_action")

        except Exception as e:
            return err(req_id, str(e), code="bad_request")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, app: BuyerFrontend) -> None:
    try:
        while True:
            msg = await read_message(reader)
            resp = await app.handle(msg)
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


async def run_server(config_path: str) -> None:
    cfg = load_config(config_path)
    customer_pool = TcpClientPool(Endpoint(cfg.backend_customer_db.host, cfg.backend_customer_db.port), size=4)
    product_pool = TcpClientPool(Endpoint(cfg.backend_product_db.host, cfg.backend_product_db.port), size=4)
    app = BuyerFrontend(customer_pool, product_pool, enable_make_purchase=cfg.features.enable_make_purchase)

    server = await asyncio.start_server(lambda r, w: handle_client(r, w, app), cfg.frontend_buyer.host, cfg.frontend_buyer.port)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(f"BuyerFrontend listening on {addrs}")
    async with server:
        await server.serve_forever()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    asyncio.run(run_server(args.config))


if __name__ == "__main__":
    main()
