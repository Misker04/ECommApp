from __future__ import annotations

from typing import Any, Dict, List, Tuple

from src.common.models import ItemId, Transaction, TransactionLine
from src.common.models import new_id
from src.server.auth import hash_password
from src.server.state import MarketState
from src.server.handlers.utils import ok, err


def _norm_action(a: Any) -> str:
    """Normalize action names while supporting assignment-style CamelCase."""
    return str(a or "").strip()


def _parse_keywords(v: Any) -> List[str]:
    """Accept list of keywords, or comma-separated string."""
    if v is None:
        return []
    if isinstance(v, list):
        kws = [str(x) for x in v if str(x)]
    elif isinstance(v, str):
        # allow "kw1,kw2"
        kws = [k.strip() for k in v.split(",") if k.strip()]
    else:
        raise ValueError("keywords must be a list or comma-separated string")

    if len(kws) > 5:
        raise ValueError("keywords must have at most 5 entries")
    for k in kws:
        if len(k) > 8:
            raise ValueError("each keyword must be <= 8 characters")
    return kws


async def _require_buyer_session(state: MarketState, req: Dict[str, Any]) -> Tuple[str, int] | None:
    """Return (token, buyer_id) if a valid buyer session token exists, else None."""
    data = req.get("data") or {}
    token = str(data.get("session_token") or req.get("session_token") or "")
    sess = await state.get_session(token)
    if not sess or sess.role != "buyer":
        return None
    return (token, int(sess.principal_id))


def _score_item_keywords(item_keywords: List[str], query_keywords: List[str]) -> int:
    if not query_keywords:
        return 0
    item_set = {k.lower() for k in item_keywords}
    return sum(1 for q in query_keywords if q.lower() in item_set)


async def _do_purchase(state: MarketState, buyer_id: int) -> Dict[str, Any]:
    """Implements purchase semantics used by checkout/make_purchase."""
    buyer = await state.db.get_buyer(buyer_id)
    if not buyer:
        raise ValueError("buyer not found")

    # copy and clear cart atomically
    async with state.carts_lock:
        cart = dict(state.carts.get(int(buyer_id), {}))
        state.carts[int(buyer_id)] = {}

    if not cart:
        raise ValueError("cart is empty")

    total = 0.0
    lines: List[TransactionLine] = []
    total_units = 0

    # Validate all items first (best-effort)
    for item_key, qty in cart.items():
        item_id = ItemId.from_any(item_key)
        it = await state.db.get_item(item_id)
        if not it:
            raise ValueError(f"item not found: {item_key}")
        if qty <= 0:
            raise ValueError("cart contains invalid quantity")
        if it.quantity < qty:
            raise ValueError(f"insufficient inventory for {item_key}")

    # Apply updates
    for item_key, qty in cart.items():
        item_id = ItemId.from_any(item_key)
        it = await state.db.get_item(item_id)
        assert it is not None

        total += float(it.sale_price) * int(qty)
        total_units += int(qty)

        lines.append(
            TransactionLine(
                item_id=item_id,
                seller_id=int(it.seller_id),
                qty=int(qty),
                price_each=float(it.sale_price),
            )
        )

        # decrement inventory
        await state.db.update_item_quantity(item_id, int(it.quantity) - int(qty))

        # increment seller items_sold
        await state.db.inc_seller_items_sold(int(it.seller_id), int(qty))

    # increment buyer items_purchased
    await state.db.inc_buyer_items_purchased(int(buyer_id), int(total_units))

    txn = Transaction(
        txn_id=new_id("txn"),
        buyer_id=int(buyer_id),
        items=lines,
        total=total,
    )
    await state.db.add_transaction(txn)
    return {"transaction": txn.to_public_dict()}


async def handle(state: MarketState, req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = str(req.get("req_id", ""))
    action = _norm_action(req.get("action"))
    data = req.get("data") or {}

    try:
        # ------------------------------
        # CreateAccount
        # ------------------------------
        if action in {"create_account", "CreateAccount", "register"}:
            name = str(data.get("buyer_name") or data.get("username") or data.get("name") or "")
            password = str(data["password"])
            if not name:
                return err(req_id, "buyer_name is required")
            buyer = await state.db.add_buyer(name=name, password_hash=hash_password(password))
            return ok(req_id, {"buyer_id": int(buyer.buyer_id), "buyer_name": buyer.name})

        # ------------------------------
        # Login
        # ------------------------------
        if action in {"login", "Login"}:
            password = str(data["password"])

            # Prefer unambiguous buyer_id if provided
            if data.get("buyer_id") is not None:
                buyer_id = int(data["buyer_id"])
                b = await state.db.get_buyer(buyer_id)
                if not b or b.password_hash != hash_password(password):
                    return err(req_id, "invalid credentials")
                token = await state.create_session("buyer", buyer_id)

                # load saved cart into active cart
                saved = await state.db.load_saved_cart(buyer_id)
                async with state.carts_lock:
                    state.carts[buyer_id] = dict(saved)

                return ok(req_id, {"buyer_id": int(b.buyer_id), "buyer_name": b.name, "session_token": token})

            # Fallback: buyer_name (may be non-unique)
            name = str(data.get("buyer_name") or data.get("username") or "")
            if not name:
                return err(req_id, "buyer_id or buyer_name is required")
            matches = await state.db.find_buyers_by_name(name)
            if len(matches) == 0:
                return err(req_id, "invalid credentials")
            if len(matches) > 1:
                return err(req_id, "ambiguous buyer_name; use buyer_id to login")
            b = matches[0]
            if b.password_hash != hash_password(password):
                return err(req_id, "invalid credentials")
            token = await state.create_session("buyer", int(b.buyer_id))

            saved = await state.db.load_saved_cart(int(b.buyer_id))
            async with state.carts_lock:
                state.carts[int(b.buyer_id)] = dict(saved)

            return ok(req_id, {"buyer_id": int(b.buyer_id), "buyer_name": b.name, "session_token": token})

        # ------------------------------
        # Logout
        # ------------------------------
        if action in {"logout", "Logout"}:
            token = str(data.get("session_token") or "")
            sess = await state.get_session(token)
            if sess and sess.role == "buyer":
                buyer_id = int(sess.principal_id)
                # Clear the active cart when logging out (unless it was saved explicitly).
                async with state.carts_lock:
                    state.carts[buyer_id] = {}
            if token:
                await state.delete_session(token)
            return ok(req_id, {"logged_out": True})

        # Require active buyer session for everything below
        sess = await _require_buyer_session(state, req)
        if not sess:
            return err(req_id, "not logged in (buyer)")
        _token, buyer_id = sess

        # ------------------------------
        # SearchItemsForSale
        # ------------------------------
        if action in {"search_items_for_sale", "SearchItemsForSale", "search"}:
            if data.get("item_category") is None and data.get("category") is None:
                return err(req_id, "item_category is required")
            category = int(data.get("item_category") if data.get("item_category") is not None else data.get("category"))
            keywords = _parse_keywords(data.get("keywords"))

            items = await state.db.list_items()
            # Filter by category and availability
            candidates = [it for it in items if int(it.category) == category and int(it.quantity) > 0]

            scored: List[Tuple[int, float, float, Dict[str, Any]]] = []
            for it in candidates:
                score = _score_item_keywords(it.keywords, keywords)
                if keywords and score == 0:
                    continue
                # tiebreakers: more thumbs up, then newer
                scored.append(
                    (score, float(it.feedback.thumbs_up), float(it.created_at), it.to_public_dict() | {"match_score": score})
                )

            scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
            return ok(req_id, {"items": [x[3] for x in scored]})

        # ------------------------------
        # GetItem
        # ------------------------------
        if action in {"get_item", "GetItem"}:
            item_id = ItemId.from_any(data.get("item_id"))
            it = await state.db.get_item(item_id)
            if not it:
                return err(req_id, "item not found")
            return ok(req_id, {"item": it.to_public_dict()})

        # ------------------------------
        # AddItemToCart
        # ------------------------------
        if action in {"add_item_to_cart", "AddItemToCart", "add_to_cart"}:
            item_id = ItemId.from_any(data.get("item_id"))
            qty = int(data.get("qty") if data.get("qty") is not None else data.get("quantity", 1))
            if qty <= 0:
                return err(req_id, "quantity must be > 0")
            it = await state.db.get_item(item_id)
            if not it:
                return err(req_id, "item not found")
            if int(it.quantity) <= 0:
                return err(req_id, "item is not available")

            async with state.carts_lock:
                cart = state.carts.setdefault(int(buyer_id), {})
                k = item_id.key()
                already = int(cart.get(k, 0))
                # ensure we don't exceed current inventory when adding
                if already + qty > int(it.quantity):
                    return err(req_id, "insufficient inventory")
                cart[k] = already + qty
                cart_snapshot = dict(cart)

            return ok(req_id, {"buyer_id": int(buyer_id), "cart": cart_snapshot})

        # ------------------------------
        # RemoveItemFromCart
        # ------------------------------
        if action in {"remove_item_from_cart", "RemoveItemFromCart", "remove_from_cart"}:
            item_id = ItemId.from_any(data.get("item_id"))
            qty = int(data.get("qty") if data.get("qty") is not None else data.get("quantity", 1))
            if qty <= 0:
                return err(req_id, "quantity must be > 0")

            async with state.carts_lock:
                cart = state.carts.setdefault(int(buyer_id), {})
                k = item_id.key()
                if k not in cart:
                    return err(req_id, "item not in cart")
                cur = int(cart[k])
                if qty > cur:
                    return err(req_id, "cannot remove more than current quantity in cart")
                new_qty = cur - qty
                if new_qty == 0:
                    cart.pop(k, None)
                else:
                    cart[k] = new_qty
                cart_snapshot = dict(cart)

            return ok(req_id, {"buyer_id": int(buyer_id), "cart": cart_snapshot})

        # ------------------------------
        # SaveCart
        # ------------------------------
        if action in {"save_cart", "SaveCart"}:
            async with state.carts_lock:
                cart = dict(state.carts.get(int(buyer_id), {}))
            await state.db.save_cart(int(buyer_id), cart)
            return ok(req_id, {"saved": True, "cart": cart})

        # ------------------------------
        # ClearCart
        # ------------------------------
        if action in {"clear_cart", "ClearCart"}:
            async with state.carts_lock:
                state.carts[int(buyer_id)] = {}
            return ok(req_id, {"cleared": True})

        # ------------------------------
        # DisplayCart
        # ------------------------------
        if action in {"display_cart", "DisplayCart"}:
            async with state.carts_lock:
                cart = dict(state.carts.get(int(buyer_id), {}))
            # return as a list of item_id + qty
            items = [{"item_id": ItemId.from_any(k).to_dict(), "qty": int(v)} for k, v in cart.items()]
            return ok(req_id, {"cart": items})

        # ------------------------------
        # MakePurchase
        # ------------------------------
        if action in {"make_purchase", "MakePurchase", "checkout"}:
            out = await _do_purchase(state, int(buyer_id))
            return ok(req_id, out)

        # ------------------------------
        # ProvideFeedback (thumbs up/down on an item)
        # ------------------------------
        if action in {"provide_feedback", "ProvideFeedback"}:
            item_id = ItemId.from_any(data.get("item_id"))
            fb = data.get("feedback")
            # Accept: {"thumbs_up":1,"thumbs_down":0} OR "up"/"down"
            up = 0
            down = 0
            if isinstance(fb, str):
                s = fb.strip().lower()
                if s in {"up", "thumbs_up", "thumbsup", "1"}:
                    up = 1
                elif s in {"down", "thumbs_down", "thumbsdown", "-1"}:
                    down = 1
                else:
                    return err(req_id, "feedback must be 'up' or 'down'")
            elif isinstance(fb, dict):
                up = int(fb.get("thumbs_up", 0))
                down = int(fb.get("thumbs_down", 0))
                if (up, down) not in {(1, 0), (0, 1)}:
                    return err(req_id, "feedback dict must be either {thumbs_up:1,thumbs_down:0} or {thumbs_up:0,thumbs_down:1}")
            else:
                # also accept direct field
                s = str(data.get("thumb") or data.get("vote") or "").strip().lower()
                if s in {"up", "down"}:
                    up = 1 if s == "up" else 0
                    down = 1 if s == "down" else 0
                else:
                    return err(req_id, "feedback is required ('up' or 'down')")

            await state.db.add_item_feedback(item_id, thumbs_up=up, thumbs_down=down)
            it = await state.db.get_item(item_id)
            assert it is not None
            return ok(req_id, {"item_id": item_id.to_dict(), "item_feedback": it.feedback.to_dict()})

        # ------------------------------
        # GetSellerRating (given seller_id)
        # ------------------------------
        if action in {"get_seller_rating", "GetSellerRating"}:
            seller_id = int(data.get("seller_id"))
            s = await state.db.get_seller(seller_id)
            if not s:
                return err(req_id, "seller not found")
            return ok(req_id, {"seller_id": int(s.seller_id), "seller_feedback": s.feedback.to_dict()})

        # ------------------------------
        # GetBuyerPurchases (history of item IDs purchased by this buyer)
        # ------------------------------
        if action in {"get_buyer_purchases", "GetBuyerPurchases"}:
            txns = await state.db.list_transactions_for_buyer(int(buyer_id))
            history: List[Dict[str, Any]] = []
            for t in txns:
                for line in t.items:
                    history.append({"item_id": line.item_id.to_dict(), "qty": int(line.qty), "txn_id": t.txn_id})
            return ok(req_id, {"buyer_id": int(buyer_id), "purchases": history, "transactions": [t.to_public_dict() for t in txns]})

        return err(req_id, f"unknown buyer action: {action}")
    except KeyError as e:
        return err(req_id, f"missing field: {e}")
    except ValueError as e:
        return err(req_id, str(e))
    except Exception as e:
        return err(req_id, str(e))
