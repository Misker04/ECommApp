from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Generic, List, TypeVar

import grpc

T = TypeVar("T")
S = TypeVar("S")


class BackendUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class GrpcTarget:
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class GrpcReplicaPool(Generic[S]):
    _next_start_index = 0
    _start_index_lock = Lock()

    def __init__(
        self,
        targets: List[GrpcTarget],
        stub_factory: Callable[[grpc.Channel], S],
        *,
        connect_timeout_s: float = 1.5,
        call_timeout_s: float = 12.0,
    ):
        if not targets:
            raise ValueError("at least one gRPC target is required")
        self.targets = list(targets)
        self.stub_factory = stub_factory
        self.connect_timeout_s = connect_timeout_s
        self.call_timeout_s = call_timeout_s
        with self._start_index_lock:
            self.current_index = self._next_start_index % len(self.targets)
            GrpcReplicaPool._next_start_index += 1
        self._lock = Lock()

    def _ordered_indices(self) -> list[int]:
        with self._lock:
            start = self.current_index
        return [(start + offset) % len(self.targets) for offset in range(len(self.targets))]

    def call(self, invoker: Callable[[S, float], T]) -> T:
        last_error: Exception | None = None

        for idx in self._ordered_indices():
            target = self.targets[idx]

            try:
                with grpc.insecure_channel(target.address) as channel:
                    grpc.channel_ready_future(channel).result(timeout=self.connect_timeout_s)
                    stub = self.stub_factory(channel)
                    response = invoker(stub, self.call_timeout_s)

                with self._lock:
                    self.current_index = idx
                return response

            except grpc.FutureTimeoutError:
                last_error = BackendUnavailableError(
                    f"gRPC channel to {target.address} was not ready within {self.connect_timeout_s:.1f}s"
                )
                continue

            except grpc.RpcError as exc:
                last_error = exc
                if exc.code() not in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
                    raise
                continue

            except OSError as exc:
                last_error = BackendUnavailableError(f"failed to reach {target.address}: {exc}")
                continue

        if last_error is None:
            raise BackendUnavailableError("no gRPC targets configured")
        raise last_error
