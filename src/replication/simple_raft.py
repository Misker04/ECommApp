from __future__ import annotations

import copy
import json
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class NotLeaderError(RuntimeError):
    pass


@dataclass
class LogEntry:
    index: int
    term: int
    payload: dict[str, Any]


@dataclass
class PendingCommit:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[str] = None


class SimpleRaftNode:
    """
    A compact Raft implementation for the PA3 product replicas.
    It keeps to the core protocol pieces we need here:
    leader election, heartbeats, log replication, and commit/apply.
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

        self.current_term = 0
        self.voted_for: Optional[int] = None
        self.role = "follower"
        self.leader_id: Optional[int] = None

        self.log: list[LogEntry] = [LogEntry(index=0, term=0, payload={"noop": True})]
        self.commit_index = 0
        self.last_applied = 0

        self.next_index: dict[int, int] = {}
        self.match_index: dict[int, int] = {}
        self.pending: dict[int, PendingCommit] = {}

        self.last_heard = time.time()
        self.election_deadline = self._next_election_deadline()
        self.last_heartbeat_sent = 0.0

        self.lock = threading.RLock()
        self.stop_event = threading.Event()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.udp_host, self.udp_port))
        self.sock.settimeout(0.2)

        self.receiver_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.ticker_thread = threading.Thread(target=self._ticker_loop, daemon=True)
        self.apply_thread = threading.Thread(target=self._apply_loop, daemon=True)

    def start(self) -> None:
        self.receiver_thread.start()
        self.ticker_thread.start()
        self.apply_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def is_leader(self) -> bool:
        with self.lock:
            return self.role == "leader"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "member_id": self.member_id,
                "role": self.role,
                "leader_id": self.leader_id,
                "term": self.current_term,
                "commit_index": self.commit_index,
                "last_applied": self.last_applied,
                "is_leader": self.role == "leader",
            }

    def submit(self, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        with self.lock:
            if self.role != "leader":
                raise NotLeaderError(f"not leader; current leader is {self.leader_id}")
            entry = LogEntry(index=len(self.log), term=self.current_term, payload=copy.deepcopy(payload))
            self.log.append(entry)
            pending = PendingCommit()
            self.pending[entry.index] = pending
            self.match_index[self.member_id] = entry.index
            self.next_index[self.member_id] = entry.index + 1
            followers = [peer for peer in range(len(self.members)) if peer != self.member_id]

        for peer_id in followers:
            self._send_append_entries(peer_id)

        if not pending.event.wait(timeout):
            raise TimeoutError("timed out waiting for Raft commit")
        if pending.error:
            raise RuntimeError(pending.error)
        return pending.result

    def _next_election_deadline(self) -> float:
        return time.time() + random.uniform(0.8, 1.2)

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

            msg_type = str(message.get("type") or "")
            if msg_type == "request_vote":
                self._handle_request_vote(message)
            elif msg_type == "vote":
                self._handle_vote(message)
            elif msg_type == "append_entries":
                self._handle_append_entries(message)
            elif msg_type == "append_response":
                self._handle_append_response(message)

    def _ticker_loop(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.05)
            with self.lock:
                now = time.time()
                if self.role == "leader":
                    if now - self.last_heartbeat_sent >= 0.25:
                        self.last_heartbeat_sent = now
                        for peer_id in range(len(self.members)):
                            if peer_id != self.member_id:
                                self._send_append_entries(peer_id)
                elif now >= self.election_deadline:
                    self._start_election()

    def _apply_loop(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(0.02)
            to_apply: Optional[LogEntry] = None
            with self.lock:
                if self.last_applied < self.commit_index:
                    self.last_applied += 1
                    to_apply = self.log[self.last_applied]
            if to_apply is None:
                continue

            try:
                result = self.apply_fn(to_apply.payload)
            except Exception as exc:
                error_text = str(exc)
                result = None
            else:
                error_text = None

            with self.lock:
                pending = self.pending.get(to_apply.index)
                if pending is not None:
                    pending.result = result
                    pending.error = error_text
                    pending.event.set()

    def _start_election(self) -> None:
        self.role = "candidate"
        self.current_term += 1
        self.voted_for = self.member_id
        self.leader_id = None
        self.last_heard = time.time()
        self.election_deadline = self._next_election_deadline()
        self._votes = {self.member_id}
        last_entry = self.log[-1]
        message = {
            "type": "request_vote",
            "term": self.current_term,
            "candidate_id": self.member_id,
            "last_log_index": last_entry.index,
            "last_log_term": last_entry.term,
        }
        self._broadcast(message)

    def _become_leader(self) -> None:
        self.role = "leader"
        self.leader_id = self.member_id
        last_index = self.log[-1].index
        self.next_index = {peer_id: last_index + 1 for peer_id in range(len(self.members))}
        self.match_index = {peer_id: 0 for peer_id in range(len(self.members))}
        self.match_index[self.member_id] = last_index
        self.last_heartbeat_sent = 0.0

    def _handle_request_vote(self, message: dict[str, Any]) -> None:
        with self.lock:
            term = int(message["term"])
            candidate_id = int(message["candidate_id"])
            last_log_index = int(message["last_log_index"])
            last_log_term = int(message["last_log_term"])

            if term > self.current_term:
                self.current_term = term
                self.role = "follower"
                self.voted_for = None
                self.leader_id = None

            vote_granted = False
            my_last = self.log[-1]
            up_to_date = (last_log_term, last_log_index) >= (my_last.term, my_last.index)
            if term == self.current_term and up_to_date:
                if self.voted_for in {None, candidate_id}:
                    vote_granted = True
                    self.voted_for = candidate_id
                    self.last_heard = time.time()
                    self.election_deadline = self._next_election_deadline()

            self._send_to(candidate_id, {
                "type": "vote",
                "term": self.current_term,
                "voter_id": self.member_id,
                "granted": vote_granted,
            })

    def _handle_vote(self, message: dict[str, Any]) -> None:
        with self.lock:
            term = int(message["term"])
            voter_id = int(message["voter_id"])
            granted = bool(message["granted"])

            if term > self.current_term:
                self.current_term = term
                self.role = "follower"
                self.voted_for = None
                self.leader_id = None
                self.election_deadline = self._next_election_deadline()
                return
            if self.role != "candidate" or term != self.current_term or not granted:
                return

            self._votes.add(voter_id)
            if len(self._votes) >= self.majority:
                self._become_leader()

    def _handle_append_entries(self, message: dict[str, Any]) -> None:
        with self.lock:
            term = int(message["term"])
            leader_id = int(message["leader_id"])
            prev_index = int(message["prev_log_index"])
            prev_term = int(message["prev_log_term"])
            entries = message.get("entries") or []
            leader_commit = int(message["leader_commit"])

            if term < self.current_term:
                self._send_to(leader_id, {
                    "type": "append_response",
                    "term": self.current_term,
                    "follower_id": self.member_id,
                    "success": False,
                    "match_index": self.log[-1].index,
                })
                return

            if term > self.current_term:
                self.current_term = term
                self.voted_for = None
            self.role = "follower"
            self.leader_id = leader_id
            self.last_heard = time.time()
            self.election_deadline = self._next_election_deadline()

            if prev_index >= len(self.log) or self.log[prev_index].term != prev_term:
                self._send_to(leader_id, {
                    "type": "append_response",
                    "term": self.current_term,
                    "follower_id": self.member_id,
                    "success": False,
                    "match_index": max(0, len(self.log) - 1),
                })
                return

            insert_index = prev_index + 1
            for raw_entry in entries:
                entry = LogEntry(
                    index=int(raw_entry["index"]),
                    term=int(raw_entry["term"]),
                    payload=raw_entry["payload"],
                )
                if insert_index < len(self.log):
                    if self.log[insert_index].term != entry.term:
                        self.log = self.log[:insert_index]
                if insert_index >= len(self.log):
                    self.log.append(entry)
                insert_index += 1

            if leader_commit > self.commit_index:
                self.commit_index = min(leader_commit, self.log[-1].index)

            self._send_to(leader_id, {
                "type": "append_response",
                "term": self.current_term,
                "follower_id": self.member_id,
                "success": True,
                "match_index": self.log[-1].index,
            })

    def _handle_append_response(self, message: dict[str, Any]) -> None:
        with self.lock:
            if self.role != "leader":
                return
            term = int(message["term"])
            follower_id = int(message["follower_id"])
            success = bool(message["success"])
            match_index = int(message["match_index"])

            if term > self.current_term:
                self.current_term = term
                self.role = "follower"
                self.voted_for = None
                self.leader_id = None
                self.election_deadline = self._next_election_deadline()
                return

            if success:
                self.match_index[follower_id] = match_index
                self.next_index[follower_id] = match_index + 1
                self._advance_commit_index()
            else:
                self.next_index[follower_id] = max(1, self.next_index.get(follower_id, len(self.log)) - 1)
                self._send_append_entries(follower_id)

    def _advance_commit_index(self) -> None:
        last_index = self.log[-1].index
        for candidate in range(last_index, self.commit_index, -1):
            if self.log[candidate].term != self.current_term:
                continue
            votes = 1
            for peer_id in range(len(self.members)):
                if peer_id == self.member_id:
                    continue
                if self.match_index.get(peer_id, 0) >= candidate:
                    votes += 1
            if votes >= self.majority:
                self.commit_index = candidate
                return

    def _send_append_entries(self, peer_id: int) -> None:
        next_index = self.next_index.get(peer_id, len(self.log))
        prev_index = max(0, next_index - 1)
        prev_term = self.log[prev_index].term
        entries = [
            {"index": entry.index, "term": entry.term, "payload": entry.payload}
            for entry in self.log[next_index:]
        ]
        self._send_to(peer_id, {
            "type": "append_entries",
            "term": self.current_term,
            "leader_id": self.member_id,
            "prev_log_index": prev_index,
            "prev_log_term": prev_term,
            "entries": entries,
            "leader_commit": self.commit_index,
        })

    def _broadcast(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message).encode("utf-8")
        for host, port in self.members:
            try:
                self.sock.sendto(encoded, (host, port))
            except OSError:
                continue

    def _send_to(self, peer_id: int, message: dict[str, Any]) -> None:
        if peer_id < 0 or peer_id >= len(self.members):
            return
        host, port = self.members[peer_id]
        try:
            self.sock.sendto(json.dumps(message).encode("utf-8"), (host, port))
        except OSError:
            pass
