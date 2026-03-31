from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class LogEntry:
    term: int
    index: int
    command: dict


@dataclass
class Peer:
    node_id: int
    host: str
    port: int


class RaftNode:
    def __init__(self, node_id: int, peers: list[Peer], apply_callback):
        self.node_id = node_id
        self.peers = [p for p in peers if p.node_id != node_id]
        self.apply_callback = apply_callback
        self.current_term = 0
        self.voted_for: Optional[int] = None
        self.log: List[LogEntry] = []
        self.commit_index = -1
        self.last_applied = -1
        self.role = 'follower'
        self.leader_id: Optional[int] = None
        self.next_index: Dict[int, int] = {}
        self.match_index: Dict[int, int] = {}
        self.server: asyncio.AbstractServer | None = None
        self.last_heartbeat = time.monotonic()
        self.lock = asyncio.Lock()
        self.pending_commits: Dict[int, asyncio.Future] = {}
        self.heartbeat_interval_s = 0.10
        self.election_timeout_min_s = 0.60
        self.election_timeout_max_s = 1.00
        self.rpc_timeout_s = 2.0
        self.election_deadline = time.monotonic()
        self._broadcast_lock = asyncio.Lock()

    def _next_election_deadline(self) -> float:
        return time.monotonic() + random.uniform(
            self.election_timeout_min_s,
            self.election_timeout_max_s,
        )

    async def start(self, host: str, port: int) -> None:
        self.server = await asyncio.start_server(self._handle_conn, host, port)
        async with self.lock:
            self.election_deadline = self._next_election_deadline()
        asyncio.create_task(self._ticker())
        asyncio.create_task(self._apply_loop())

    async def _send_rpc(self, peer: Peer, payload: dict) -> dict:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(peer.host, peer.port),
            timeout=self.rpc_timeout_s,
        )
        try:
            writer.write((json.dumps(payload) + '\n').encode('utf-8'))
            await asyncio.wait_for(writer.drain(), timeout=self.rpc_timeout_s)
            raw = await asyncio.wait_for(reader.readline(), timeout=self.rpc_timeout_s)
            if not raw:
                raise ConnectionError('empty raft response')
            return json.loads(raw.decode('utf-8'))
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=self.rpc_timeout_s)
            except Exception:
                pass

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readline()
        if not raw:
            writer.close()
            await writer.wait_closed()
            return
        msg = json.loads(raw.decode('utf-8'))
        typ = msg.get('type')
        if typ == 'request_vote':
            resp = await self._handle_request_vote(msg)
        elif typ == 'append_entries':
            resp = await self._handle_append_entries(msg)
        else:
            resp = {'ok': False, 'error': 'unknown raft rpc'}
        writer.write((json.dumps(resp) + '\n').encode('utf-8'))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_s)
            async with self.lock:
                role = self.role
                election_deadline = self.election_deadline
            if role == 'leader':
                await self._broadcast_heartbeats()
                continue
            if time.monotonic() >= election_deadline:
                await self._start_election()

    async def _start_election(self) -> None:
        async with self.lock:
            self.role = 'candidate'
            self.current_term += 1
            term = self.current_term
            self.voted_for = self.node_id
            self.last_heartbeat = time.monotonic()
            self.election_deadline = self._next_election_deadline()
            last_log_index = self.log[-1].index if self.log else -1
            last_log_term = self.log[-1].term if self.log else -1
        votes = 1
        quorum = (len(self.peers) + 1) // 2 + 1
        for peer in self.peers:
            try:
                resp = await self._send_rpc(peer, {
                    'type': 'request_vote',
                    'term': term,
                    'candidate_id': self.node_id,
                    'last_log_index': last_log_index,
                    'last_log_term': last_log_term,
                })
                if resp.get('term', term) > term:
                    async with self.lock:
                        self.role = 'follower'
                        self.current_term = resp['term']
                        self.voted_for = None
                        self.leader_id = None
                        self.election_deadline = self._next_election_deadline()
                    return
                if resp.get('vote_granted'):
                    votes += 1
            except Exception:
                continue
        if votes >= quorum:
            async with self.lock:
                self.role = 'leader'
                self.leader_id = self.node_id
                next_idx = len(self.log)
                self.next_index = {p.node_id: next_idx for p in self.peers}
                self.match_index = {p.node_id: -1 for p in self.peers}

    async def _handle_request_vote(self, msg: dict) -> dict:
        async with self.lock:
            term = int(msg['term'])
            if term < self.current_term:
                return {'term': self.current_term, 'vote_granted': False}
            if term > self.current_term:
                self.current_term = term
                self.role = 'follower'
                self.voted_for = None
                self.leader_id = None
                self.election_deadline = self._next_election_deadline()
            last_log_index = self.log[-1].index if self.log else -1
            last_log_term = self.log[-1].term if self.log else -1
            candidate_up_to_date = (msg['last_log_term'], msg['last_log_index']) >= (last_log_term, last_log_index)
            can_vote = self.voted_for in (None, int(msg['candidate_id']))
            granted = can_vote and candidate_up_to_date
            if granted:
                self.voted_for = int(msg['candidate_id'])
                self.last_heartbeat = time.monotonic()
                self.election_deadline = self._next_election_deadline()
            return {'term': self.current_term, 'vote_granted': granted}

    async def _handle_append_entries(self, msg: dict) -> dict:
        async with self.lock:
            term = int(msg['term'])
            if term < self.current_term:
                return {'term': self.current_term, 'success': False}
            self.current_term = term
            self.role = 'follower'
            self.leader_id = int(msg['leader_id'])
            self.last_heartbeat = time.monotonic()
            self.election_deadline = self._next_election_deadline()
            prev_index = int(msg['prev_log_index'])
            prev_term = int(msg['prev_log_term'])
            if prev_index >= 0:
                if prev_index >= len(self.log) or self.log[prev_index].term != prev_term:
                    return {'term': self.current_term, 'success': False}
            entries = [LogEntry(term=e['term'], index=e['index'], command=e['command']) for e in msg.get('entries', [])]
            cursor = prev_index + 1
            for entry in entries:
                if cursor < len(self.log):
                    if self.log[cursor].term != entry.term:
                        self.log = self.log[:cursor]
                if cursor >= len(self.log):
                    self.log.append(entry)
                cursor += 1
            leader_commit = int(msg.get('leader_commit', -1))
            if leader_commit > self.commit_index:
                self.commit_index = min(leader_commit, len(self.log) - 1)
            return {'term': self.current_term, 'success': True, 'match_index': len(self.log) - 1}

    async def _broadcast_heartbeats(self) -> None:
        async with self._broadcast_lock:
            tasks = [self._replicate_to_peer(peer) for peer in self.peers]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _replicate_to_peer(self, peer: Peer) -> None:
        async with self.lock:
            next_idx = self.next_index.get(peer.node_id, len(self.log))
            prev_index = next_idx - 1
            prev_term = self.log[prev_index].term if prev_index >= 0 else -1
            entries = [e.__dict__ for e in self.log[next_idx:]]
            payload = {
                'type': 'append_entries',
                'term': self.current_term,
                'leader_id': self.node_id,
                'prev_log_index': prev_index,
                'prev_log_term': prev_term,
                'entries': entries,
                'leader_commit': self.commit_index,
            }
        try:
            resp = await self._send_rpc(peer, payload)
        except Exception:
            return
        async with self.lock:
            if resp.get('term', self.current_term) > self.current_term:
                self.current_term = resp['term']
                self.role = 'follower'
                self.voted_for = None
                self.leader_id = None
                self.election_deadline = self._next_election_deadline()
                return
            if resp.get('success'):
                match_idx = int(resp.get('match_index', prev_index))
                self.match_index[peer.node_id] = match_idx
                self.next_index[peer.node_id] = match_idx + 1
                await self._advance_commit_index_locked()
            else:
                self.next_index[peer.node_id] = max(0, self.next_index.get(peer.node_id, len(self.log)) - 1)

    async def _advance_commit_index_locked(self) -> None:
        quorum = (len(self.peers) + 1) // 2 + 1
        for idx in range(len(self.log) - 1, self.commit_index, -1):
            if self.log[idx].term != self.current_term:
                continue
            replicas = 1
            for peer_id, match_idx in self.match_index.items():
                if match_idx >= idx:
                    replicas += 1
            if replicas >= quorum:
                self.commit_index = idx
                for commit_idx, fut in list(self.pending_commits.items()):
                    if commit_idx <= self.commit_index and not fut.done():
                        fut.set_result(True)
                return

    async def _apply_loop(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            while self.last_applied < self.commit_index:
                self.last_applied += 1
                entry = self.log[self.last_applied]
                await self.apply_callback(entry.command)

    async def submit(self, command: dict) -> None:
        async with self.lock:
            if self.role != 'leader':
                raise RuntimeError(f'NOT_LEADER:{self.leader_id if self.leader_id is not None else -1}')
            entry = LogEntry(term=self.current_term, index=len(self.log), command=command)
            self.log.append(entry)
            fut = asyncio.get_running_loop().create_future()
            self.pending_commits[entry.index] = fut
        await self._broadcast_heartbeats()
        await asyncio.wait_for(fut, timeout=15.0)

    def leader_hint(self) -> Optional[int]:
        return self.leader_id
