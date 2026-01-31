from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from src.common.protocol import read_message, send_message


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int


class _Conn:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def ensure_open(self) -> None:
        if self.writer is not None and not self.writer.is_closing():
            return
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None


class TcpClientPool:
    """Small connection pool for backend-to-backend calls.

    This stores NO per-user state; it is safe under the "stateless frontend" requirement.
    """

    def __init__(self, endpoint: Endpoint, size: int = 4):
        self.endpoint = endpoint
        self.size = max(1, int(size))
        self._q: asyncio.Queue[_Conn] = asyncio.Queue()
        self._init_done = False
        self._init_lock = asyncio.Lock()

    async def _init(self) -> None:
        if self._init_done:
            return
        async with self._init_lock:
            if self._init_done:
                return
            for _ in range(self.size):
                self._q.put_nowait(_Conn(self.endpoint.host, self.endpoint.port))
            self._init_done = True

    async def call(self, msg: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        await self._init()
        conn = await self._q.get()
        try:
            await conn.ensure_open()
            assert conn.reader is not None and conn.writer is not None
            await send_message(conn.writer, msg)
            resp = await asyncio.wait_for(read_message(conn.reader), timeout=timeout)
            return resp
        except Exception:
            # If anything goes wrong, drop the connection so the next user gets a fresh one.
            await conn.close()
            raise
        finally:
            self._q.put_nowait(conn)
