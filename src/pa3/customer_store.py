from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List
import uuid


def new_session_token() -> str:
    return uuid.uuid4().hex


@dataclass
class BuyerRecord:
    buyer_id: int
    username: str
    password: str
    purchased_units: int = 0


@dataclass
class SellerRecord:
    seller_id: int
    username: str
    password: str
    thumbs_up: int = 0
    thumbs_down: int = 0


@dataclass
class SessionRecord:
    role: str
    user_id: int
    session_token: str
    last_activity_ms: int


@dataclass
class CustomerStore:
    next_buyer_id: int = 1
    next_seller_id: int = 1
    buyers_by_id: Dict[int, BuyerRecord] = field(default_factory=dict)
    sellers_by_id: Dict[int, SellerRecord] = field(default_factory=dict)
    buyer_ids_by_username: Dict[str, int] = field(default_factory=dict)
    seller_ids_by_username: Dict[str, int] = field(default_factory=dict)
    sessions: Dict[str, SessionRecord] = field(default_factory=dict)
    buyer_purchases: Dict[int, List[str]] = field(default_factory=dict)

    @staticmethod
    def _normalize_role(role: str) -> str:
        role = str(role).strip().lower()
        if role not in {"buyer", "seller"}:
            raise ValueError("invalid role")
        return role

    @staticmethod
    def _normalize_username(username: str) -> str:
        username = str(username).strip()
        if not username:
            raise ValueError("username cannot be empty")
        if len(username) > 32:
            raise ValueError("username must be 32 characters or fewer")
        return username

    @staticmethod
    def _normalize_password(password: str) -> str:
        password = str(password)
        if not password:
            raise ValueError("password cannot be empty")
        return password

    @staticmethod
    def _timeout_ms(timeout_seconds: int) -> int:
        return max(1, int(timeout_seconds)) * 1000

    def _session_if_active(self, session_token: str, now_ms: int, timeout_seconds: int) -> SessionRecord | None:
        session = self.sessions.get(str(session_token))
        if session is None:
            return None
        if int(now_ms) - int(session.last_activity_ms) >= self._timeout_ms(timeout_seconds):
            return None
        return session

    def create_account(self, role: str, username: str, password: str) -> int:
        role = self._normalize_role(role)
        username = self._normalize_username(username)
        password = self._normalize_password(password)

        if role == "buyer":
            if username in self.buyer_ids_by_username:
                raise ValueError("buyer account already exists")
            buyer_id = self.next_buyer_id
            self.next_buyer_id += 1
            self.buyers_by_id[buyer_id] = BuyerRecord(
                buyer_id=buyer_id,
                username=username,
                password=password,
            )
            self.buyer_ids_by_username[username] = buyer_id
            self.buyer_purchases.setdefault(buyer_id, [])
            return buyer_id

        if username in self.seller_ids_by_username:
            raise ValueError("seller account already exists")
        seller_id = self.next_seller_id
        self.next_seller_id += 1
        self.sellers_by_id[seller_id] = SellerRecord(
            seller_id=seller_id,
            username=username,
            password=password,
        )
        self.seller_ids_by_username[username] = seller_id
        return seller_id

    def login(
        self,
        role: str,
        username: str,
        password: str,
        session_token: str,
        activity_ms: int,
    ) -> tuple[str, int]:
        role = self._normalize_role(role)
        username = self._normalize_username(username)
        password = self._normalize_password(password)
        session_token = str(session_token).strip()
        if not session_token:
            raise ValueError("session token cannot be empty")

        if role == "buyer":
            buyer_id = self.buyer_ids_by_username.get(username)
            buyer = self.buyers_by_id.get(int(buyer_id or 0))
            if buyer is None or buyer.password != password:
                raise ValueError("invalid credentials")
            user_id = buyer.buyer_id
        else:
            seller_id = self.seller_ids_by_username.get(username)
            seller = self.sellers_by_id.get(int(seller_id or 0))
            if seller is None or seller.password != password:
                raise ValueError("invalid credentials")
            user_id = seller.seller_id

        if session_token in self.sessions:
            raise ValueError("session token already exists")

        self.sessions[session_token] = SessionRecord(
            role=role,
            user_id=user_id,
            session_token=session_token,
            last_activity_ms=int(activity_ms),
        )
        return session_token, user_id

    def logout(self, session_token: str) -> None:
        self.sessions.pop(str(session_token), None)

    def touch_session(self, session_token: str, activity_ms: int, timeout_seconds: int = 300) -> bool:
        session = self._session_if_active(session_token, int(activity_ms), timeout_seconds)
        if session is None:
            return False
        session.last_activity_ms = max(int(session.last_activity_ms), int(activity_ms))
        return True

    def validate_session(
        self,
        session_token: str,
        role: str,
        now_ms: int,
        timeout_seconds: int,
    ) -> tuple[bool, int]:
        session = self._session_if_active(session_token, int(now_ms), timeout_seconds)
        if session is None:
            return False, 0
        if session.role != self._normalize_role(role):
            return False, 0
        return True, int(session.user_id)

    def record_purchase(
        self,
        session_token: str,
        item_ids: list[str],
        total_units: int,
        activity_ms: int,
        timeout_seconds: int = 300,
    ) -> None:
        session = self._session_if_active(session_token, int(activity_ms), timeout_seconds)
        if session is None or session.role != "buyer":
            raise ValueError("invalid buyer session")

        buyer = self.buyers_by_id.get(int(session.user_id))
        if buyer is None:
            raise ValueError("unknown buyer")

        purchases = self.buyer_purchases.setdefault(int(session.user_id), [])
        purchases.extend(str(item_id) for item_id in item_ids)
        buyer.purchased_units += max(0, int(total_units))
        session.last_activity_ms = max(int(session.last_activity_ms), int(activity_ms))

    def get_seller_rating(self, seller_id: int) -> tuple[int, int]:
        seller = self.sellers_by_id.get(int(seller_id))
        if seller is None:
            raise ValueError("unknown seller")
        return int(seller.thumbs_up), int(seller.thumbs_down)

    def record_seller_feedback(self, seller_id: int, positive: bool) -> None:
        seller = self.sellers_by_id.get(int(seller_id))
        if seller is None:
            raise ValueError("unknown seller")
        if positive:
            seller.thumbs_up += 1
        else:
            seller.thumbs_down += 1

    def get_buyer_purchases(
        self,
        session_token: str,
        now_ms: int,
        timeout_seconds: int,
    ) -> list[str]:
        session = self._session_if_active(session_token, int(now_ms), timeout_seconds)
        if session is None or session.role != "buyer":
            raise ValueError("invalid buyer session")
        return list(self.buyer_purchases.get(int(session.user_id), []))
