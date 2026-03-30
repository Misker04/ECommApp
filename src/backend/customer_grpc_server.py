"""
customer_grpc_server.py
-----------------------
Runs on the Customer DB VM (port 50051 for buyers, 50053 for sellers).
This is the SINGLE SOURCE OF TRUTH for all sessions.
Product DB calls ValidateSession here instead of checking locally.
This is what makes separate VM processes work correctly.
"""

import asyncio
import grpc
import os
import uuid
from concurrent import futures

from src.proto import customer_pb2, customer_pb2_grpc
from src.server.state import MarketState
from src.server.handlers import buyer, seller
from src.replication.rotating_sequencer import RotatingSequencerGroup

import argparse
from src.common.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--replica-id", type=int, default=0)
args = parser.parse_args()

cfg = load_config(args.config)
# One shared state for this entire process
_SHARED_STATE = MarketState()
_CUSTOMER_GROUP: RotatingSequencerGroup | None = None


def _apply_customer_operation(payload: dict) -> dict:
    role = str(payload.get("role") or "")
    action = str(payload.get("action") or "")
    data = dict(payload.get("data") or {})

    if role == "buyer":
        return asyncio.run(buyer.handle(_SHARED_STATE, {
            "req_id": f"atomic_{action}",
            "action": action,
            "data": data,
        }))

    if role == "seller":
        return asyncio.run(seller.handle(_SHARED_STATE, {
            "req_id": f"atomic_{action}",
            "action": action,
            "data": data,
        }))

    if role == "system" and action == "RecordPurchase":
        sess = asyncio.run(_SHARED_STATE.get_session(data.get("session_token", "")))
        if not sess or sess.role != "buyer":
            raise ValueError("invalid session")
        asyncio.run(_SHARED_STATE.db.inc_buyer_items_purchased(
            int(sess.principal_id), int(data.get("total_units", 0))
        ))
        return {"ok": True, "data": {"recorded": True}}

    raise ValueError(f"unsupported customer mutation: role={role} action={action}")


def _broadcast_customer_mutation(payload: dict) -> dict:
    if _CUSTOMER_GROUP is None:
        return _apply_customer_operation(payload)
    return _CUSTOMER_GROUP.submit(payload)


class CustomerService(customer_pb2_grpc.CustomerServiceServicer):
    """Handles buyer account operations."""

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def CreateAccount(self, request, context):
        resp = _broadcast_customer_mutation({
            "role": "buyer",
            "action": "CreateAccount",
            "data": {"username": request.username, "password": request.password},
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return customer_pb2.CreateAccountResponse(user_id=resp["data"]["buyer_id"])

    def Login(self, request, context):
        resp = _broadcast_customer_mutation({
            "role": "buyer",
            "action": "Login",
            "data": {
                "username": request.username,
                "password": request.password,
                "session_token": uuid.uuid4().hex,
            },
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, resp.get("error", "invalid credentials"))
        return customer_pb2.LoginResponse(
            session_token=resp["data"]["session_token"],
            user_id=resp["data"]["buyer_id"]
        )

    def Logout(self, request, context):
        _broadcast_customer_mutation({
            "role": "buyer",
            "action": "Logout",
            "data": {"session_token": request.session_token},
        })
        return customer_pb2.Empty()

    def ValidateSession(self, request, context):
        """
        KEY RPC — called by Product DB VM to validate a session token.
        Returns principal_id and role so Product DB never needs
        its own session store.
        """
        sess = asyncio.run(self.state.get_session(request.session_token))
        if not sess:
            return customer_pb2.SessionInfo(valid=False, principal_id=0, role="")
        return customer_pb2.SessionInfo(
            valid=True,
            principal_id=int(sess.principal_id),
            role=str(sess.role)
        )

    def GetSellerRating(self, request, context):
        resp = self._run(buyer.handle, {
            "req_id": "grpc_get_rating", "action": "GetSellerRating",
            "data": {"seller_id": request.seller_id, "session_token": request.session_token}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        rating = resp["data"]["seller_feedback"]
        return customer_pb2.RatingResponse(
            thumbs_up=rating["thumbs_up"],
            thumbs_down=rating["thumbs_down"]
        )

    def GetBuyerPurchases(self, request, context):
        resp = self._run(buyer.handle, {
            "req_id": "grpc_get_purchases", "action": "GetBuyerPurchases",
            "data": {"session_token": request.session_token}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        purchases = resp["data"]["purchases"]
        item_ids = [
            f"{p['item_id']['category']}:{p['item_id']['number']}"
            for p in purchases
        ]
        return customer_pb2.PurchaseList(item_ids=item_ids)

    def RecordPurchase(self, request, context):
        try:
            _broadcast_customer_mutation({
                "role": "system",
                "action": "RecordPurchase",
                "data": {
                    "session_token": request.session_token,
                    "total_units": request.total_units,
                },
            })
        except Exception as exc:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        return customer_pb2.Empty()


class SellerCustomerService(customer_pb2_grpc.CustomerServiceServicer):
    """Handles seller account operations — same shared state as buyer service."""

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def CreateAccount(self, request, context):
        resp = _broadcast_customer_mutation({
            "role": "seller",
            "action": "CreateAccount",
            "data": {"username": request.username, "password": request.password},
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return customer_pb2.CreateAccountResponse(user_id=resp["data"]["seller_id"])

    def Login(self, request, context):
        resp = _broadcast_customer_mutation({
            "role": "seller",
            "action": "Login",
            "data": {
                "username": request.username,
                "password": request.password,
                "session_token": uuid.uuid4().hex,
            },
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, resp.get("error", "invalid credentials"))
        return customer_pb2.LoginResponse(
            session_token=resp["data"]["session_token"],
            user_id=resp["data"]["seller_id"]
        )

    def Logout(self, request, context):
        _broadcast_customer_mutation({
            "role": "seller",
            "action": "Logout",
            "data": {"session_token": request.session_token},
        })
        return customer_pb2.Empty()

    def ValidateSession(self, request, context):
        """Same session store — sellers and buyers share the same DB."""
        sess = asyncio.run(self.state.get_session(request.session_token))
        if not sess:
            return customer_pb2.SessionInfo(valid=False, principal_id=0, role="")
        return customer_pb2.SessionInfo(
            valid=True,
            principal_id=int(sess.principal_id),
            role=str(sess.role)
        )

    def GetSellerRating(self, request, context):
        resp = self._run(seller.handle, {
            "req_id": "grpc_seller_get_rating", "action": "GetSellerRating",
            "data": {"seller_id": request.seller_id, "session_token": request.session_token}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        rating = resp["data"]["seller_feedback"]
        return customer_pb2.RatingResponse(
            thumbs_up=rating["thumbs_up"],
            thumbs_down=rating["thumbs_down"]
        )

    def GetBuyerPurchases(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "not applicable for sellers")

    def RecordPurchase(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "not applicable for sellers")


def serve():
    global _CUSTOMER_GROUP
    buyer_replicas = cfg.backend_customer_db.buyer_targets()
    seller_replicas = cfg.backend_customer_db.seller_targets()
    udp_members = [(replica.host, replica.port + 1000) for replica in buyer_replicas]
    member_id = max(0, min(args.replica_id, len(udp_members) - 1))
    buyer_target = buyer_replicas[member_id]
    seller_target = seller_replicas[min(member_id, len(seller_replicas) - 1)]
    buyer_port = buyer_target.port
    seller_port = seller_target.port
    if udp_members:
        my_host, my_udp_port = udp_members[member_id]
        _CUSTOMER_GROUP = RotatingSequencerGroup(
            member_id=member_id,
            members=udp_members,
            apply_fn=_apply_customer_operation,
            udp_host=my_host,
            udp_port=my_udp_port,
        )
        _CUSTOMER_GROUP.start()

    buyer_server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    customer_pb2_grpc.add_CustomerServiceServicer_to_server(
        CustomerService(_SHARED_STATE), buyer_server
    )
    buyer_server.add_insecure_port(f"[::]:{buyer_port}")
    buyer_server.start()
    print(f"Customer gRPC (buyer)  listening on port {buyer_port}")

    seller_server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    customer_pb2_grpc.add_CustomerServiceServicer_to_server(
        SellerCustomerService(_SHARED_STATE), seller_server
    )
    seller_server.add_insecure_port(f"[::]:{seller_port}")
    seller_server.start()
    print(f"Customer gRPC (seller) listening on port {seller_port}")

    buyer_server.wait_for_termination()


if __name__ == "__main__":
    serve()
