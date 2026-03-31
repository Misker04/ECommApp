from __future__ import annotations

import copy
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


RequestId = Tuple[int, int]


@dataclass
class PendingResult:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[str] = None


class RotatingSequencerGroup:
    """
    Small rotating-sequencer atomic broadcast helper used by the customer replicas.

    The code stays intentionally direct:
    - every client mutation becomes a Request message
    - the sequencer for global sequence k is member k mod n
    - replicas deliver only in sequence order after enough metadata shows
      that a majority has seen the needed traffic
    """

    def __init__(
        self,
        member_id: int,
        members: list[tuple[str, int]],
        apply_fn: Callable[[dict[str, Any]], Any],
        udp_host: str,
        udp_port: int,
    ) -> None:
        self.member_id = int(member_id)
        self.members = list(members)
        self.apply_fn = apply_fn
        self.udp_host = udp_host
        self.udp_port = int(udp_port)
        self.majority = (len(self.members) // 2) + 1

        self.local_request_seq = 0
        self.next_delivery_seq = 0
        self.delivered_seq = -1
        self.next_sequence_for_me = self.member_id

        self.requests: dict[RequestId, dict[str, Any]] = {}
        self.request_from_sender: dict[int, dict[int, RequestId]] = {}
        self.request_meta: dict[RequestId, dict[str, Any]] = {}
        self.assigned_seq_by_request: dict[RequestId, int] = {}
        self.sequence_to_request: dict[int, RequestId] = {}
        self.highest_contiguous_request_by_sender: dict[int, int] = {}
        self.highest_assigned_request_by_sender: dict[int, int] = {}

        self.peer_seen_seq: dict[int, int] = {i: -1 for i in range(len(self.members))}
        self.peer_seen_request: dict[int, dict[int, int]] = {
            i: {j: -1 for j in range(len(self.members))}
            for i in range(len(self.members))
        }

        self.pending_results: dict[RequestId, PendingResult] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.udp_host, self.udp_port))
        self.sock.settimeout(0.2)

        self.receiver_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.sequencer_thread = threading.Thread(target=self._sequencer_loop, daemon=True)
        self.delivery_thread = threading.Thread(target=self._delivery_loop, daemon=True)
        self.retransmit_thread = threading.Thread(target=self._retransmit_loop, daemon=True)

    def start(self) -> None:
        self.receiver_thread.start()
        self.sequencer_thread.start()
        self.delivery_thread.start()
        self.retransmit_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def submit(self, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        with self.lock:
            request_id = (self.member_id, self.local_request_seq)
            self.local_request_seq += 1
            pending = PendingResult()
            self.pending_results[request_id] = pending
            self._record_request(request_id, payload)

        self._broadcast(
            {
                "type": "request",
                "request_id": [request_id[0], request_id[1]],
                "payload": payload,
                "meta": self._local_meta(),
            }
        )

        if not pending.event.wait(timeout):
            raise TimeoutError("timed out waiting for atomic broadcast delivery")
        if pending.error:
            raise RuntimeError(pending.error)
        return pending.result

    def _local_meta(self) -> dict[str, Any]:
        return {
            "from_member": self.member_id,
            "highest_sequence_seen": self._highest_contiguous_sequence_seen(),
            "highest_request_seen": {
                str(sender): int(self.highest_contiguous_request_by_sender.get(sender, -1))
                for sender in range(len(self.members))
            },
            "highest_sequence_delivered": self.delivered_seq,
        }

    def _highest_contiguous_sequence_seen(self) -> int:
        seq = -1
        while (seq + 1) in self.sequence_to_request:
            seq += 1
        return seq

    def _record_request(self, request_id: RequestId, payload: dict[str, Any]) -> None:
        if request_id in self.requests:
            return
        sender_id, local_seq = request_id
        self.requests[request_id] = copy.deepcopy(payload)
        self.request_from_sender.setdefault(sender_id, {})[local_seq] = request_id
        self.request_meta.setdefault(request_id, {})

        seen = self.highest_contiguous_request_by_sender.get(sender_id, -1)
        while (seen + 1) in self.request_from_sender.get(sender_id, {}):
            seen += 1
        self.highest_contiguous_request_by_sender[sender_id] = seen

    def _set_sequence(self, global_seq: int, request_id: RequestId) -> None:
        if global_seq in self.sequence_to_request:
            return
        self.sequence_to_request[global_seq] = request_id
        self.assigned_seq_by_request[request_id] = global_seq
        sender_id, local_seq = request_id
        current = self.highest_assigned_request_by_sender.get(sender_id, -1)
        while True:
            next_local = current + 1
            next_request = self.request_from_sender.get(sender_id, {}).get(next_local)
            if next_request is None or next_request not in self.assigned_seq_by_request:
                break
            current = next_local
        self.highest_assigned_request_by_sender[sender_id] = current

    def _recv_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                data, _addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                message = json.loads(data.decode("utf-8"))
            except Exception:
                continue

            with self.lock:
                self._note_peer_progress(message.get("meta") or {})
                msg_type = str(message.get("type") or "")

                if msg_type == "request":
                    rid_raw = message.get("request_id") or []
                    request_id = (int(rid_raw[0]), int(rid_raw[1]))
                    self._record_request(request_id, message.get("payload") or {})
                    sender_id, local_seq = request_id
                    expected = self.highest_contiguous_request_by_sender.get(sender_id, -1) + 1
                    if local_seq > expected:
                        for missing_seq in range(expected, local_seq):
                            self._send_retransmit_request("request", sender_id, request_id=(sender_id, missing_seq))

                elif msg_type == "sequence":
                    global_seq = int(message["global_seq"])
                    rid_raw = message.get("request_id") or []
                    request_id = (int(rid_raw[0]), int(rid_raw[1]))
                    self._set_sequence(global_seq, request_id)
                    if request_id not in self.requests:
                        self._send_retransmit_request("request", request_id[0], request_id=request_id)
                    missing_from = self._highest_contiguous_sequence_seen() + 1
                    if global_seq > missing_from:
                        for missing_seq in range(missing_from, global_seq):
                            owner = missing_seq % len(self.members)
                            self._send_retransmit_request("sequence", owner, global_seq=missing_seq)

                elif msg_type == "retransmit":
                    kind = str(message.get("kind") or "")
                    if kind == "request":
                        rid_raw = message.get("request_id") or []
                        request_id = (int(rid_raw[0]), int(rid_raw[1]))
                        payload = self.requests.get(request_id)
                        if payload is not None:
                            self._send_to(
                                int(message["requester_id"]),
                                {
                                    "type": "request",
                                    "request_id": [request_id[0], request_id[1]],
                                    "payload": payload,
                                    "meta": self._local_meta(),
                                },
                            )
                    elif kind == "sequence":
                        global_seq = int(message["global_seq"])
                        request_id = self.sequence_to_request.get(global_seq)
                        if request_id is not None:
                            self._send_to(
                                int(message["requester_id"]),
                                {
                                    "type": "sequence",
                                    "global_seq": global_seq,
                                    "request_id": [request_id[0], request_id[1]],
                                    "meta": self._local_meta(),
                                },
                            )

    def _note_peer_progress(self, meta: dict[str, Any]) -> None:
        peer_id = int(meta.get("from_member", -1))
        if peer_id < 0 or peer_id >= len(self.members):
            return
        self.peer_seen_seq[peer_id] = max(
            self.peer_seen_seq.get(peer_id, -1),
            int(meta.get("highest_sequence_seen", -1)),
        )
        highest_request_seen = meta.get("highest_request_seen") or {}
        for sender_text, seen in highest_request_seen.items():
            sender_id = int(sender_text)
            self.peer_seen_request.setdefault(peer_id, {})[sender_id] = max(
                self.peer_seen_request.get(peer_id, {}).get(sender_id, -1),
                int(seen),
            )

    def _sequencer_loop(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.02)
            with self.lock:
                k = self.next_sequence_for_me
                if k % len(self.members) != self.member_id:
                    self.next_sequence_for_me += 1
                    continue
                if not self._sequencer_ready_for(k):
                    continue
                request_id = self._choose_request_for_sequence()
                if request_id is None:
                    continue
                self._set_sequence(k, request_id)
                self.next_sequence_for_me += len(self.members)
                self._broadcast(
                    {
                        "type": "sequence",
                        "global_seq": k,
                        "request_id": [request_id[0], request_id[1]],
                        "meta": self._local_meta(),
                    }
                )

    def _sequencer_ready_for(self, global_seq: int) -> bool:
        for seq in range(global_seq):
            request_id = self.sequence_to_request.get(seq)
            if request_id is None:
                return False
            if request_id not in self.requests:
                return False
        return True

    def _choose_request_for_sequence(self) -> Optional[RequestId]:
        candidates: list[RequestId] = []
        for request_id in self.requests:
            if request_id in self.assigned_seq_by_request:
                continue
            sender_id, local_seq = request_id
            previous_assigned = self.highest_assigned_request_by_sender.get(sender_id, -1)
            if local_seq == previous_assigned + 1:
                candidates.append(request_id)
        if not candidates:
            return None
        candidates.sort()
        return candidates[0]

    def _delivery_loop(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.02)
            deliverable: Optional[tuple[int, RequestId, dict[str, Any]]] = None
            with self.lock:
                seq = self.next_delivery_seq
                request_id = self.sequence_to_request.get(seq)
                if request_id is None:
                    continue
                payload = self.requests.get(request_id)
                if payload is None:
                    self._send_retransmit_request("request", request_id[0], request_id=request_id)
                    continue
                sender_id, local_seq = request_id
                if not self._majority_received(seq, sender_id, local_seq):
                    continue
                deliverable = (seq, request_id, copy.deepcopy(payload))
                self.next_delivery_seq += 1
                self.delivered_seq = seq
                self.peer_seen_seq[self.member_id] = max(self.peer_seen_seq[self.member_id], seq)
                self.peer_seen_request[self.member_id][sender_id] = max(
                    self.peer_seen_request[self.member_id].get(sender_id, -1),
                    local_seq,
                )

            if deliverable is None:
                continue

            seq, request_id, payload = deliverable
            pending = None
            try:
                result = self.apply_fn(payload)
            except Exception as exc:
                result = None
                error_text = str(exc)
            else:
                error_text = None

            with self.lock:
                pending = self.pending_results.get(request_id)
                if pending is not None:
                    pending.result = result
                    pending.error = error_text
                    pending.event.set()

    def _majority_received(self, global_seq: int, sender_id: int, local_seq: int) -> bool:
        count = 0
        for peer_id in range(len(self.members)):
            if peer_id == self.member_id:
                seen_seq = self._highest_contiguous_sequence_seen()
                seen_request = self.highest_contiguous_request_by_sender.get(sender_id, -1)
            else:
                seen_seq = self.peer_seen_seq.get(peer_id, -1)
                seen_request = self.peer_seen_request.get(peer_id, {}).get(sender_id, -1)
            if seen_seq >= global_seq and seen_request >= local_seq:
                count += 1
        return count >= self.majority

    def _retransmit_loop(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.15)
            with self.lock:
                missing_seq = self._highest_contiguous_sequence_seen() + 1
                if missing_seq < self.next_delivery_seq + len(self.members):
                    owner = missing_seq % len(self.members)
                    self._send_retransmit_request("sequence", owner, global_seq=missing_seq)

    def _send_retransmit_request(
        self,
        kind: str,
        target_id: int,
        request_id: Optional[RequestId] = None,
        global_seq: Optional[int] = None,
    ) -> None:
        message = {
            "type": "retransmit",
            "kind": kind,
            "requester_id": self.member_id,
            "meta": self._local_meta(),
        }
        if request_id is not None:
            message["request_id"] = [request_id[0], request_id[1]]
        if global_seq is not None:
            message["global_seq"] = int(global_seq)
        self._send_to(target_id, message)

    def _broadcast(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message).encode("utf-8")
        for host, port in self.members:
            try:
                self.sock.sendto(encoded, (host, port))
            except OSError:
                continue

    def _send_to(self, target_id: int, message: dict[str, Any]) -> None:
        if target_id < 0 or target_id >= len(self.members):
            return
        host, port = self.members[target_id]
        try:
            self.sock.sendto(json.dumps(message).encode("utf-8"), (host, port))
        except OSError:
            pass
