from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from concurrent import futures
import concurrent.futures as concurrent_futures
from threading import Lock

import grpc

from src.pa3.config import load_pa3_config
from src.pa3.customer_store import CustomerStore, new_session_token
from src.pa3.rotating.replica import RotatingSequencerReplica, UdpPeer
from src.proto import customer_pb2, customer_pb2_grpc


def _now_ms() -> int:
    return int(time.time() * 1000)


class ServiceAbortError(RuntimeError):
    def __init__(self, status_code: grpc.StatusCode, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _abort_for_store_error(context, exc: Exception, default_status: grpc.StatusCode) -> None:
    if isinstance(exc, ServiceAbortError):
        context.abort(exc.status_code, exc.detail)
    if isinstance(exc, concurrent_futures.TimeoutError):
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "customer replication timed out")
    if isinstance(exc, ValueError):
        message = str(exc)
        if "already exists" in message:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, message)
        if "invalid credentials" in message or "unknown" in message or "invalid" in message:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, message)
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, message)
    context.abort(default_status, str(exc))


class CustomerStateMachine:
    def __init__(self, timeout_seconds: int):
        self.store = CustomerStore()
        self.timeout_seconds = timeout_seconds

    async def apply(self, op: dict) -> dict:
        try:
            kind = op["kind"]
            if kind == "create_account":
                user_id = self.store.create_account(op["role"], op["username"], op["password"])
                return {"user_id": user_id}
            if kind == "login":
                token, user_id = self.store.login(
                    op["role"],
                    op["username"],
                    op["password"],
                    op["session_token"],
                    int(op["activity_ms"]),
                )
                return {"session_token": token, "user_id": user_id}
            if kind == "logout":
                self.store.logout(op["session_token"])
                return {"ok": True}
            if kind == "touch_session":
                touched = self.store.touch_session(
                    op["session_token"],
                    int(op["activity_ms"]),
                    self.timeout_seconds,
                )
                if not touched:
                    raise ValueError("invalid session")
                return {"ok": True}
            if kind == "record_purchase":
                self.store.record_purchase(
                    op["session_token"],
                    list(op.get("item_ids", [])),
                    int(op.get("total_units", 0)),
                    int(op["activity_ms"]),
                )
                return {"ok": True}
            if kind == "record_seller_feedback":
                self.store.record_seller_feedback(
                    int(op["seller_id"]),
                    bool(op["positive"]),
                )
                return {"ok": True}
            if kind == "barrier":
                return {"ok": True}
            raise ValueError(f"unknown customer op: {kind}")
        except Exception as exc:
            return {"error": str(exc)}


class _BaseCustomerService(customer_pb2_grpc.CustomerServiceServicer):
    role: str = ""

    def __init__(self, replica: RotatingSequencerReplica, machine: CustomerStateMachine, loop: asyncio.AbstractEventLoop):
        self.replica = replica
        self.machine = machine
        self.loop = loop
        self._touch_lock = Lock()
        # Reads should not turn into a replicated write for every single request.
        # Periodic touch replication is enough to keep sessions fresh across replicas.
        self._touch_replication_interval_ms = 5000
        self._last_replicated_touch_ms: dict[str, int] = {}

    def _submit(self, op: dict) -> dict:
        fut = asyncio.run_coroutine_threadsafe(self.replica.submit(op), self.loop)
        try:
            result = fut.result(timeout=10)
        except concurrent_futures.TimeoutError:
            print(
                "[customer replica timeout] " + json.dumps(self.replica.debug_state(), sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
            raise
        if isinstance(result, dict) and "error" in result:
            raise ValueError(result["error"])
        return result

    def _validate_local(self, session_token: str) -> tuple[bool, int]:
        return self.machine.store.validate_session(
            session_token,
            self.role,
            _now_ms(),
            self.machine.timeout_seconds,
        )

    def _touch_session(self, session_token: str, *, force_replicate: bool = False) -> None:
        now_ms = _now_ms()
        touched = self.machine.store.touch_session(
            session_token,
            now_ms,
            self.machine.timeout_seconds,
        )
        if not touched:
            raise ValueError("invalid session")

        should_replicate = force_replicate
        if not should_replicate:
            with self._touch_lock:
                last_ms = self._last_replicated_touch_ms.get(session_token, 0)
            should_replicate = (now_ms - last_ms) >= self._touch_replication_interval_ms

        if not should_replicate:
            return

        self._submit(
            {
                "kind": "touch_session",
                "session_token": session_token,
                "activity_ms": now_ms,
            }
        )
        with self._touch_lock:
            self._last_replicated_touch_ms[session_token] = now_ms

    def _touch_session_best_effort(self, session_token: str) -> None:
        try:
            self._touch_session(session_token)
        except Exception:
            # Session validation/read paths should stay available as long as the local
            # replica has a fresh enough session entry. Replicated touch updates are
            # best-effort to reduce customer-cluster write amplification under load.
            pass

    def RecordSellerFeedback(self, request, context):
        try:
            self._submit(
                {
                    "kind": "record_seller_feedback",
                    "seller_id": int(request.seller_id),
                    "positive": bool(request.positive),
                }
            )
            return customer_pb2.Empty()
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def Barrier(self, request, context):
        try:
            self._submit(
                {
                    "kind": "barrier",
                    "nonce": str(request.nonce),
                }
            )
            return customer_pb2.Empty()
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)


class BuyerCustomerService(_BaseCustomerService):
    role = "buyer"

    def CreateAccount(self, request, context):
        try:
            result = self._submit(
                {
                    "kind": "create_account",
                    "role": "buyer",
                    "username": request.username,
                    "password": request.password,
                }
            )
            return customer_pb2.CreateAccountResponse(user_id=result["user_id"])
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def Login(self, request, context):
        try:
            result = self._submit(
                {
                    "kind": "login",
                    "role": "buyer",
                    "username": request.username,
                    "password": request.password,
                    "session_token": new_session_token(),
                    "activity_ms": _now_ms(),
                }
            )
            return customer_pb2.LoginResponse(session_token=result["session_token"], user_id=result["user_id"])
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.UNAUTHENTICATED)

    def Logout(self, request, context):
        try:
            self._submit({"kind": "logout", "session_token": request.session_token})
            return customer_pb2.Empty()
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def ValidateSession(self, request, context):
        ok, principal_id = self._validate_local(request.session_token)
        if ok:
            self._touch_session_best_effort(request.session_token)
        return customer_pb2.SessionInfo(valid=ok, principal_id=principal_id, role="buyer" if ok else "")

    def GetSellerRating(self, request, context):
        ok, _ = self._validate_local(request.session_token)
        if not ok:
            raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
        try:
            self._touch_session_best_effort(request.session_token)
            up, down = self.machine.store.get_seller_rating(int(request.seller_id))
            return customer_pb2.RatingResponse(thumbs_up=up, thumbs_down=down)
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def GetBuyerPurchases(self, request, context):
        ok, _ = self._validate_local(request.session_token)
        if not ok:
            raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid buyer session")
        try:
            self._touch_session_best_effort(request.session_token)
            items = self.machine.store.get_buyer_purchases(
                request.session_token,
                _now_ms(),
                self.machine.timeout_seconds,
            )
            return customer_pb2.PurchaseList(item_ids=items)
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def RecordPurchase(self, request, context):
        try:
            self._submit(
                {
                    "kind": "record_purchase",
                    "session_token": request.session_token,
                    "item_ids": list(request.item_ids),
                    "total_units": int(request.total_units),
                    "activity_ms": _now_ms(),
                }
            )
            return customer_pb2.Empty()
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)


class SellerCustomerService(_BaseCustomerService):
    role = "seller"

    def CreateAccount(self, request, context):
        try:
            result = self._submit(
                {
                    "kind": "create_account",
                    "role": "seller",
                    "username": request.username,
                    "password": request.password,
                }
            )
            return customer_pb2.CreateAccountResponse(user_id=result["user_id"])
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def Login(self, request, context):
        try:
            result = self._submit(
                {
                    "kind": "login",
                    "role": "seller",
                    "username": request.username,
                    "password": request.password,
                    "session_token": new_session_token(),
                    "activity_ms": _now_ms(),
                }
            )
            return customer_pb2.LoginResponse(session_token=result["session_token"], user_id=result["user_id"])
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.UNAUTHENTICATED)

    def Logout(self, request, context):
        try:
            with self._touch_lock:
                self._last_replicated_touch_ms.pop(request.session_token, None)
            self._submit({"kind": "logout", "session_token": request.session_token})
            return customer_pb2.Empty()
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def ValidateSession(self, request, context):
        ok, principal_id = self._validate_local(request.session_token)
        if ok:
            self._touch_session_best_effort(request.session_token)
        return customer_pb2.SessionInfo(valid=ok, principal_id=principal_id, role="seller" if ok else "")

    def GetSellerRating(self, request, context):
        ok, _ = self._validate_local(request.session_token)
        if not ok:
            raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
        try:
            self._touch_session_best_effort(request.session_token)
            up, down = self.machine.store.get_seller_rating(int(request.seller_id))
            return customer_pb2.RatingResponse(thumbs_up=up, thumbs_down=down)
        except Exception as exc:
            _abort_for_store_error(context, exc, grpc.StatusCode.INTERNAL)

    def GetBuyerPurchases(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "not applicable for sellers")

    def RecordPurchase(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "not applicable for sellers")


async def _async_main(config_path: str, replica_id: int) -> None:
    cfg = load_pa3_config(config_path)
    loop = asyncio.get_running_loop()

    peers = [UdpPeer(r.id, r.host, r.udp_port) for r in cfg.customer_replicas]
    me = next(r for r in cfg.customer_replicas if r.id == replica_id)

    machine = CustomerStateMachine(cfg.session_timeout_seconds)
    replica = RotatingSequencerReplica(replica_id, peers, machine.apply)
    await replica.start()

    buyer_server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    customer_pb2_grpc.add_CustomerServiceServicer_to_server(
        BuyerCustomerService(replica, machine, loop),
        buyer_server,
    )
    buyer_bound_port = buyer_server.add_insecure_port(f"[::]:{me.buyer_grpc_port}")
    if buyer_bound_port != me.buyer_grpc_port:
        raise RuntimeError(
            f"failed to bind buyer gRPC port {me.buyer_grpc_port} for customer replica {replica_id}"
        )
    buyer_server.start()

    seller_server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    customer_pb2_grpc.add_CustomerServiceServicer_to_server(
        SellerCustomerService(replica, machine, loop),
        seller_server,
    )
    seller_bound_port = seller_server.add_insecure_port(f"[::]:{me.seller_grpc_port}")
    if seller_bound_port != me.seller_grpc_port:
        raise RuntimeError(
            f"failed to bind seller gRPC port {me.seller_grpc_port} for customer replica {replica_id}"
        )
    seller_server.start()

    print(
        f"Customer replica {replica_id} "
        f"buyer gRPC={me.buyer_grpc_port} "
        f"seller gRPC={me.seller_grpc_port} "
        f"udp={me.udp_port}"
    )

    try:
        await asyncio.Event().wait()
    finally:
        buyer_server.stop(0)
        seller_server.stop(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--replica-id", required=True, type=int)
    args = parser.parse_args()
    asyncio.run(_async_main(args.config, args.replica_id))
