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


def _debug(message: str) -> None:
    print(f"[customer-replica {args.replica_id}] {message}", flush=True)


def _login_data_from_request(username: str, password: str, role: str) -> dict:
    raw_username = str(username)
    if raw_username.startswith("__id__:"):
        numeric_id = int(raw_username.split(":", 1)[1])
        id_key = "buyer_id" if role == "buyer" else "seller_id"
        return {id_key: numeric_id, "password": password}
    return {"username": raw_username, "password": password}


def _apply_customer_operation(payload: dict) -> dict:
    role = str(payload.get("role") or "")
    action = str(payload.get("action") or "")
    data = dict(payload.get("data") or {})
    _debug(f"apply action={action} role={role}")

    if role == "buyer":
        result = asyncio.run(buyer.handle(_SHARED_STATE, {
            "req_id": f"atomic_{action}",
            "action": action,
            "data": data,
        }))
        _debug(f"apply result action={action} role={role} ok={result.get('ok')}")
        return result

    if role == "seller":
        result = asyncio.run(seller.handle(_SHARED_STATE, {
            "req_id": f"atomic_{action}",
            "action": action,
            "data": data,
        }))
        _debug(f"apply result action={action} role={role} ok={result.get('ok')}")
        return result

    if role == "system" and action == "RecordPurchase":
        sess = asyncio.run(_SHARED_STATE.get_session(data.get("session_token", "")))
        if not sess or sess.role != "buyer":
            _debug("RecordPurchase rejected because session was missing")
            raise ValueError("invalid session")
        asyncio.run(_SHARED_STATE.db.inc_buyer_items_purchased(
            int(sess.principal_id), int(data.get("total_units", 0))
        ))
        _debug("RecordPurchase applied successfully")
        return {"ok": True, "data": {"recorded": True}}

    if role == "system" and action == "RecordSellerFeedback":
        asyncio.run(_SHARED_STATE.db.add_seller_feedback(
            int(data.get("seller_id", 0)),
            thumbs_up=int(data.get("thumbs_up", 0)),
            thumbs_down=int(data.get("thumbs_down", 0)),
        ))
        _debug("RecordSellerFeedback applied successfully")
        return {"ok": True, "data": {"recorded": True}}

    raise ValueError(f"unsupported customer mutation: role={role} action={action}")


def _broadcast_customer_mutation(payload: dict) -> dict:
    if _CUSTOMER_GROUP is None:
        _debug(f"local mutation action={payload.get('action')} role={payload.get('role')}")
        return _apply_customer_operation(payload)
    _debug(f"broadcast mutation action={payload.get('action')} role={payload.get('role')}")
    return _CUSTOMER_GROUP.submit(payload)


class CustomerService(customer_pb2_grpc.CustomerServiceServicer):
    """Handles buyer account operations."""

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def CreateAccount(self, request, context):
        _debug(f"CreateAccount request username={request.username!r} role=buyer")
        resp = _broadcast_customer_mutation({
            "role": "buyer",
            "action": "CreateAccount",
            "data": {"username": request.username, "password": request.password},
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return customer_pb2.CreateAccountResponse(user_id=resp["data"]["buyer_id"])

    def Login(self, request, context):
        _debug(f"Login request username={request.username!r} role=buyer")
        resp = _broadcast_customer_mutation({
            "role": "buyer",
            "action": "Login",
            "data": {
                **_login_data_from_request(request.username, request.password, "buyer"),
                "session_token": uuid.uuid4().hex,
            },
        })
        if not resp.get("ok"):
            _debug(f"Login failed role=buyer error={resp.get('error', 'invalid credentials')}")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, resp.get("error", "invalid credentials"))
        _debug(f"Login ok role=buyer token={resp['data']['session_token']}")
        return customer_pb2.LoginResponse(
            session_token=resp["data"]["session_token"],
            user_id=resp["data"]["buyer_id"]
        )

    def Logout(self, request, context):
        _debug(f"Logout request role=buyer token={request.session_token}")
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
            _debug(f"ValidateSession miss token={request.session_token}")
            return customer_pb2.SessionInfo(valid=False, principal_id=0, role="")
        _debug(f"ValidateSession hit token={request.session_token} principal={sess.principal_id} role={sess.role}")
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
            _debug(f"RecordPurchase request token={request.session_token} total_units={request.total_units}")
            _broadcast_customer_mutation({
                "role": "system",
                "action": "RecordPurchase",
                "data": {
                    "session_token": request.session_token,
                    "total_units": request.total_units,
                },
            })
        except Exception as exc:
            _debug(f"RecordPurchase failed error={exc}")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        return customer_pb2.Empty()

    def RecordSellerFeedback(self, request, context):
        try:
            _debug(
                f"RecordSellerFeedback request seller_id={request.seller_id} "
                f"up={request.thumbs_up} down={request.thumbs_down}"
            )
            _broadcast_customer_mutation({
                "role": "system",
                "action": "RecordSellerFeedback",
                "data": {
                    "seller_id": request.seller_id,
                    "thumbs_up": request.thumbs_up,
                    "thumbs_down": request.thumbs_down,
                },
            })
        except Exception as exc:
            _debug(f"RecordSellerFeedback failed error={exc}")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return customer_pb2.Empty()


class SellerCustomerService(customer_pb2_grpc.CustomerServiceServicer):
    """Handles seller account operations — same shared state as buyer service."""

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def CreateAccount(self, request, context):
        _debug(f"CreateAccount request username={request.username!r} role=seller")
        resp = _broadcast_customer_mutation({
            "role": "seller",
            "action": "CreateAccount",
            "data": {"username": request.username, "password": request.password},
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return customer_pb2.CreateAccountResponse(user_id=resp["data"]["seller_id"])

    def Login(self, request, context):
        _debug(f"Login request username={request.username!r} role=seller")
        resp = _broadcast_customer_mutation({
            "role": "seller",
            "action": "Login",
            "data": {
                **_login_data_from_request(request.username, request.password, "seller"),
                "session_token": uuid.uuid4().hex,
            },
        })
        if not resp.get("ok"):
            _debug(f"Login failed role=seller error={resp.get('error', 'invalid credentials')}")
            context.abort(grpc.StatusCode.UNAUTHENTICATED, resp.get("error", "invalid credentials"))
        _debug(f"Login ok role=seller token={resp['data']['session_token']}")
        return customer_pb2.LoginResponse(
            session_token=resp["data"]["session_token"],
            user_id=resp["data"]["seller_id"]
        )

    def Logout(self, request, context):
        _debug(f"Logout request role=seller token={request.session_token}")
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
            _debug(f"ValidateSession miss token={request.session_token}")
            return customer_pb2.SessionInfo(valid=False, principal_id=0, role="")
        _debug(f"ValidateSession hit token={request.session_token} principal={sess.principal_id} role={sess.role}")
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

    def RecordSellerFeedback(self, request, context):
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
