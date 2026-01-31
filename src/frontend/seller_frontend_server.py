from __future__ import annotations

import asyncio
from typing import Any, Dict

from src.common.api import err, ok, get_req_id, norm_action, require_fields
from src.common.config import load_config
from src.common.internal_client import Endpoint, TcpClientPool
from src.common.protocol import read_message, send_message, ProtocolError


class SellerFrontend:
    def __init__(self, customer_db: TcpClientPool, product_db: TcpClientPool):
        self.customer_db = customer_db
        self.product_db = product_db

    async def _validate(self, session_token: str) -> int:
        resp = await self.customer_db.call(
            {"req_id": "", "action": "validate_session", "data": {"role": "seller", "session_token": session_token}}
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
                require_fields(data, ["username", "password"])
                r = await self.customer_db.call(
                    {"req_id": req_id, "action": "create_account", "data": {"role": "seller", "username": data["username"], "password": data["password"]}}
                )
                return r

            if action == "login":
                require_fields(data, ["username", "password"])
                r = await self.customer_db.call(
                    {"req_id": req_id, "action": "login", "data": {"role": "seller", "username": data["username"], "password": data["password"]}}
                )
                return r

            if action == "logout":
                require_fields(data, ["session_token"])
                r = await self.customer_db.call({"req_id": req_id, "action": "logout", "data": {"session_token": data["session_token"]}})
                return r

            # everything else requires seller session
            require_fields(data, ["session_token"])
            token = str(data["session_token"])
            seller_id = await self._validate(token)

            # -------- seller ops --------
            if action in ("getsellerrating", "get_seller_rating"):
                r = await self.customer_db.call({"req_id": req_id, "action": "get_seller_rating_by_session", "data": {"session_token": token}})
                return r

            if action in ("registeritemforsale", "register_item_for_sale"):
                require_fields(data, ["item_name", "item_category", "keywords", "condition", "sale_price", "item_quantity"])
                attrs = {
                    "item_name": data["item_name"],
                    "item_category": int(data["item_category"]),
                    "keywords": data["keywords"],
                    "condition": data["condition"],
                    "sale_price": data["sale_price"],
                    "item_quantity": int(data["item_quantity"]),
                }
                r = await self.product_db.call({"req_id": req_id, "action": "register_item", "data": {"seller_id": seller_id, "attrs": attrs}})
                return r

            if action in ("changeitemprice", "change_item_price"):
                require_fields(data, ["item_id", "new_price"])
                r = await self.product_db.call(
                    {"req_id": req_id, "action": "change_item_price", "data": {"seller_id": seller_id, "item_id": data["item_id"], "new_price": data["new_price"]}}
                )
                return r

            if action in ("updateunitsforsale", "update_units_for_sale"):
                require_fields(data, ["item_id", "quantity"])
                r = await self.product_db.call(
                    {"req_id": req_id, "action": "update_units_remove", "data": {"seller_id": seller_id, "item_id": data["item_id"], "qty": int(data["quantity"])}}
                )
                return r

            if action in ("displayitemsforsale", "display_items_for_sale"):
                r = await self.product_db.call({"req_id": req_id, "action": "list_items_for_seller", "data": {"seller_id": seller_id}})
                return r

            return err(req_id, f"unknown action: {action}", code="unknown_action")

        except Exception as e:
            return err(req_id, str(e), code="bad_request")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, app: SellerFrontend) -> None:
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
    app = SellerFrontend(customer_pool, product_pool)

    server = await asyncio.start_server(lambda r, w: handle_client(r, w, app), cfg.frontend_seller.host, cfg.frontend_seller.port)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(f"SellerFrontend listening on {addrs}")
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
