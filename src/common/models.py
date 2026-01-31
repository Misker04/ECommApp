from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Any, Tuple
import time
import uuid


def new_id(prefix: str) -> str:
    # Used for internal IDs like transactions
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ------------------------------
# Feedback (thumbs up/down)
# ------------------------------


@dataclass
class Feedback:
    thumbs_up: int = 0
    thumbs_down: int = 0

    def to_tuple(self) -> Tuple[int, int]:
        return (int(self.thumbs_up), int(self.thumbs_down))

    def to_dict(self) -> Dict[str, int]:
        return {"thumbs_up": int(self.thumbs_up), "thumbs_down": int(self.thumbs_down)}


# ------------------------------
# Sellers / Buyers
# ------------------------------


@dataclass
class Seller:
    seller_id: int                # unique integer provided by server
    name: str                     # <= 32 chars, may not be unique
    feedback: Feedback = field(default_factory=Feedback)  # starts at <0,0>
    items_sold: int = 0           # starts at 0

    password_hash: str = ""
    created_at: float = field(default_factory=lambda: time.time())

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "seller_name": self.name,
            "seller_id": int(self.seller_id),
            "seller_feedback": self.feedback.to_dict(),
            "items_sold": int(self.items_sold),
            "created_at": float(self.created_at),
        }


@dataclass
class Buyer:
    buyer_id: int                 # unique integer provided by server
    name: str                     # <= 32 chars, may not be unique
    items_purchased: int = 0      # starts at 0

    password_hash: str = ""
    created_at: float = field(default_factory=lambda: time.time())

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "buyer_name": self.name,
            "buyer_id": int(self.buyer_id),
            "items_purchased": int(self.items_purchased),
            "created_at": float(self.created_at),
        }


# ------------------------------
# Items
# ------------------------------


ItemCondition = Literal["new", "used"]


@dataclass(frozen=True)
class ItemId:
    """Assignment-required unique identifier: <item category, integer>."""

    category: int
    number: int

    def key(self) -> str:
        # Internal stable key for dicts / serialization
        return f"{self.category}:{self.number}"

    def to_dict(self) -> Dict[str, int]:
        return {"category": int(self.category), "number": int(self.number)}

    @staticmethod
    def from_any(v: Any) -> "ItemId":
        """Accepts ItemId, dict, list/tuple, or string like 'category:number'."""
        if isinstance(v, ItemId):
            return v
        if isinstance(v, dict):
            return ItemId(category=int(v["category"]), number=int(v["number"]))
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return ItemId(category=int(v[0]), number=int(v[1]))
        if isinstance(v, str):
            # Common CLI-friendly form: "<category>:<number>"
            parts = v.split(":")
            if len(parts) != 2:
                raise ValueError("invalid item_id string; expected 'category:number'")
            return ItemId(category=int(parts[0]), number=int(parts[1]))
        raise ValueError("invalid item_id")


@dataclass
class Item:
    item_id: ItemId
    seller_id: int
    name: str  # <= 32 chars (item names may not be unique)
    category: int
    keywords: List[str]  # <=5, each <=8 chars
    condition: ItemCondition  # new | used
    sale_price: float  # assigned/updated by seller
    quantity: int      # maintained by server as items are sold/updated
    feedback: Feedback = field(default_factory=Feedback)  # starts at <0,0>
    created_at: float = field(default_factory=lambda: time.time())

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id.to_dict(),  # <category, integer>
            "item_name": self.name,
            "item_category": int(self.category),
            "keywords": list(self.keywords),
            "condition": self.condition,
            "sale_price": float(self.sale_price),
            "item_quantity": int(self.quantity),
            "seller_id": int(self.seller_id),
            "item_feedback": self.feedback.to_dict(),  # <thumbs_up, thumbs_down>
            "created_at": float(self.created_at),
        }


# ------------------------------
# Financial transactions
# ------------------------------


@dataclass
class TransactionLine:
    item_id: ItemId
    seller_id: int
    qty: int
    price_each: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id.to_dict(),
            "seller_id": int(self.seller_id),
            "qty": int(self.qty),
            "price_each": float(self.price_each),
        }


@dataclass
class Transaction:
    txn_id: str
    buyer_id: int
    items: List[TransactionLine]
    total: float
    created_at: float = field(default_factory=lambda: time.time())

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "txn_id": self.txn_id,
            "buyer_id": int(self.buyer_id),
            "items": [i.to_dict() for i in self.items],
            "total": float(self.total),
            "created_at": float(self.created_at),
        }


# ------------------------------
# Backward-compat shims
# ------------------------------


@dataclass
class Customer:
    customer_id: str
    username: str
    password_hash: str
    role: Literal["buyer", "seller"]
    created_at: float = field(default_factory=lambda: time.time())


@dataclass
class Product:
    product_id: str
    seller_id: str
    name: str
    price_cents: int
    quantity: int
    created_at: float = field(default_factory=lambda: time.time())
