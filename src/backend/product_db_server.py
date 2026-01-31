from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.common.api import err, ok, get_req_id, norm_action, require_fields
from src.common.models import Item, ItemId, Feedback
from src.common.protocol import read_message, send_message, ProtocolError


class ProductDB:
    def __init__(self):
        self._lock = asyncio.Lock()
        # Per-category ID counters
        self._next_in_category: Dict[int, int] = {}
        # item_key -> Item
        self.items: Dict[str, Item] = {}

    def _new_item_id(self, category: int) -> ItemId:
        n = self._next_in_category.get(category, 1)
        self._next_in_category[category] = n + 1
        return ItemId(category=category, number=n)

    @staticmethod
    def _validate_item_fields(name: str, category: int, condition: str, price: Any, qty: Any, keywords: List[str]) -> Tuple[str, int, str, float, int, List[str]]:
        name = str(name)
        if len(name) == 0 or len(name) > 32:
            raise ValueError("item_name must be 1..32 characters")
        category = int(category)
        cond = str(condition).strip().lower()
        if cond not in ("new", "used"):
            raise ValueError("condition must be 'new' or 'used'")
        try:
            price_f = float(price)
        except Exception:
            raise ValueError("sale_price must be a number")
        if price_f < 0:
            raise ValueError("sale_price must be >= 0")
        qty_i = int(qty)
        if qty_i < 0:
            raise ValueError("item_quantity must be >= 0")
        if not isinstance(keywords, list):
            raise ValueError("keywords must be a list")
        if len(keywords) > 5:
            raise ValueError("keywords must have at most 5 entries")
        cleaned: List[str] = []
        for kw in keywords:
            s = str(kw)
            if len(s) == 0 or len(s) > 8:
                raise ValueError("each keyword must be 1..8 characters")
            cleaned.append(s)
        return name, category, cond, price_f, qty_i, cleaned

    @staticmethod
    def _tokenize_name(name: str) -> List[str]:
        return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]

    async def register_item(self, seller_id: int, attrs: Dict[str, Any]) -> Dict[str, Any]:
        require_fields(attrs, ["item_name", "item_category", "condition", "sale_price", "item_quantity", "keywords"])
        name, category, cond, price_f, qty_i, kws = self._validate_item_fields(
            attrs["item_name"], attrs["item_category"], attrs["condition"], attrs["sale_price"], attrs["item_quantity"], attrs["keywords"]
        )
        async with self._lock:
            iid = self._new_item_id(category)
            item = Item(
                item_id=iid,
                seller_id=int(seller_id),
                name=name,
                category=category,
                keywords=kws,
                condition=cond,  # type: ignore
                sale_price=price_f,
                quantity=qty_i,
                feedback=Feedback(0, 0),
            )
            self.items[iid.key()] = item
            return {"item_id": iid.to_dict()}

    async def change_item_price(self, seller_id: int, item_id_any: Any, new_price: Any) -> Dict[str, Any]:
        iid = ItemId.from_any(item_id_any)
        try:
            price_f = float(new_price)
        except Exception:
            raise ValueError("new_price must be a number")
        if price_f < 0:
            raise ValueError("new_price must be >= 0")
        async with self._lock:
            item = self.items.get(iid.key())
            if not item:
                raise ValueError("unknown item_id")
            if int(item.seller_id) != int(seller_id):
                raise ValueError("item does not belong to this seller")
            item.sale_price = price_f
            return {"updated": True}

    async def update_units_remove(self, seller_id: int, item_id_any: Any, qty_remove: int) -> Dict[str, Any]:
        iid = ItemId.from_any(item_id_any)
        qty_remove = int(qty_remove)
        if qty_remove <= 0:
            raise ValueError("quantity to remove must be positive")
        async with self._lock:
            item = self.items.get(iid.key())
            if not item:
                raise ValueError("unknown item_id")
            if int(item.seller_id) != int(seller_id):
                raise ValueError("item does not belong to this seller")
            if qty_remove > item.quantity:
                raise ValueError("cannot remove more than available")
            item.quantity -= qty_remove
            return {"updated": True, "item_quantity": int(item.quantity)}

    async def list_items_for_seller(self, seller_id: int) -> Dict[str, Any]:
        async with self._lock:
            items = [it.to_public_dict() for it in self.items.values() if int(it.seller_id) == int(seller_id)]
            return {"items": items}

    async def get_item(self, item_id_any: Any) -> Dict[str, Any]:
        iid = ItemId.from_any(item_id_any)
        async with self._lock:
            item = self.items.get(iid.key())
            if not item:
                raise ValueError("unknown item_id")
            return {"item": item.to_public_dict()}

    async def provide_feedback(self, item_id_any: Any, vote: str) -> Dict[str, Any]:
        iid = ItemId.from_any(item_id_any)
        v = str(vote).strip().lower()
        if v not in ("up", "down"):
            raise ValueError("vote must be 'up' or 'down'")
        async with self._lock:
            item = self.items.get(iid.key())
            if not item:
                raise ValueError("unknown item_id")
            if v == "up":
                item.feedback.thumbs_up += 1
            else:
                item.feedback.thumbs_down += 1
            return {"item_feedback": item.feedback.to_dict()}

    async def search(self, category: int, keywords: List[str]) -> Dict[str, Any]:
        category = int(category)
        q = [str(k).lower() for k in (keywords or []) if str(k).strip()]
        if len(q) > 5:
            raise ValueError("keywords must have at most 5 entries")
        for k in q:
            if len(k) > 8:
                raise ValueError("each keyword must be <= 8 characters")

        async with self._lock:
            candidates = [it for it in self.items.values() if int(it.category) == category and int(it.quantity) > 0]

            if not q:
                # No keywords: return all in category, sorted by positive feedback then price.
                def key(it: Item):
                    score = int(it.feedback.thumbs_up) - int(it.feedback.thumbs_down)
                    return (-score, float(it.sale_price), it.item_id.category, it.item_id.number)

                out = sorted(candidates, key=key)
                return {"items": [it.to_public_dict() for it in out]}

            # "Best" keyword match semantics:
            # Score = count of query keywords that match either:
            #   (1) item.keywords (case-insensitive exact match), or
            #   (2) tokens from item name (case-insensitive exact match)
            # Ties broken by: (thumbs_up - thumbs_down) desc, price asc, item_id asc
            def match_score(it: Item) -> int:
                kwset = {k.lower() for k in it.keywords}
                nameset = set(self._tokenize_name(it.name))
                return sum(1 for k in q if k in kwset or k in nameset)

            scored = []
            for it in candidates:
                s = match_score(it)
                if s > 0:
                    scored.append((s, it))

            def sort_key(pair):
                s, it = pair
                feedback_score = int(it.feedback.thumbs_up) - int(it.feedback.thumbs_down)
                return (-s, -feedback_score, float(it.sale_price), it.item_id.category, it.item_id.number)

            out = [it for _, it in sorted(scored, key=sort_key)]
            return {"items": [it.to_public_dict() for it in out]}


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, db: ProductDB) -> None:
    try:
        while True:
            msg = await read_message(reader)
            req_id = get_req_id(msg)
            action = norm_action(msg.get("action"))
            data = msg.get("data") or {}
            try:
                if action in ("register_item",):
                    require_fields(data, ["seller_id", "attrs"])
                    out = await db.register_item(int(data["seller_id"]), dict(data["attrs"]))
                    resp = ok(req_id, out)
                elif action in ("change_item_price",):
                    require_fields(data, ["seller_id", "item_id", "new_price"])
                    out = await db.change_item_price(int(data["seller_id"]), data["item_id"], data["new_price"])
                    resp = ok(req_id, out)
                elif action in ("update_units_remove",):
                    require_fields(data, ["seller_id", "item_id", "qty"])
                    out = await db.update_units_remove(int(data["seller_id"]), data["item_id"], int(data["qty"]))
                    resp = ok(req_id, out)
                elif action in ("list_items_for_seller",):
                    require_fields(data, ["seller_id"])
                    out = await db.list_items_for_seller(int(data["seller_id"]))
                    resp = ok(req_id, out)
                elif action in ("get_item",):
                    require_fields(data, ["item_id"])
                    out = await db.get_item(data["item_id"])
                    resp = ok(req_id, out)
                elif action in ("provide_feedback",):
                    require_fields(data, ["item_id", "vote"])
                    out = await db.provide_feedback(data["item_id"], str(data["vote"]))
                    resp = ok(req_id, out)
                elif action in ("search",):
                    require_fields(data, ["category", "keywords"])
                    out = await db.search(int(data["category"]), list(data["keywords"]))
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


async def run_product_db(host: str, port: int) -> None:
    db = ProductDB()
    server = await asyncio.start_server(lambda r, w: handle_client(r, w, db), host, port)
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(f"ProductDB listening on {addrs}")
    async with server:
        await server.serve_forever()


def main() -> None:
    import argparse
    from src.common.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_config(args.config)
    asyncio.run(run_product_db(cfg.backend_product_db.host, cfg.backend_product_db.port))


if __name__ == "__main__":
    main()
