from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Literal


MessageType = Literal["REQUEST", "SEQUENCE", "RETRANSMIT"]


@dataclass
class BaseWireMessage:
    type: MessageType
    source_id: int
    known_request_vector: Dict[str, int]
    known_sequence_contig: int
    delivered_contig: int

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode("utf-8")


@dataclass
class RequestWireMessage(BaseWireMessage):
    origin_id: int
    local_seq: int
    request_id: str
    op: Dict[str, Any]


@dataclass
class SequenceWireMessage(BaseWireMessage):
    sequencer_id: int
    global_seq: int
    request_id: str


@dataclass
class RetransmitWireMessage(BaseWireMessage):
    target_id: int
    missing_requests: List[str]
    missing_sequences: List[int]


def parse_message(raw: bytes) -> Dict[str, Any]:
    msg = json.loads(raw.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("wire message must decode to a JSON object")
    msg_type = msg.get("type")
    if msg_type not in {"REQUEST", "SEQUENCE", "RETRANSMIT"}:
        raise ValueError(f"unknown message type: {msg_type}")
    return msg