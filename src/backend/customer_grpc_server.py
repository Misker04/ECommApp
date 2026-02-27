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
from concurrent import futures

from src.proto import customer_pb2, customer_pb2_grpc
from src.server.state import MarketState
from src.server.handlers import buyer, seller

import argparse
from src.common.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
args = parser.parse_args()

cfg = load_config(args.config)
# One shared state for this entire process
_SHARED_STATE = MarketState()


class CustomerService(customer_pb2_grpc.CustomerServiceServicer):
    """Handles buyer account operations."""

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def CreateAccount(self, request, context):
        resp = self._run(buyer.handle, {
            "req_id": "grpc_create_account", "action": "CreateAccount",
            "data": {"username": request.username, "password": request.password}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return customer_pb2.CreateAccountResponse(user_id=resp["data"]["buyer_id"])

    def Login(self, request, context):
        resp = self._run(buyer.handle, {
            "req_id": "grpc_login", "action": "Login",
            "data": {"username": request.username, "password": request.password}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, resp.get("error", "invalid credentials"))
        return customer_pb2.LoginResponse(
            session_token=resp["data"]["session_token"],
            user_id=resp["data"]["buyer_id"]
        )

    def Logout(self, request, context):
        self._run(buyer.handle, {
            "req_id": "grpc_logout", "action": "Logout",
            "data": {"session_token": request.session_token}
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
        """Called by Product DB after a successful purchase to update buyer history."""
        sess = asyncio.run(self.state.get_session(request.session_token))
        if not sess or sess.role != "buyer":
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid session")
        buyer_id = int(sess.principal_id)
        asyncio.run(self.state.db.inc_buyer_items_purchased(
            buyer_id, int(request.total_units)
        ))

        counts = Counter(list(request.item_ids))
        lines = []
        for key, qty in counts.items():
            item_id = ItemId.from_any(key)
            lines.append(TransactionLine(
                item_id=item_id,
                seller_id=0,     
                qty=int(qty),
                price_each=0.0  
            ))

        txn = Transaction(
            txn_id=new_id("txn"),
            buyer_id=buyer_id,
            items=lines,
            total=0.0
        )
        asyncio.run(self.state.db.add_transaction(txn))
        asyncio.run(self.state.db.inc_buyer_items_purchased(
            int(sess.principal_id), int(request.total_units)
        ))
        return customer_pb2.Empty()


class SellerCustomerService(customer_pb2_grpc.CustomerServiceServicer):
    """Handles seller account operations — same shared state as buyer service."""

    def __init__(self, state: MarketState):
        self.state = state

    def _run(self, handler, req_dict):
        return asyncio.run(handler(self.state, req_dict))

    def CreateAccount(self, request, context):
        resp = self._run(seller.handle, {
            "req_id": "grpc_seller_create", "action": "CreateAccount",
            "data": {"username": request.username, "password": request.password}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, resp.get("error", "error"))
        return customer_pb2.CreateAccountResponse(user_id=resp["data"]["seller_id"])

    def Login(self, request, context):
        resp = self._run(seller.handle, {
            "req_id": "grpc_seller_login", "action": "Login",
            "data": {"username": request.username, "password": request.password}
        })
        if not resp.get("ok"):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, resp.get("error", "invalid credentials"))
        return customer_pb2.LoginResponse(
            session_token=resp["data"]["session_token"],
            user_id=resp["data"]["seller_id"]
        )

    def Logout(self, request, context):
        self._run(seller.handle, {
            "req_id": "grpc_seller_logout", "action": "Logout",
            "data": {"session_token": request.session_token}
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
    buyer_port = cfg.backend_customer_db.port
    seller_port = cfg.backend_customer_db.seller_port

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
