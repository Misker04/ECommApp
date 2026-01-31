from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional, List

from src.common.models import Seller, Buyer, Item, ItemId, Transaction


@dataclass
class InMemoryDB:
    # Sellers/Buyers
    sellers_by_id: Dict[int, Seller] = field(default_factory=dict)
    sellers_by_name: Dict[str, List[int]] = field(default_factory=dict)

    buyers_by_id: Dict[int, Buyer] = field(default_factory=dict)
    buyers_by_name: Dict[str, List[int]] = field(default_factory=dict)

    next_seller_id: int = 1
    next_buyer_id: int = 1

    # Items keyed by "<category>:<number>"
    items_by_key: Dict[str, Item] = field(default_factory=dict)
    items_by_seller: Dict[int, List[str]] = field(default_factory=dict)

    # Per-category item counters: <item category, integer>
    next_item_number_by_category: Dict[int, int] = field(default_factory=dict)

    transactions_by_id: Dict[str, Transaction] = field(default_factory=dict)
    transactions_by_buyer: Dict[int, List[str]] = field(default_factory=dict)

    # buyer_id -> saved cart mapping "<category>:<number>" -> qty
    saved_carts_by_buyer: Dict[int, Dict[str, int]] = field(default_factory=dict)

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------
    # Seller / Buyer management
    # ------------------------------

    async def add_seller(self, name: str, password_hash: str) -> Seller:
        name = str(name)
        if len(name) > 32:
            raise ValueError("seller_name must be <= 32 characters")
        async with self.lock:
            sid = int(self.next_seller_id)
            self.next_seller_id += 1
            s = Seller(seller_id=sid, name=name, password_hash=password_hash)
            self.sellers_by_id[sid] = s
            self.sellers_by_name.setdefault(name, []).append(sid)
            return s

    async def add_buyer(self, name: str, password_hash: str) -> Buyer:
        name = str(name)
        if len(name) > 32:
            raise ValueError("buyer_name must be <= 32 characters")
        async with self.lock:
            bid = int(self.next_buyer_id)
            self.next_buyer_id += 1
            b = Buyer(buyer_id=bid, name=name, password_hash=password_hash)
            self.buyers_by_id[bid] = b
            self.buyers_by_name.setdefault(name, []).append(bid)
            return b

    async def get_seller(self, seller_id: int) -> Optional[Seller]:
        async with self.lock:
            return self.sellers_by_id.get(int(seller_id))

    async def get_buyer(self, buyer_id: int) -> Optional[Buyer]:
        async with self.lock:
            return self.buyers_by_id.get(int(buyer_id))

    async def find_sellers_by_name(self, name: str) -> List[Seller]:
        async with self.lock:
            ids = list(self.sellers_by_name.get(str(name), []))
            return [self.sellers_by_id[i] for i in ids if i in self.sellers_by_id]

    async def find_buyers_by_name(self, name: str) -> List[Buyer]:
        async with self.lock:
            ids = list(self.buyers_by_name.get(str(name), []))
            return [self.buyers_by_id[i] for i in ids if i in self.buyers_by_id]

    async def inc_seller_items_sold(self, seller_id: int, delta: int) -> None:
        async with self.lock:
            s = self.sellers_by_id.get(int(seller_id))
            if not s:
                raise ValueError("seller not found")
            s.items_sold = int(s.items_sold) + int(delta)

    async def inc_buyer_items_purchased(self, buyer_id: int, delta: int) -> None:
        async with self.lock:
            b = self.buyers_by_id.get(int(buyer_id))
            if not b:
                raise ValueError("buyer not found")
            b.items_purchased = int(b.items_purchased) + int(delta)

    # ------------------------------
    # Items
    # ------------------------------

    async def allocate_item_id(self, category: int) -> ItemId:
        """Allocates a new ItemId for a given category (atomic)."""
        async with self.lock:
            c = int(category)
            n = int(self.next_item_number_by_category.get(c, 1))
            self.next_item_number_by_category[c] = n + 1
            return ItemId(category=c, number=n)

    async def add_item(self, item: Item) -> None:
        async with self.lock:
            k = item.item_id.key()
            if k in self.items_by_key:
                raise ValueError("item_id already exists")
            self.items_by_key[k] = item
            self.items_by_seller.setdefault(int(item.seller_id), []).append(k)

    async def list_items(self) -> List[Item]:
        async with self.lock:
            return list(self.items_by_key.values())

    async def get_item(self, item_id: ItemId) -> Optional[Item]:
        async with self.lock:
            return self.items_by_key.get(item_id.key())

    async def update_item_quantity(self, item_id: ItemId, new_qty: int) -> None:
        async with self.lock:
            it = self.items_by_key.get(item_id.key())
            if not it:
                raise ValueError("item not found")
            it.quantity = int(new_qty)

    async def update_item_price(self, item_id: ItemId, new_price: float) -> None:
        async with self.lock:
            it = self.items_by_key.get(item_id.key())
            if not it:
                raise ValueError("item not found")
            it.sale_price = float(new_price)

    async def add_item_feedback(self, item_id: ItemId, thumbs_up: int = 0, thumbs_down: int = 0) -> None:
        up = int(thumbs_up)
        down = int(thumbs_down)
        if up < 0 or down < 0:
            raise ValueError("feedback deltas must be >= 0")
        async with self.lock:
            it = self.items_by_key.get(item_id.key())
            if not it:
                raise ValueError("item not found")
            it.feedback.thumbs_up = int(it.feedback.thumbs_up) + up
            it.feedback.thumbs_down = int(it.feedback.thumbs_down) + down

    async def list_items_for_seller(self, seller_id: int) -> List[Item]:
        async with self.lock:
            keys = list(self.items_by_seller.get(int(seller_id), []))
            return [self.items_by_key[k] for k in keys if k in self.items_by_key]

    async def remove_units_for_sale(self, item_id: ItemId, remove_qty: int) -> int:
        """Decrease quantity by remove_qty, returns new quantity."""
        rq = int(remove_qty)
        if rq < 0:
            raise ValueError("remove_qty must be >= 0")
        async with self.lock:
            it = self.items_by_key.get(item_id.key())
            if not it:
                raise ValueError("item not found")
            if it.quantity < rq:
                raise ValueError("insufficient units to remove")
            it.quantity = int(it.quantity) - rq
            return int(it.quantity)

    # ------------------------------
    # Carts (saved across sessions)
    # ------------------------------

    async def save_cart(self, buyer_id: int, cart: Dict[str, int]) -> None:
        """Persist a buyer's cart so it survives logout/login."""
        bid = int(buyer_id)
        clean: Dict[str, int] = {}
        for k, v in (cart or {}).items():
            q = int(v)
            if q > 0:
                clean[str(k)] = q
        async with self.lock:
            self.saved_carts_by_buyer[bid] = dict(clean)

    async def load_saved_cart(self, buyer_id: int) -> Dict[str, int]:
        bid = int(buyer_id)
        async with self.lock:
            return dict(self.saved_carts_by_buyer.get(bid, {}))

    async def clear_saved_cart(self, buyer_id: int) -> None:
        bid = int(buyer_id)
        async with self.lock:
            self.saved_carts_by_buyer.pop(bid, None)

    # ------------------------------
    # Transactions
    # ------------------------------

    async def add_transaction(self, txn: Transaction) -> None:
        async with self.lock:
            self.transactions_by_id[txn.txn_id] = txn
            self.transactions_by_buyer.setdefault(int(txn.buyer_id), []).append(txn.txn_id)

    async def list_transactions_for_buyer(self, buyer_id: int) -> List[Transaction]:
        bid = int(buyer_id)
        async with self.lock:
            ids = list(self.transactions_by_buyer.get(bid, []))
            return [self.transactions_by_id[i] for i in ids if i in self.transactions_by_id]
