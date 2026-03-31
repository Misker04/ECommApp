"""
product_grpc_server.py
----------------------
Runs on the Product DB VM (port 50052).
Does NOT store sessions locally.
Calls Customer DB (ValidateSession) over gRPC to verify tokens.
"""

import asyncio
import grpc
import json
import os
import time
import uuid
import threading
from concurrent import futures

from src.proto import product_pb2, product_pb2_grpc
from src.proto import customer_pb2, customer_pb2_grpc
from src.server.state import MarketState
from src.server.handlers import buyer, seller
from src.common.models import Seller, Buyer, ItemId
from src.replication.simple_raft import NotLeaderError, SimpleRaftNode

import argparse
from src.common.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--replica-id", type=int, default=0)
args = parser.parse_args()

cfg = load_config(args.config)

_SHARED_STATE = MarketState()

_buyer_customer_stubs = [
    customer_pb2_grpc.CustomerServiceStub(grpc.insecure_channel(f"{replica.host}:{replica.port}"))
    for replica in cfg.backend_customer_db.buyer_targets()
]
_seller_customer_stubs = [
    customer_pb2_grpc.CustomerServiceStub(grpc.insecure_channel(f"{replica.host}:{replica.port}"))
    for replica in cfg.backend_customer_db.seller_targets()
]
_RAFT_NODE: SimpleRaftNode | None = None


def _debug(message: str) -> None:
    print(f"[product-replica {args.replica_id}] {message}", flush=True)


def validate_session(session_token: str, expected_role: str) -> tuple[bool, int]:
    stubs = _buyer_customer_stubs if expected_role == "buyer" else _seller_customer_stubs
    for _attempt in range(3):
        saw_invalid = False
        for idx, stub in enumerate(stubs):
            try:
                info = stub.ValidateSession(
                    customer_pb2.SessionRequest(session_token=session_token)
                )
                if info.valid and info.role == expected_role:
                    _debug(
                        f"validate_session ok role={expected_role} "
                        f"via customer replica index={idx} principal={int(info.principal_id)}"
                    )
                    return True, int(info.principal_id)
                saw_invalid = True
                _debug(
                    f"validate_session miss role={expected_role} via customer replica index={idx} "
                    f"valid={info.valid} role={info.role!r}"
                )
            except grpc.RpcError:
                _debug(f"validate_session rpc error role={expected_role} via customer replica index={idx}")
                continue
        if not saw_invalid:
            time.sleep(0.1)
    _debug(f"validate_session failed role={expected_role}")
    return False, 0


def _item_to_proto(it: dict) -> product_pb2.ItemResponse:
    """
    Converts a to_public_dict() item to proto.
    to_public_dict() keys: item_id, item_name, item_category,
                           sale_price, item_quantity, seller_id, ...
    """
    iid = it["item_id"]  # {"category": x, "number": y}
    return product_pb2.ItemResponse(
        item_id=f"{iid['category']}:{iid['number']}",
        name=it["item_name"],
        price=float(it["sale_price"]),
        quantity=int(it["item_quantity"]),
    )


def _cart_item_to_proto(ci: dict) -> product_pb2.CartItem:
    """
    Converts a cart entry to proto.
    buyer.py DisplayCart returns: {"item_id": ItemId.to_dict(), "qty": int}
    """
    iid = ci["item_id"]  # {"category": x, "number": y}
    return product_pb2.CartItem(
        item_id=f"{iid['category']}:{iid['number']}",
        quantity=int(ci["qty"]),
    )


def _apply_product_mutation(payload: dict[str, object]) -> dict:
    role = str(payload.get("role") or "")
    action = str(payload.get("action") or "")
    data = dict(payload.get("data") or {})

    if role == "seller":
        return asyncio.run(seller.handle(_SHARED_STATE, {
            "req_id": f"raft_{action}",
            "action": action,
            "data": data,
        }))

    if role == "buyer":
        return asyncio.run(buyer.handle(_SHARED_STATE, {
            "req_id": f"raft_{action}",
            "action": action,
            "data": data,
        }))

    raise ValueError(f"unsupported product mutation: role={role} action={action}")


def _write_runtime_status(replica_id: int) -> None:
    runtime_dir = cfg.storage.data_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_file = runtime_dir / f"product-db-{replica_id}.json"
    while True:
        snapshot = _RAFT_NODE.snapshot() if _RAFT_NODE is not None else {"is_leader": False, "role": "standalone"}
        payload = {
            "pid": os.getpid(),
            "replica_id": replica_id,
            **snapshot,
        }
        runtime_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        time.sleep(0.5)


class ProductService(product_pb2_grpc.ProductServiceServicer):

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def _require_leader(self, context):
        if _RAFT_NODE is not None and not _RAFT_NODE.is_leader():
            snapshot = _RAFT_NODE.snapshot()
            _debug(
                f"rejecting request because not leader: role={snapshot.get('role')} "
                f"leader_id={snapshot.get('leader_id')} term={snapshot.get('term')}"
            )
            context.abort(grpc.StatusCode.UNAVAILABLE, "product replica is not the leader")

    def _replicate(self, context, payload: dict) -> dict:
        if _RAFT_NODE is None:
            return _apply_product_mutation(payload)
        try:
            _debug(f"replicating action={payload.get('action')} role={payload.get('role')}")
            return _RAFT_NODE.submit(payload)
        except NotLeaderError as exc:
            _debug(f"replicate failed not leader for action={payload.get('action')}: {exc}")
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except TimeoutError as exc:
            _debug(f"replicate timeout for action={payload.get('action')}: {exc}")
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, str(exc))
        except Exception as exc:
            _debug(f"replicate internal error for action={payload.get('action')}: {exc}")
            context.abort(grpc.StatusCode.INTERNAL, str(exc))

    def _ensure_local_session(self, token: str, principal_id: int, role: str):
        import time
        self.state.db._sessions[token] = {
            "principal_id": principal_id,
            "role": role,
            "created_at": time.time()
        }

    def _ensure_seller(self, seller_id: int):
        async def _create():
            existing = await self.state.db.get_seller(seller_id)
            if not existing:
                async with self.state.db.lock:
                    if seller_id not in self.state.db.sellers_by_id:
                        s = Seller(seller_id=seller_id, name=f"seller_{seller_id}", password_hash="")
                        self.state.db.sellers_by_id[seller_id] = s
        asyncio.run(_create())

    def _ensure_buyer(self, buyer_id: int):
        async def _create():
            existing = await self.state.db.get_buyer(buyer_id)
            if not existing:
                async with self.state.db.lock:
                    if buyer_id not in self.state.db.buyers_by_id:
                        b = Buyer(buyer_id=buyer_id, name=f"buyer_{buyer_id}", password_hash="")
                        self.state.db.buyers_by_id[buyer_id] = b
        asyncio.run(_create())

    # ==================================================
    # SELLER APIs
    # ==================================================

    def RegisterItem(self, request, context):
        self._require_leader(context)
        valid, seller_id = validate_session(request.session_token, "seller")
        if not valid:
            _debug("RegisterItem rejected: invalid or expired seller session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, seller_id, "seller")
        self._ensure_seller(seller_id)

        resp = self._replicate(context, {
            "role": "seller",
            "action": "RegisterItemForSale",
            "data": {
                "item_name": request.name,
                "item_category": request.category,
                "sale_price": request.price,
                "quantity": request.quantity,
                "keywords": [],
                "condition": "new",
                "session_token": request.session_token,
            },
        })
        if not resp.get("ok"):
            _debug(f"RegisterItem handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        item_id = resp["data"]["item_id"]  # {"category": x, "number": y}
        return product_pb2.RegisterItemResponse(
            item_id=f"{item_id['category']}:{item_id['number']}"
        )

    def ChangePrice(self, request, context):
        self._require_leader(context)
        valid, seller_id = validate_session(request.session_token, "seller")
        if not valid:
            _debug("ChangePrice rejected: invalid or expired seller session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, seller_id, "seller")
        self._ensure_seller(seller_id)

        resp = self._replicate(context, {
            "role": "seller",
            "action": "ChangeItemPrice",
            "data": {"item_id": request.item_id, "new_price": request.new_price,
                     "session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"ChangePrice handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return product_pb2.Empty()

    def UpdateQuantity(self, request, context):
        self._require_leader(context)
        valid, seller_id = validate_session(request.session_token, "seller")
        if not valid:
            _debug("UpdateQuantity rejected: invalid or expired seller session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, seller_id, "seller")
        self._ensure_seller(seller_id)

        resp = self._replicate(context, {
            "role": "seller",
            "action": "UpdateUnitsForSale",
            "data": {"item_id": request.item_id, "quantity": request.quantity,
                     "session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"UpdateQuantity handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return product_pb2.Empty()

    def DisplayItemsForSale(self, request, context):
        self._require_leader(context)
        valid, seller_id = validate_session(request.session_token, "seller")
        if not valid:
            _debug("DisplayItemsForSale rejected: invalid or expired seller session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, seller_id, "seller")
        self._ensure_seller(seller_id)

        resp = self._run(seller.handle, {
            "req_id": "grpc_display_items", "action": "DisplayItemsForSale",
            "data": {"session_token": request.session_token}
        })
        if not resp.get("ok"):
            _debug(f"DisplayItemsForSale handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        # seller to_public_dict() returns item_name, sale_price, item_quantity
        return product_pb2.ItemList(items=[_item_to_proto(it) for it in resp["data"]["items"]])

    # ==================================================
    # BUYER PRODUCT APIs
    # ==================================================

    def SearchItems(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("SearchItems rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._run(buyer.handle, {
            "req_id": "grpc_search", "action": "SearchItemsForSale",
            "data": {"item_category": request.category, "session_token": request.session_token}
        })
        if not resp.get("ok"):
            _debug(f"SearchItems handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        # buyer search returns to_public_dict() — item_name, sale_price, item_quantity
        return product_pb2.ItemList(items=[_item_to_proto(it) for it in resp["data"]["items"]])

    def GetItem(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("GetItem rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._run(buyer.handle, {
            "req_id": "grpc_get_item", "action": "GetItem",
            "data": {"item_id": request.item_id, "session_token": request.session_token}
        })
        if not resp.get("ok"):
            _debug(f"GetItem handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        # GetItem returns {"item": to_public_dict()}
        return _item_to_proto(resp["data"]["item"])

    # ==================================================
    # CART APIs
    # ==================================================

    def AddToCart(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("AddToCart rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._replicate(context, {
            "role": "buyer",
            "action": "AddItemToCart",
            "data": {"item_id": request.item_id, "quantity": request.quantity,
                     "session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"AddToCart handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return product_pb2.Empty()

    def RemoveFromCart(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("RemoveFromCart rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._replicate(context, {
            "role": "buyer",
            "action": "RemoveItemFromCart",
            "data": {"item_id": request.item_id, "quantity": request.quantity,
                     "session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"RemoveFromCart handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return product_pb2.Empty()

    def SaveCart(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("SaveCart rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._replicate(context, {
            "role": "buyer",
            "action": "SaveCart",
            "data": {"session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"SaveCart handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return product_pb2.Empty()

    def ClearCart(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("ClearCart rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._replicate(context, {
            "role": "buyer",
            "action": "ClearCart",
            "data": {"session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"ClearCart handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return product_pb2.Empty()

    def GetCart(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("GetCart rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._run(buyer.handle, {
            "req_id": "grpc_get_cart", "action": "DisplayCart",
            "data": {"session_token": request.session_token}
        })
        if not resp.get("ok"):
            _debug(f"GetCart handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        # DisplayCart returns: {"cart": [{"item_id": {...}, "qty": int}]}
        return product_pb2.CartResponse(
            items=[_cart_item_to_proto(ci) for ci in resp["data"]["cart"]]
        )

    # ==================================================
    # FEEDBACK
    # ==================================================

    def ProvideFeedback(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("ProvideFeedback rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._replicate(context, {
            "role": "buyer",
            "action": "ProvideFeedback",
            "data": {"item_id": request.item_id, "feedback": request.feedback,
                     "session_token": request.session_token},
        })
        if not resp.get("ok"):
            _debug(f"ProvideFeedback handler error: {resp.get('error', 'error')}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))

        seller_id = 0
        local_item = asyncio.run(self.state.db.get_item(ItemId.from_any(request.item_id)))
        if local_item is not None:
            seller_id = int(local_item.seller_id)

        vote = str(request.feedback or "").strip().lower()
        thumbs_up = 1 if vote in {"up", "thumbs_up", "thumbsup", "1"} else 0
        thumbs_down = 1 if vote in {"down", "thumbs_down", "thumbsdown", "-1"} else 0
        if seller_id:
            for stub in _buyer_customer_stubs:
                try:
                    stub.RecordSellerFeedback(
                        customer_pb2.RecordSellerFeedbackRequest(
                            seller_id=seller_id,
                            thumbs_up=thumbs_up,
                            thumbs_down=thumbs_down,
                        )
                    )
                    break
                except grpc.RpcError:
                    continue
        return product_pb2.Empty()

    # ==================================================
    # PURCHASE (PA2)
    # ==================================================

    def FinalizePurchase(self, request, context):
        self._require_leader(context)
        valid, buyer_id = validate_session(request.session_token, "buyer")
        if not valid:
            _debug("FinalizePurchase rejected: invalid or expired buyer session")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid or expired session")
        self._ensure_local_session(request.session_token, buyer_id, "buyer")
        self._ensure_buyer(buyer_id)

        resp = self._replicate(context, {
            "role": "buyer",
            "action": "MakePurchase",
            "data": {
                "session_token": request.session_token,
                "txn_id": f"txn_{uuid.uuid4().hex[:12]}",
            },
        })
        if not resp.get("ok"):
            _debug(f"FinalizePurchase handler error: {resp.get('error', 'Purchase failed')}")
            return product_pb2.PurchaseResult(
                success=False, message=resp.get("error", "Purchase failed")
            )

        try:
            total_units = sum(
                line.get("qty", 1)
                for line in resp["data"]["transaction"].get("items", [])
            )
            for stub in _buyer_customer_stubs:
                try:
                    stub.RecordPurchase(
                        customer_pb2.RecordPurchaseRequest(
                            session_token=request.session_token,
                            total_units=total_units
                        )
                    )
                    break
                except grpc.RpcError:
                    continue
        except grpc.RpcError:
            pass

        return product_pb2.PurchaseResult(
            success=True, message="Purchase completed successfully"
        )


def serve():
    global _RAFT_NODE
    replicas = cfg.backend_product_db.targets()
    raft_members = [(replica.host, replica.port + 1000) for replica in replicas]
    member_id = max(0, min(args.replica_id, len(raft_members) - 1))
    port = replicas[member_id].port
    if raft_members:
        my_host, my_raft_port = raft_members[member_id]
        _RAFT_NODE = SimpleRaftNode(
            member_id=member_id,
            members=raft_members,
            apply_fn=_apply_product_mutation,
            udp_host=my_host,
            udp_port=my_raft_port,
        )
        _RAFT_NODE.start()
        threading.Thread(target=_write_runtime_status, args=(member_id,), daemon=True).start()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    product_pb2_grpc.add_ProductServiceServicer_to_server(
        ProductService(_SHARED_STATE), server
    )
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"Product gRPC server listening on port {port}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
