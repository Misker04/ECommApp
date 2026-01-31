from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional
import asyncio
import time
import uuid

from src.server.db.inmemory import InMemoryDB


def _new_session_token() -> str:
    return uuid.uuid4().hex


@dataclass
class Session:
    principal_id: int
    role: str  # "buyer" | "seller"
    created_at: float = field(default_factory=lambda: time.time())


@dataclass
class MarketState:
    db: InMemoryDB = field(default_factory=InMemoryDB)

    # session_token -> Session
    sessions: Dict[str, Session] = field(default_factory=dict)
    sessions_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # buyer_id -> {"<category>:<number>": qty}
    carts: Dict[int, Dict[str, int]] = field(default_factory=dict)
    carts_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def create_session(self, role: str, principal_id: int) -> str:
        token = _new_session_token()
        async with self.sessions_lock:
            self.sessions[token] = Session(principal_id=int(principal_id), role=str(role))
        return token

    async def get_session(self, token: str) -> Optional[Session]:
        if not token:
            return None
        async with self.sessions_lock:
            return self.sessions.get(token)

    async def delete_session(self, token: str) -> None:
        async with self.sessions_lock:
            self.sessions.pop(token, None)
