from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ItemRecord:
    item_id: str
    seller_id: int
    name: str
    category: int
    price: float
    quantity: int
    thumbs_up: int = 0
    thumbs_down: int = 0


@dataclass
class ProductStore:
    items: Dict[str, ItemRecord] = field(default_factory=dict)
    items_by_seller: Dict[int, List[str]] = field(default_factory=dict)
    carts: Dict[int, Dict[str, int]] = field(default_factory=dict)
    next_item_number_by_category: Dict[int, int] = field(default_factory=dict)

    @staticmethod
    def _normalize_price(price: float) -> float:
        price = float(price)
        if price <= 0:
            raise ValueError("price must be positive")
        return price

    @staticmethod
    def _normalize_quantity(quantity: int, *, allow_zero: bool) -> int:
        quantity = int(quantity)
        if allow_zero:
            if quantity < 0:
                raise ValueError("quantity cannot be negative")
        else:
            if quantity <= 0:
                raise ValueError("quantity must be positive")
        return quantity

    def allocate_item_id(self, category: int) -> str:
        category = int(category)
        num = self.next_item_number_by_category.get(category, 1)
        self.next_item_number_by_category[category] = num + 1
        return f"{category}:{num}"

    def register_item(self, seller_id: int, name: str, category: int, price: float, quantity: int) -> str:
        name = str(name).strip()
        if not name:
            raise ValueError("item name cannot be empty")

        category = int(category)
        price = self._normalize_price(price)
        quantity = self._normalize_quantity(quantity, allow_zero=True)

        item_id = self.allocate_item_id(category)
        self.items[item_id] = ItemRecord(
            item_id=item_id,
            seller_id=int(seller_id),
            name=name,
            category=category,
            price=price,
            quantity=quantity,
        )
        self.items_by_seller.setdefault(int(seller_id), []).append(item_id)
        return item_id

    def change_price(self, seller_id: int, item_id: str, new_price: float) -> None:
        item = self.items.get(item_id)
        if not item or item.seller_id != int(seller_id):
            raise ValueError("item not owned by seller")
        item.price = self._normalize_price(new_price)

    def update_quantity(self, seller_id: int, item_id: str, new_quantity: int) -> None:
        item = self.items.get(item_id)
        if not item or item.seller_id != int(seller_id):
            raise ValueError("item not owned by seller")
        item.quantity = self._normalize_quantity(new_quantity, allow_zero=True)

    def display_items_for_seller(self, seller_id: int) -> list[ItemRecord]:
        return [
            self.items[item_id]
            for item_id in sorted(self.items_by_seller.get(int(seller_id), []))
            if item_id in self.items
        ]

    def search_items(self, category: int) -> list[ItemRecord]:
        category = int(category)
        return sorted(
            [item for item in self.items.values() if item.category == category and item.quantity > 0],
            key=lambda item: item.item_id,
        )

    def get_item(self, item_id: str) -> ItemRecord:
        item = self.items.get(item_id)
        if not item:
            raise ValueError("item not found")
        return item

    def add_to_cart(self, buyer_id: int, item_id: str, quantity: int) -> None:
        quantity = self._normalize_quantity(quantity, allow_zero=False)
        item = self.get_item(item_id)
        cart = self.carts.setdefault(int(buyer_id), {})
        new_qty = cart.get(item_id, 0) + quantity
        if new_qty > item.quantity:
            raise ValueError("insufficient inventory")
        cart[item_id] = new_qty

    def remove_from_cart(self, buyer_id: int, item_id: str, quantity: int) -> None:
        quantity = self._normalize_quantity(quantity, allow_zero=False)
        cart = self.carts.setdefault(int(buyer_id), {})
        if item_id not in cart:
            raise ValueError("item not in cart")
        remaining = cart[item_id] - quantity
        if remaining < 0:
            raise ValueError("cannot remove more than cart quantity")
        if remaining == 0:
            cart.pop(item_id, None)
        else:
            cart[item_id] = remaining

    def clear_cart(self, buyer_id: int) -> None:
        self.carts[int(buyer_id)] = {}

    def get_cart(self, buyer_id: int) -> Dict[str, int]:
        return dict(self.carts.get(int(buyer_id), {}))

    def add_feedback(self, item_id: str, positive: bool) -> None:
        item = self.get_item(item_id)
        if positive:
            item.thumbs_up += 1
        else:
            item.thumbs_down += 1

    def finalize_purchase(self, buyer_id: int) -> tuple[list[str], int]:
        cart = dict(self.carts.get(int(buyer_id), {}))
        if not cart:
            raise ValueError("cart is empty")

        for item_id, qty in cart.items():
            item = self.get_item(item_id)
            if int(qty) > item.quantity:
                raise ValueError(f"insufficient inventory for {item_id}")

        item_ids: list[str] = []
        total_units = 0

        for item_id, qty in cart.items():
            item = self.get_item(item_id)
            qty = int(qty)
            item.quantity -= qty
            item_ids.extend([item_id] * qty)
            total_units += qty

        self.carts[int(buyer_id)] = {}
        return item_ids, total_units