from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from src.common.protocol import read_message, send_message


class MarketplaceClient:
    def __init__(self, host: str, port: int, role: str):
        self.host = host
        self.port = port
        self.role = role
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        # Set after login; automatically sent with each request
        self.session_token: str | None = None

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None

    async def __aenter__(self) -> "MarketplaceClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.close()
        return False

    async def request(self, action: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.reader or not self.writer:
            raise RuntimeError("not connected")
        req_id = uuid.uuid4().hex
        payload = dict(data or {})
        # Attach session token unless caller overrides it explicitly
        if self.session_token and "session_token" not in payload:
            payload["session_token"] = self.session_token

        req = {"req_id": req_id, "role": self.role, "action": action, "data": payload}
        await send_message(self.writer, req)
        resp = await read_message(self.reader)
        return resp
