from __future__ import annotations

from typing import Any, Dict

from src.common.models import Item, Feedback, ItemId
from src.server.auth import hash_password
from src.server.state import MarketState
from src.server.handlers.utils import ok, err


def _norm_action(a: Any) -> str:
    # accept both CreateAccount and create_account, etc.
    s = str(a or "")
    return s.strip()


async def _require_seller_session(state: MarketState, req: Dict[str, Any]) -> tuple[str, int] | None:
    req_id = str(req.get("req_id", ""))
    data = req.get("data") or {}
    token = str(data.get("session_token") or req.get("session_token") or "")
    sess = await state.get_session(token)
    if not sess or sess.role != "seller":
        return None
    return (token, int(sess.principal_id))


async def handle(state: MarketState, req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = str(req.get("req_id", ""))
    action = _norm_action(req.get("action"))
    data = req.get("data") or {}

    try:
        # ------------------------------
        # CreateAccount
        # ------------------------------
        if action in {"create_account", "CreateAccount", "register"}:
            name = str(data.get("seller_name") or data.get("username") or data.get("name") or "")
            password = str(data["password"])
            if not name:
                return err(req_id, "seller_name is required")
            seller = await state.db.add_seller(name=name, password_hash=hash_password(password))
            return ok(req_id, {"seller_id": int(seller.seller_id), "seller_name": seller.name})

        # ------------------------------
        # Login
        # ------------------------------
        if action in {"login", "Login"}:
            password = str(data["password"])

            # Prefer unambiguous seller_id if provided
            if data.get("seller_id") is not None:
                seller_id = int(data["seller_id"])
                s = await state.db.get_seller(seller_id)
                if not s or s.password_hash != hash_password(password):
                    return err(req_id, "invalid credentials")
                token = await state.create_session("seller", seller_id)
                return ok(req_id, {"seller_id": int(s.seller_id), "seller_name": s.name, "session_token": token})

            # Fallback: seller_name (may be non-unique)
            name = str(data.get("seller_name") or data.get("username") or "")
            if not name:
                return err(req_id, "seller_id or seller_name is required")
            matches = await state.db.find_sellers_by_name(name)
            if len(matches) == 0:
                return err(req_id, "invalid credentials")
            if len(matches) > 1:
                return err(req_id, "ambiguous seller_name; use seller_id to login")
            s = matches[0]
            if s.password_hash != hash_password(password):
                return err(req_id, "invalid credentials")
            token = await state.create_session("seller", int(s.seller_id))
            return ok(req_id, {"seller_id": int(s.seller_id), "seller_name": s.name, "session_token": token})

        # ------------------------------
        # Logout
        # ------------------------------
        if action in {"logout", "Logout"}:
            token = str(data.get("session_token") or "")
            if token:
                await state.delete_session(token)
            return ok(req_id, {"logged_out": True})

        # Require active seller session for everything below
        sess = await _require_seller_session(state, req)
        if not sess:
            return err(req_id, "not logged in (seller)")
        token, seller_id = sess

        # ------------------------------
        # GetSellerRating
        # ------------------------------
        if action in {"get_seller_rating", "GetSellerRating"}:
            s = await state.db.get_seller(seller_id)
            if not s:
                return err(req_id, "seller not found")
            return ok(req_id, {"seller_id": int(s.seller_id), "seller_feedback": s.feedback.to_dict()})

        # ------------------------------
        # RegisterItemForSale
        # ------------------------------
        if action in {"register_item_for_sale", "RegisterItemForSale", "list_item", "list_product"}:
            s = await state.db.get_seller(seller_id)
            if not s:
                return err(req_id, "seller not found")

            item_name = str(data.get("item_name") or data.get("name") or "")
            item_category = int(data.get("item_category") if data.get("item_category") is not None else data.get("category"))
            keywords = data.get("keywords") or []
            condition_raw = str(data.get("condition") or "").strip().lower()
            sale_price = float(data.get("sale_price") if data.get("sale_price") is not None else data.get("price"))
            quantity = int(data.get("quantity"))

            # Validate assignment constraints
            if not item_name:
                return err(req_id, "item_name is required")
            if len(item_name) > 32:
                return err(req_id, "item_name must be <= 32 characters")
            if condition_raw not in {"new", "used"}:
                return err(req_id, "condition must be 'New' or 'Used'")

            if not isinstance(keywords, list):
                return err(req_id, "keywords must be a list")
            if len(keywords) > 5:
                return err(req_id, "keywords must have at most 5 entries")
            clean_keywords = []
            for kw in keywords:
                s_kw = str(kw)
                if len(s_kw) > 8:
                    return err(req_id, "each keyword must be <= 8 characters")
                clean_keywords.append(s_kw)

            if quantity < 0:
                return err(req_id, "quantity must be >= 0")
            if sale_price < 0:
                return err(req_id, "sale_price must be >= 0")

            item_id = await state.db.allocate_item_id(item_category)
            item = Item(
                item_id=item_id,
                seller_id=int(seller_id),
                name=item_name,
                category=item_category,
                keywords=clean_keywords,
                condition=condition_raw,  # type: ignore
                sale_price=sale_price,
                quantity=quantity,
                feedback=Feedback(0, 0),
            )
            await state.db.add_item(item)
            return ok(req_id, {"item_id": item_id.to_dict()})

        # ------------------------------
        # ChangeItemPrice
        # ------------------------------
        if action in {"change_item_price", "ChangeItemPrice"}:
            item_id = ItemId.from_any(data.get("item_id"))
            new_price = float(data.get("sale_price") if data.get("sale_price") is not None else data.get("new_price"))
            it = await state.db.get_item(item_id)
            if not it:
                return err(req_id, "item not found")
            if int(it.seller_id) != int(seller_id):
                return err(req_id, "cannot modify item: not owned by seller")
            if new_price < 0:
                return err(req_id, "sale_price must be >= 0")
            await state.db.update_item_price(item_id, new_price)
            return ok(req_id, {"item_id": item_id.to_dict(), "sale_price": float(new_price)})

        # ------------------------------
        # UpdateUnitsForSale (remove units)
        # ------------------------------
        if action in {"update_units_for_sale", "UpdateUnitsForSale"}:
            item_id = ItemId.from_any(data.get("item_id"))
            remove_qty = int(data.get("remove_qty") if data.get("remove_qty") is not None else data.get("quantity"))
            it = await state.db.get_item(item_id)
            if not it:
                return err(req_id, "item not found")
            if int(it.seller_id) != int(seller_id):
                return err(req_id, "cannot modify item: not owned by seller")
            new_qty = await state.db.remove_units_for_sale(item_id, remove_qty)
            return ok(req_id, {"item_id": item_id.to_dict(), "quantity": int(new_qty)})

        # ------------------------------
        # DisplayItemsForSale
        # ------------------------------
        if action in {"display_items_for_sale", "DisplayItemsForSale"}:
            items = await state.db.list_items_for_seller(seller_id)
            return ok(req_id, {"items": [it.to_public_dict() for it in items]})

        return err(req_id, f"unknown seller action: {action}")
    except KeyError as e:
        return err(req_id, f"missing field: {e}")
    except Exception as e:
        return err(req_id, str(e))
