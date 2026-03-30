from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.server.db.inmemory import InMemoryDB


# ------------------------------------------------------------------
# Session is now a pure data object with no lock — it is stored
# and retrieved from the DB, not from an in-memory dict on the
# frontend. This keeps the frontend fully stateless per PA2.
# ------------------------------------------------------------------

@dataclass
class Session:
    principal_id: int
    role: str  # "buyer" | "seller"


@dataclass
class MarketState:
    db: InMemoryDB = field(default_factory=InMemoryDB)

    # ------------------------------------------------------------------
    # Session Management — delegates entirely to DB
    # ------------------------------------------------------------------

    async def create_session(self, role: str, principal_id: int, token: str | None = None) -> str:
        """
        Create a new session in the DB and return the token.
        No in-memory state is touched.
        """
        token = await self.db.create_session(role=role, principal_id=int(principal_id), token=token)
        return token

    async def get_session(self, token: str) -> Optional[Session]:
        """
        Look up a session from the DB by token.
        Returns None if token is missing or expired.
        """
        if not token:
            return None
        sess_data = await self.db.get_session(token)
        if not sess_data:
            return None
        return Session(
            principal_id=int(sess_data["principal_id"]),
            role=str(sess_data["role"]),
        )

    async def delete_session(self, token: str) -> None:
        """Remove a session from the DB."""
        if token:
            await self.db.delete_session(token)

    # ------------------------------------------------------------------
    # Cart Management — delegates entirely to DB
    # No in-memory carts dict. All cart reads/writes go to the DB.
    # ------------------------------------------------------------------

    async def get_cart(self, buyer_id: int) -> dict:
        """Load the active cart for this buyer from the DB."""
        return await self.db.load_saved_cart(int(buyer_id))

    async def set_cart(self, buyer_id: int, cart: dict) -> None:
        """Persist the active cart for this buyer to the DB."""
        await self.db.save_cart(int(buyer_id), cart)

    async def clear_cart(self, buyer_id: int) -> None:
        """Clear the buyer's cart in the DB."""
        await self.db.save_cart(int(buyer_id), {})
