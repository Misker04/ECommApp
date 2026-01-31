from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Dict, Optional

_LEN = struct.Struct(">I")  # 4-byte big-endian unsigned length


class ProtocolError(Exception):
    pass


def encode_message(obj: Dict[str, Any]) -> bytes:
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _LEN.pack(len(payload)) + payload


async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError as e:
        raise ProtocolError("connection closed while reading") from e


async def read_message(reader: asyncio.StreamReader, max_bytes: int = 4 * 1024 * 1024) -> Dict[str, Any]:
    header = await read_exactly(reader, _LEN.size)
    (n,) = _LEN.unpack(header)
    if n <= 0 or n > max_bytes:
        raise ProtocolError(f"invalid payload length: {n}")
    payload = await read_exactly(reader, n)
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception as e:
        raise ProtocolError("invalid json payload") from e
    if not isinstance(obj, dict):
        raise ProtocolError("payload must be a JSON object")
    return obj


async def send_message(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    writer.write(encode_message(obj))
    await writer.drain()
