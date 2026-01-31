import asyncio
import pytest

from src.common.protocol import encode_message, read_message


@pytest.mark.asyncio
async def test_roundtrip():
    # use streamreader with fed bytes
    reader = asyncio.StreamReader()
    msg = {"req_id": "1", "role": "buyer", "action": "ping", "data": {}}
    reader.feed_data(encode_message(msg))
    reader.feed_eof()
    out = await read_message(reader)
    assert out["action"] == "ping"
