from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict

from src.pa3.rotating.messages import (
    RequestWireMessage,
    RetransmitWireMessage,
    SequenceWireMessage,
    parse_message,
)
from src.pa3.rotating.state import PendingRequest, RotationState


ApplyFn = Callable[[dict], Awaitable[dict]]


@dataclass(frozen=True)
class UdpPeer:
    replica_id: int
    host: str
    port: int


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes, tuple], None]):
        self.on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.on_datagram(data, addr)


class RotatingSequencerReplica:
    def __init__(self, node_id: int, peers: list[UdpPeer], apply_fn: ApplyFn):
        self.node_id = node_id
        self.peers = list(peers)
        self.peer_by_id = {p.replica_id: p for p in peers}
        self.state = RotationState(node_id=node_id, cluster_size=len(peers))
        self.apply_fn = apply_fn

        self.transport: asyncio.DatagramTransport | None = None
        self.results: Dict[str, asyncio.Future] = {}
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.started = False
        self._last_receipt_advertisement = 0.0
        self._receipt_advertise_interval_s = 0.2
        self._last_pending_rebroadcast = 0.0
        self._pending_rebroadcast_interval_s = 0.5
        self._request_repair_batch = 32
        self._sequence_repair_batch = 64

        self._pump_task: asyncio.Task | None = None
        self._background_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.started:
            return

        loop = asyncio.get_running_loop()
        me = self.peer_by_id[self.node_id]
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self._enqueue_raw),
            local_addr=(me.host, me.port),
        )

        self.transport = transport
        self.started = True
        self._pump_task = asyncio.create_task(self._pump(), name=f"rotating-pump-{self.node_id}")
        self._background_task = asyncio.create_task(self._background(), name=f"rotating-bg-{self.node_id}")
        self._pump_task.add_done_callback(self._log_task_failure)
        self._background_task.add_done_callback(self._log_task_failure)

    def _log_task_failure(self, task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        print(
            f"[rotating replica {self.node_id}] task {task.get_name()} failed: {exc!r}",
            file=sys.stderr,
            flush=True,
        )

    def debug_state(self) -> dict:
        next_deliver = self.state.next_global_seq_to_deliver
        window_end = next_deliver + 16
        return {
            "node_id": self.node_id,
            "next_local_seq": self.state.next_local_seq,
            "next_global_seq_to_assign": self.state.next_global_seq_to_assign,
            "next_global_seq_to_deliver": self.state.next_global_seq_to_deliver,
            "received_sequence_contig": self.state.received_sequence_contig,
            "delivered_contig": self.state.delivered_contig,
            "known_request_vector": dict(self.state.known_request_vector),
            "peer_request_vectors": {
                int(peer_id): dict(vector)
                for peer_id, vector in self.state.peer_request_vectors.items()
            },
            "peer_sequence_contig": {
                int(peer_id): int(contig)
                for peer_id, contig in self.state.peer_sequence_contig.items()
            },
            "request_log_size": len(self.state.request_log),
            "sequence_log_size": len(self.state.sequence_by_global),
            "pending_results": len(self.results),
            "sequence_window": {
                int(global_seq): request_id
                for global_seq, request_id in sorted(self.state.sequence_by_global.items())
                if next_deliver <= global_seq < window_end
            },
        }

    def _enqueue_raw(self, raw: bytes, _: tuple) -> None:
        try:
            self.inbox.put_nowait(parse_message(raw))
        except Exception:
            pass

    def _send(self, replica_id: int, payload: bytes) -> None:
        if self.transport is None:
            raise RuntimeError("replica transport not started")
        peer = self.peer_by_id[replica_id]
        self.transport.sendto(payload, (peer.host, peer.port))

    def _broadcast_to_others(self, payload: bytes) -> None:
        for peer in self.peers:
            if peer.replica_id == self.node_id:
                continue
            self._send(peer.replica_id, payload)

    def _broadcast_receipt_advertisement(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_receipt_advertisement) < self._receipt_advertise_interval_s:
            return

        self._last_receipt_advertisement = now
        self._broadcast_to_others(
            self._retransmit_msg(
                target_id=-1,
                missing_requests=[],
                missing_sequences=[],
            ).to_bytes()
        )

    def _request_msg(
        self,
        *,
        source_id: int,
        origin_id: int,
        local_seq: int,
        request_id: str,
        op: dict,
    ) -> RequestWireMessage:
        return RequestWireMessage(
            type="REQUEST",
            source_id=source_id,
            origin_id=origin_id,
            local_seq=local_seq,
            request_id=request_id,
            op=op,
            known_request_vector=dict(self.state.known_request_vector),
            known_sequence_contig=self.state.received_sequence_contig,
            delivered_contig=self.state.delivered_contig,
        )

    def _sequence_msg(
        self,
        *,
        source_id: int,
        global_seq: int,
        request_id: str,
    ) -> SequenceWireMessage:
        return SequenceWireMessage(
            type="SEQUENCE",
            source_id=source_id,
            sequencer_id=self.state.current_sequencer_for(global_seq),
            global_seq=global_seq,
            request_id=request_id,
            known_request_vector=dict(self.state.known_request_vector),
            known_sequence_contig=self.state.received_sequence_contig,
            delivered_contig=self.state.delivered_contig,
        )

    def _retransmit_msg(
        self,
        *,
        target_id: int,
        missing_requests: list[str],
        missing_sequences: list[int],
    ) -> RetransmitWireMessage:
        return RetransmitWireMessage(
            type="RETRANSMIT",
            source_id=self.node_id,
            target_id=target_id,
            missing_requests=missing_requests,
            missing_sequences=missing_sequences,
            known_request_vector=dict(self.state.known_request_vector),
            known_sequence_contig=self.state.received_sequence_contig,
            delivered_contig=self.state.delivered_contig,
        )

    def _store_request(self, req: PendingRequest) -> None:
        if req.request_id in self.state.request_log:
            return
        self.state.request_log[req.request_id] = req
        self.state.request_by_sender_local[(req.origin_id, req.local_seq)] = req.request_id
        self.state.update_local_request_vector(req.origin_id)
        self.state.retransmit_requested_requests.discard(req.request_id)

    def _store_sequence(self, global_seq: int, request_id: str) -> None:
        existing = self.state.sequence_by_global.get(global_seq)
        if existing is not None and existing != request_id:
            # Ignore conflicting sequence info instead of corrupting local state.
            return
        self.state.sequence_by_global[global_seq] = request_id
        self.state.sequence_by_request[request_id] = global_seq
        self.state.refresh_assignment_progress()
        self.state.retransmit_requested_sequences.discard(global_seq)

    async def submit(self, op: dict) -> dict:
        local_seq = self.state.next_local_seq
        request_id = self.state.request_key(self.node_id, local_seq)
        self.state.next_local_seq += 1

        req = PendingRequest(
            request_id=request_id,
            origin_id=self.node_id,
            local_seq=local_seq,
            op=op,
        )
        self._store_request(req)

        fut = asyncio.get_running_loop().create_future()
        self.results[request_id] = fut

        self._broadcast_to_others(
            self._request_msg(
                source_id=self.node_id,
                origin_id=req.origin_id,
                local_seq=req.local_seq,
                request_id=req.request_id,
                op=req.op,
            ).to_bytes()
        )

        return await fut

    async def _pump(self) -> None:
        while True:
            msg = await self.inbox.get()
            mtype = msg.get("type")
            source_id = int(msg.get("source_id", -1))

            if source_id >= 0:
                self.state.update_peer_vector(
                    source_id,
                    msg.get("known_request_vector", {}),
                    int(msg.get("known_sequence_contig", -1)),
                )

            if mtype == "REQUEST":
                await self._handle_request(msg)
            elif mtype == "SEQUENCE":
                await self._handle_sequence(msg)
            elif mtype == "RETRANSMIT":
                await self._handle_retransmit(msg)

    async def _background(self) -> None:
        while True:
            await asyncio.sleep(0.01)
            work_budget = 256
            while work_budget > 0:
                progressed = False
                if await self._maybe_assign_next():
                    progressed = True
                    work_budget -= 1
                if work_budget <= 0:
                    break
                if await self._maybe_deliver():
                    progressed = True
                    work_budget -= 1
                if not progressed:
                    break
            await self._gossip_and_recover()
            self._broadcast_receipt_advertisement()

    async def _handle_request(self, msg: dict) -> None:
        req = PendingRequest(
            request_id=str(msg["request_id"]),
            origin_id=int(msg["origin_id"]),
            local_seq=int(msg["local_seq"]),
            op=dict(msg["op"]),
        )
        self._store_request(req)
        self._broadcast_receipt_advertisement(force=True)

    async def _handle_sequence(self, msg: dict) -> None:
        global_seq = int(msg["global_seq"])
        request_id = str(msg["request_id"])

        self._store_sequence(global_seq, request_id)
        self._broadcast_receipt_advertisement(force=True)

        if request_id not in self.state.request_log:
            fallback_origin_id = int(request_id.split(":", 1)[0])
            target_id = source_id if source_id >= 0 else fallback_origin_id
            if request_id not in self.state.retransmit_requested_requests:
                self.state.retransmit_requested_requests.add(request_id)
                self._send(
                    target_id,
                    self._retransmit_msg(
                        target_id=target_id,
                        missing_requests=[request_id],
                        missing_sequences=[],
                    ).to_bytes(),
                )

        # If this sequence reveals a gap before it, request those missing sequence messages
        # from the responsible rotating sequencers.
        for missing_seq in range(self.state.received_sequence_contig + 1, global_seq):
            if missing_seq in self.state.sequence_by_global:
                continue
            if missing_seq in self.state.retransmit_requested_sequences:
                continue
            sequencer_id = self.state.current_sequencer_for(missing_seq)
            self.state.retransmit_requested_sequences.add(missing_seq)
            self._send(
                sequencer_id,
                self._retransmit_msg(
                    target_id=sequencer_id,
                    missing_requests=[],
                    missing_sequences=[missing_seq],
                ).to_bytes(),
            )

    async def _handle_retransmit(self, msg: dict) -> None:
        target_id = int(msg.get("target_id", -1))
        if target_id not in {-1, self.node_id}:
            return

        requester = int(msg["source_id"])

        for request_id in msg.get("missing_requests", []):
            req = self.state.request_log.get(str(request_id))
            if req is None:
                continue
            self._send(
                requester,
                self._request_msg(
                    source_id=self.node_id,
                    origin_id=req.origin_id,
                    local_seq=req.local_seq,
                    request_id=req.request_id,
                    op=req.op,
                ).to_bytes(),
            )

        for raw_seq in msg.get("missing_sequences", []):
            global_seq = int(raw_seq)
            if self.state.current_sequencer_for(global_seq) != self.node_id:
                continue
            request_id = self.state.sequence_by_global.get(global_seq)
            if request_id is None:
                continue
            self._send(
                requester,
                self._sequence_msg(
                    source_id=self.node_id,
                    global_seq=global_seq,
                    request_id=request_id,
                ).to_bytes(),
            )

    async def _maybe_assign_next(self) -> bool:
        k = self.state.next_global_seq_to_assign

        if self.state.current_sequencer_for(k) != self.node_id:
            return False

        if any(seq not in self.state.sequence_by_global for seq in range(k)):
            return False

        if not self.state.have_all_prior_assigned_requests_present(k):
            return False

        for request_id in sorted(self.state.request_log):
            req = self.state.request_log[request_id]
            if not self.state.can_assign(req):
                continue

            self.state.mark_assigned(k, req)
            self._broadcast_to_others(
                self._sequence_msg(
                    source_id=self.node_id,
                    global_seq=k,
                    request_id=req.request_id,
                ).to_bytes()
            )
            return True
        return False

    async def _maybe_deliver(self) -> bool:
        s = self.state.next_global_seq_to_deliver
        request_id = self.state.sequence_by_global.get(s)
        if request_id is None:
            return False

        req = self.state.request_log.get(request_id)
        if req is None:
            return False

        if not self.state.have_quorum_receipt(s):
            return False

        result = await self.apply_fn(req.op)

        self.state.delivered_contig = s
        self.state.next_global_seq_to_deliver += 1
        self.state.retransmit_requested_sequences.discard(s)
        self.state.retransmit_requested_requests.discard(request_id)
        self._broadcast_receipt_advertisement(force=True)

        fut = self.results.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)
        return True

    async def _gossip_and_recover(self) -> None:
        # 1) Recover missing request messages that peers have hinted exist.
        for peer_id, peer_vec in self.state.peer_request_vectors.items():
            for origin_str, peer_max_seq in peer_vec.items():
                origin_id = int(origin_str)
                local_contig = int(self.state.known_request_vector.get(origin_str, -1))
                for seq in range(local_contig + 1, int(peer_max_seq) + 1):
                    request_id = self.state.request_key(origin_id, seq)
                    if request_id in self.state.request_log:
                        continue
                    if request_id in self.state.retransmit_requested_requests:
                        continue
                    self.state.retransmit_requested_requests.add(request_id)
                    self._send(
                        peer_id,
                        self._retransmit_msg(
                            target_id=peer_id,
                            missing_requests=[request_id],
                            missing_sequences=[],
                        ).to_bytes(),
                    )

        # 2) Recover missing sequence messages if peers report a longer contiguous prefix.
        max_peer_seq_contig = max(self.state.peer_sequence_contig.values(), default=-1)
        for seq in range(self.state.received_sequence_contig + 1, max_peer_seq_contig + 1):
            if seq in self.state.sequence_by_global:
                continue
            if seq in self.state.retransmit_requested_sequences:
                continue
            sequencer_id = self.state.current_sequencer_for(seq)
            self.state.retransmit_requested_sequences.add(seq)
            self._send(
                sequencer_id,
                self._retransmit_msg(
                    target_id=sequencer_id,
                    missing_requests=[],
                    missing_sequences=[seq],
                ).to_bytes(),
            )

        # 3) Recover requests referenced by known sequence assignments but missing locally.
        for seq in range(self.state.next_global_seq_to_deliver, self.state.received_sequence_contig + 1):
            request_id = self.state.sequence_by_global.get(seq)
            if request_id is None or request_id in self.state.request_log:
                continue
            if request_id in self.state.retransmit_requested_requests:
                continue
            origin_id = int(request_id.split(":", 1)[0])
            self.state.retransmit_requested_requests.add(request_id)
            self._send(
                origin_id,
                self._retransmit_msg(
                    target_id=origin_id,
                    missing_requests=[request_id],
                    missing_sequences=[],
                ).to_bytes(),
            )

        now = time.monotonic()
        if (now - self._last_pending_rebroadcast) < self._pending_rebroadcast_interval_s:
            return

        self._last_pending_rebroadcast = now

        # 4) Repair only the earliest locally-originated request gap peers still report.
        local_key = str(self.node_id)
        local_max_seq = int(self.state.known_request_vector.get(local_key, -1))
        if local_max_seq >= 0 and self.state.peer_request_vectors:
            lowest_peer_local = min(
                int(peer_vec.get(local_key, -1))
                for peer_vec in self.state.peer_request_vectors.values()
            )
            request_end = min(local_max_seq + 1, lowest_peer_local + 1 + self._request_repair_batch)
            for local_seq in range(lowest_peer_local + 1, request_end):
                request_id = self.state.request_by_sender_local.get((self.node_id, local_seq))
                if request_id is None:
                    continue
                req = self.state.request_log.get(request_id)
                if req is None:
                    continue
                payload = self._request_msg(
                    source_id=self.node_id,
                    origin_id=req.origin_id,
                    local_seq=req.local_seq,
                    request_id=req.request_id,
                    op=req.op,
                ).to_bytes()
                for peer_id, peer_vec in self.state.peer_request_vectors.items():
                    if int(peer_vec.get(local_key, -1)) >= local_seq:
                        continue
                    self._send(peer_id, payload)

        # 5) Repair only the earliest missing sequence window for lagging peers.
        if self.state.peer_sequence_contig:
            lowest_peer_seq = min(self.state.peer_sequence_contig.values())
            sequence_end = min(
                self.state.received_sequence_contig + 1,
                lowest_peer_seq + 1 + self._sequence_repair_batch,
            )
            for seq in range(lowest_peer_seq + 1, sequence_end):
                request_id = self.state.sequence_by_global.get(seq)
                if request_id is None:
                    continue
                if self.state.current_sequencer_for(seq) != self.node_id:
                    continue
                payload = self._sequence_msg(
                    source_id=self.node_id,
                    global_seq=seq,
                    request_id=request_id,
                ).to_bytes()
                for peer_id, peer_seq in self.state.peer_sequence_contig.items():
                    if peer_seq >= seq:
                        continue
                    self._send(peer_id, payload)
