from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as concurrent_futures
import time
import uuid
from concurrent import futures

import grpc

from src.pa3.config import load_pa3_config
from src.pa3.frontend.grpc_pool import BackendUnavailableError, GrpcReplicaPool, GrpcTarget
from src.pa3.product_store import ProductStore
from src.pa3.raft.node import Peer, RaftNode
from src.proto import customer_pb2, customer_pb2_grpc
from src.proto import product_pb2, product_pb2_grpc


def _now_ms() -> int:
    return int(time.time() * 1000)


class ServiceAbortError(RuntimeError):
    def __init__(self, status_code: grpc.StatusCode, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _item_to_proto(item) -> product_pb2.ItemResponse:
    return product_pb2.ItemResponse(
        item_id=item.item_id,
        name=item.name,
        price=item.price,
        quantity=item.quantity,
    )


class ProductStateMachine:
    def __init__(self):
        self.store = ProductStore()
        self.completed: dict[str, dict] = {}

    async def apply(self, command: dict) -> dict:
        command_id = command["command_id"]
        try:
            kind = command["kind"]

            if kind == "register_item":
                item_id = self.store.register_item(
                    command["seller_id"],
                    command["name"],
                    command["category"],
                    command["price"],
                    command["quantity"],
                )
                result = {"item_id": item_id}

            elif kind == "change_price":
                self.store.change_price(
                    command["seller_id"],
                    command["item_id"],
                    command["new_price"],
                )
                result = {"ok": True}

            elif kind == "update_quantity":
                self.store.update_quantity(
                    command["seller_id"],
                    command["item_id"],
                    command["quantity"],
                )
                result = {"ok": True}

            elif kind == "add_to_cart":
                self.store.add_to_cart(
                    command["buyer_id"],
                    command["item_id"],
                    command["quantity"],
                )
                result = {"ok": True}

            elif kind == "remove_from_cart":
                self.store.remove_from_cart(
                    command["buyer_id"],
                    command["item_id"],
                    command["quantity"],
                )
                result = {"ok": True}

            elif kind == "clear_cart":
                self.store.clear_cart(command["buyer_id"])
                result = {"ok": True}

            elif kind == "feedback":
                self.store.add_feedback(
                    command["item_id"],
                    str(command["feedback"]).lower() == "up",
                )
                result = {"ok": True}

            elif kind == "finalize_purchase":
                item_ids, total_units = self.store.finalize_purchase(command["buyer_id"])
                result = {"item_ids": item_ids, "total_units": total_units}

            elif kind == "barrier":
                result = {"ok": True}

            else:
                raise ValueError(f"unknown product command: {kind}")

        except Exception as exc:
            result = {"error": str(exc)}

        self.completed[command_id] = result
        return result

    def take_completed(self, command_id: str) -> dict | None:
        return self.completed.pop(command_id, None)


class ProductService(product_pb2_grpc.ProductServiceServicer):
    def __init__(
        self,
        node: RaftNode,
        machine: ProductStateMachine,
        loop: asyncio.AbstractEventLoop,
        buyer_customer_pool: GrpcReplicaPool,
        seller_customer_pool: GrpcReplicaPool,
    ):
        self.node = node
        self.machine = machine
        self.loop = loop
        self.buyer_customer_pool = buyer_customer_pool
        self.seller_customer_pool = seller_customer_pool

    def _customer_pool_for(self, role: str) -> GrpcReplicaPool:
        return self.buyer_customer_pool if role == "buyer" else self.seller_customer_pool

    def _validate(self, token: str, role: str) -> tuple[bool, int]:
        pool = self._customer_pool_for(role)
        try:
            info = pool.call(
                lambda stub, timeout: stub.ValidateSession(
                    customer_pb2.SessionRequest(session_token=token),
                    timeout=timeout,
                )
            )
            return bool(info.valid), int(info.principal_id)
        except BackendUnavailableError as exc:
            raise RuntimeError(f"CUSTOMER_BACKEND_UNAVAILABLE:{exc}") from exc
        except grpc.RpcError as exc:
            if exc.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
                raise RuntimeError(f"CUSTOMER_BACKEND_UNAVAILABLE:{exc.details() or exc.code().name}") from exc
            raise

    def _record_purchase(self, session_token: str, item_ids: list[str], total_units: int) -> None:
        self.buyer_customer_pool.call(
            lambda stub, timeout: stub.RecordPurchase(
                customer_pb2.RecordPurchaseRequest(
                    session_token=session_token,
                    item_ids=item_ids,
                    total_units=total_units,
                ),
                timeout=timeout,
            )
        )

    def _record_seller_feedback(self, seller_id: int, positive: bool) -> None:
        self.seller_customer_pool.call(
            lambda stub, timeout: stub.RecordSellerFeedback(
                customer_pb2.RecordSellerFeedbackRequest(
                    seller_id=int(seller_id),
                    positive=bool(positive),
                ),
                timeout=timeout,
            )
        )

    def _submit_to_raft(self, command: dict) -> None:
        fut = asyncio.run_coroutine_threadsafe(self.node.submit(command), self.loop)
        fut.result(timeout=8)

    async def _await_result(self, command_id: str) -> dict:
        for _ in range(400):
            result = self.machine.take_completed(command_id)
            if result is not None:
                return result
            await asyncio.sleep(0.01)
        raise TimeoutError("timed out waiting for committed result")

    def _await_result_sync(self, command_id: str) -> dict:
        fut = asyncio.run_coroutine_threadsafe(self._await_result(command_id), self.loop)
        return fut.result(timeout=8)

    def _require_read_leader(self) -> None:
        leader_id = self.node.leader_hint()
        if leader_id is None:
            raise ServiceAbortError(grpc.StatusCode.UNAVAILABLE, "leader election in progress")
        if leader_id != self.node.node_id:
            raise ServiceAbortError(grpc.StatusCode.UNAVAILABLE, f"NOT_LEADER:{leader_id}")

    @staticmethod
    def _abort_for_exception(context, exc: Exception) -> None:
        if isinstance(exc, ServiceAbortError):
            context.abort(exc.status_code, exc.detail)
        if isinstance(exc, concurrent_futures.TimeoutError) or isinstance(exc, TimeoutError):
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "product replication timed out")
        if isinstance(exc, ValueError):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if isinstance(exc, RuntimeError):
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        if isinstance(exc, grpc.RpcError):
            context.abort(exc.code(), exc.details() or exc.code().name)
        context.abort(grpc.StatusCode.INTERNAL, str(exc))

    def _run_write_and_get_result(self, command: dict) -> dict:
        self._submit_to_raft(command)
        return self._await_result_sync(command["command_id"])

    def RegisterItem(self, request, context):
        try:
            ok, seller_id = self._validate(request.session_token, "seller")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            command = {
                "command_id": uuid.uuid4().hex,
                "kind": "register_item",
                "seller_id": seller_id,
                "name": request.name,
                "category": int(request.category),
                "price": float(request.price),
                "quantity": int(request.quantity),
            }
            result = self._run_write_and_get_result(command)
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.RegisterItemResponse(item_id=result["item_id"])
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def ChangePrice(self, request, context):
        try:
            ok, seller_id = self._validate(request.session_token, "seller")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "change_price",
                    "seller_id": seller_id,
                    "item_id": request.item_id,
                    "new_price": float(request.new_price),
                }
            )
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def UpdateQuantity(self, request, context):
        try:
            ok, seller_id = self._validate(request.session_token, "seller")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "update_quantity",
                    "seller_id": seller_id,
                    "item_id": request.item_id,
                    "quantity": int(request.quantity),
                }
            )
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def DisplayItemsForSale(self, request, context):
        try:
            ok, seller_id = self._validate(request.session_token, "seller")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
            self._require_read_leader()

            items = self.machine.store.display_items_for_seller(seller_id)
            return product_pb2.ItemList(items=[_item_to_proto(item) for item in items])
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def SearchItems(self, request, context):
        try:
            ok, _buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
            self._require_read_leader()

            items = self.machine.store.search_items(int(request.category))
            return product_pb2.ItemList(items=[_item_to_proto(item) for item in items])
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def GetItem(self, request, context):
        try:
            ok, _buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
            self._require_read_leader()

            item = self.machine.store.get_item(request.item_id)
            return _item_to_proto(item)
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def AddToCart(self, request, context):
        try:
            ok, buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "add_to_cart",
                    "buyer_id": buyer_id,
                    "item_id": request.item_id,
                    "quantity": int(request.quantity),
                }
            )
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def RemoveFromCart(self, request, context):
        try:
            ok, buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "remove_from_cart",
                    "buyer_id": buyer_id,
                    "item_id": request.item_id,
                    "quantity": int(request.quantity),
                }
            )
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def SaveCart(self, request, context):
        try:
            ok, _buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def ClearCart(self, request, context):
        try:
            ok, buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "clear_cart",
                    "buyer_id": buyer_id,
                }
            )
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def GetCart(self, request, context):
        try:
            ok, buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
            self._require_read_leader()

            cart = self.machine.store.get_cart(buyer_id)
            return product_pb2.CartResponse(
                items=[
                    product_pb2.CartItem(item_id=item_id, quantity=qty)
                    for item_id, qty in cart.items()
                ]
            )
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def ProvideFeedback(self, request, context):
        try:
            ok, _buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "feedback",
                    "item_id": request.item_id,
                    "feedback": request.feedback,
                }
            )
            if "error" in result:
                raise ValueError(result["error"])

            try:
                item = self.machine.store.get_item(request.item_id)
                self._record_seller_feedback(
                    seller_id=int(item.seller_id),
                    positive=str(request.feedback).strip().lower() == "up",
                )
            except Exception:
                # Feedback is already committed in the product DB; avoid reporting a false failure.
                pass

            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def FinalizePurchase(self, request, context):
        try:
            ok, buyer_id = self._validate(request.session_token, "buyer")
            if not ok:
                raise ServiceAbortError(grpc.StatusCode.UNAUTHENTICATED, "invalid session")

            command_id = uuid.uuid4().hex
            result = self._run_write_and_get_result(
                {
                    "command_id": command_id,
                    "kind": "finalize_purchase",
                    "buyer_id": buyer_id,
                }
            )
            if "error" in result:
                return product_pb2.PurchaseResult(success=False, message=result["error"])

            item_ids = list(result["item_ids"])
            total_units = int(result["total_units"])

            try:
                self._record_purchase(request.session_token, item_ids, total_units)
            except Exception:
                # Purchase already committed in the product DB; do not claim it failed.
                pass

            return product_pb2.PurchaseResult(success=True, message="Purchase completed successfully")
        except Exception as exc:
            self._abort_for_exception(context, exc)

    def Barrier(self, request, context):
        try:
            result = self._run_write_and_get_result(
                {
                    "command_id": uuid.uuid4().hex,
                    "kind": "barrier",
                    "nonce": str(request.nonce),
                }
            )
            if "error" in result:
                raise ValueError(result["error"])
            return product_pb2.Empty()
        except Exception as exc:
            self._abort_for_exception(context, exc)


async def _async_main(config_path: str, replica_id: int) -> None:
    cfg = load_pa3_config(config_path)
    loop = asyncio.get_running_loop()

    me = next(r for r in cfg.product_replicas if r.id == replica_id)
    peers = [Peer(r.id, r.host, r.raft_port) for r in cfg.product_replicas]

    machine = ProductStateMachine()
    node = RaftNode(replica_id, peers, machine.apply)
    await node.start(me.host, me.raft_port)

    buyer_customer_pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.buyer_grpc_port) for r in cfg.customer_replicas],
        customer_pb2_grpc.CustomerServiceStub,
    )
    seller_customer_pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.seller_grpc_port) for r in cfg.customer_replicas],
        customer_pb2_grpc.CustomerServiceStub,
    )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    product_pb2_grpc.add_ProductServiceServicer_to_server(
        ProductService(node, machine, loop, buyer_customer_pool, seller_customer_pool),
        server,
    )
    bound_port = server.add_insecure_port(f"[::]:{me.grpc_port}")
    if bound_port != me.grpc_port:
        raise RuntimeError(f"failed to bind product gRPC port {me.grpc_port} for replica {replica_id}")
    server.start()

    print(f"Product replica {replica_id} gRPC={me.grpc_port} raft={me.raft_port}")

    try:
        await asyncio.Event().wait()
    finally:
        server.stop(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--replica-id", required=True, type=int)
    args = parser.parse_args()
    asyncio.run(_async_main(args.config, args.replica_id))
