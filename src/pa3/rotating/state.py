from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Set


@dataclass
class PendingRequest:
    request_id: str
    origin_id: int
    local_seq: int
    op: dict


@dataclass
class RotationState:
    node_id: int
    cluster_size: int

    next_local_seq: int = 0
    next_global_seq_to_assign: int = 0
    next_global_seq_to_deliver: int = 0

    request_log: Dict[str, PendingRequest] = field(default_factory=dict)
    request_by_sender_local: Dict[tuple[int, int], str] = field(default_factory=dict)

    sequence_by_global: Dict[int, str] = field(default_factory=dict)
    sequence_by_request: Dict[str, int] = field(default_factory=dict)

    known_request_vector: Dict[str, int] = field(default_factory=dict)
    peer_request_vectors: Dict[int, Dict[str, int]] = field(default_factory=dict)

    # Highest contiguous global sequence number for which this replica has received
    # the corresponding SEQUENCE message.
    received_sequence_contig: int = -1

    # Highest contiguous global sequence number this replica has delivered.
    delivered_contig: int = -1

    peer_sequence_contig: Dict[int, int] = field(default_factory=dict)

    # For assignment condition (3): per-origin contiguous local requests already assigned.
    assigned_local_contig: Dict[int, int] = field(default_factory=dict)

    retransmit_requested_requests: Set[str] = field(default_factory=set)
    retransmit_requested_sequences: Set[int] = field(default_factory=set)

    def request_key(self, origin_id: int, local_seq: int) -> str:
        return f"{origin_id}:{local_seq}"

    def update_local_request_vector(self, origin_id: int) -> None:
        key = str(origin_id)
        current = int(self.known_request_vector.get(key, -1))
        while (origin_id, current + 1) in self.request_by_sender_local:
            current += 1
        self.known_request_vector[key] = current

    def update_peer_vector(self, peer_id: int, vector: Dict[str, int], sequence_contig: int) -> None:
        self.peer_request_vectors[peer_id] = {str(k): int(v) for k, v in dict(vector).items()}
        self.peer_sequence_contig[peer_id] = int(sequence_contig)

    def current_sequencer_for(self, global_seq: int) -> int:
        return global_seq % self.cluster_size

    def can_assign(self, req: PendingRequest) -> bool:
        contig = self.assigned_local_contig.get(req.origin_id, -1)
        return req.local_seq == contig + 1 and req.request_id not in self.sequence_by_request

    def mark_assigned(self, global_seq: int, req: PendingRequest) -> None:
        self.sequence_by_global[global_seq] = req.request_id
        self.sequence_by_request[req.request_id] = global_seq
        self.refresh_assignment_progress()

    def update_received_sequence_contig(self) -> None:
        while (self.received_sequence_contig + 1) in self.sequence_by_global:
            self.received_sequence_contig += 1

    def refresh_assignment_progress(self) -> None:
        self.update_received_sequence_contig()

        assigned_local_contig: Dict[int, int] = {}
        next_global_seq_to_assign = 0

        while next_global_seq_to_assign in self.sequence_by_global:
            request_id = self.sequence_by_global[next_global_seq_to_assign]
            origin_str, local_str = request_id.split(":", 1)
            origin_id = int(origin_str)
            local_seq = int(local_str)
            current = assigned_local_contig.get(origin_id, -1)
            if local_seq == current + 1:
                assigned_local_contig[origin_id] = local_seq
            next_global_seq_to_assign += 1

        self.assigned_local_contig = assigned_local_contig
        self.next_global_seq_to_assign = next_global_seq_to_assign

    def have_all_prior_assigned_requests_present(self, global_seq: int) -> bool:
        for seq in range(global_seq):
            request_id = self.sequence_by_global.get(seq)
            if request_id is None or request_id not in self.request_log:
                return False
        return True

    def have_quorum_receipt(self, global_seq: int) -> bool:
        # Local replica must itself have all sequence+request info through global_seq.
        for seq in range(global_seq + 1):
            request_id = self.sequence_by_global.get(seq)
            if request_id is None or request_id not in self.request_log:
                return False

        quorum = self.cluster_size // 2 + 1
        count = 1  # self

        for peer_id, seq_contig in self.peer_sequence_contig.items():
            if seq_contig < global_seq:
                continue

            peer_vec = self.peer_request_vectors.get(peer_id, {})
            ok = True

            for seq in range(global_seq + 1):
                request_id = self.sequence_by_global.get(seq)
                if request_id is None:
                    ok = False
                    break

                origin_str, local_str = request_id.split(":", 1)
                origin_id = int(origin_str)
                local_seq = int(local_str)

                if int(peer_vec.get(str(origin_id), -1)) < local_seq:
                    ok = False
                    break

            if ok:
                count += 1

        return count >= quorum
