from __future__ import annotations

import argparse

import grpc
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.financial.soap_client import SOAPClient
from src.pa3.config import load_pa3_config
from src.pa3.frontend.grpc_pool import BackendUnavailableError, GrpcReplicaPool, GrpcTarget
from src.proto import customer_pb2, customer_pb2_grpc
from src.proto import product_pb2, product_pb2_grpc


class AccountRequest(BaseModel):
    username: str
    password: str


class SessionRequest(BaseModel):
    session_token: str


class SearchRequestModel(BaseModel):
    item_category: int
    session_token: str


class ItemRequestModel(BaseModel):
    item_id: str
    session_token: str


class CartRequestModel(BaseModel):
    item_id: str
    quantity: int
    session_token: str


class SellerRatingRequest(BaseModel):
    seller_id: int
    session_token: str


class FeedbackRequestModel(BaseModel):
    item_id: str
    feedback: str
    session_token: str


class PurchaseRequest(BaseModel):
    session_token: str
    card_name: str
    card_number: str
    expiration: str
    security_code: str


def _raise_http_from_grpc(exc: grpc.RpcError) -> None:
    mapping = {
        grpc.StatusCode.INVALID_ARGUMENT: 400,
        grpc.StatusCode.FAILED_PRECONDITION: 400,
        grpc.StatusCode.NOT_FOUND: 404,
        grpc.StatusCode.ALREADY_EXISTS: 409,
        grpc.StatusCode.UNAUTHENTICATED: 401,
        grpc.StatusCode.PERMISSION_DENIED: 403,
        grpc.StatusCode.UNAVAILABLE: 503,
        grpc.StatusCode.DEADLINE_EXCEEDED: 504,
    }
    status = mapping.get(exc.code(), 500)
    detail = exc.details() or exc.code().name
    raise HTTPException(status_code=status, detail=detail)


def build_app(config_path: str, frontend_index: int) -> tuple[FastAPI, str, int]:
    cfg = load_pa3_config(config_path)
    frontend = cfg.buyer_frontend_replicas[frontend_index]

    customer_pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.buyer_grpc_port) for r in cfg.customer_replicas],
        customer_pb2_grpc.CustomerServiceStub,
    )
    product_pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.grpc_port) for r in cfg.product_replicas],
        product_pb2_grpc.ProductServiceStub,
    )
    soap_client = SOAPClient(f"http://{cfg.soap.host}:{cfg.soap.port}/?wsdl")

    app = FastAPI(title="PA3 Buyer Frontend")

    @app.post("/buyer/create_account")
    def create_account(req: AccountRequest):
        try:
            response = customer_pool.call(
                lambda stub, timeout: stub.CreateAccount(
                    customer_pb2.CreateAccountRequest(
                        username=req.username,
                        password=req.password,
                    ),
                    timeout=timeout,
                )
            )
            return {"buyer_id": response.user_id}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/login")
    def login(req: AccountRequest):
        try:
            response = customer_pool.call(
                lambda stub, timeout: stub.Login(
                    customer_pb2.LoginRequest(
                        username=req.username,
                        password=req.password,
                    ),
                    timeout=timeout,
                )
            )
            return {"session_token": response.session_token, "buyer_id": response.user_id}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/logout")
    def logout(req: SessionRequest):
        try:
            customer_pool.call(
                lambda stub, timeout: stub.Logout(
                    customer_pb2.SessionRequest(session_token=req.session_token),
                    timeout=timeout,
                )
            )
            return {"status": "logged_out"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/search")
    def search_items(req: SearchRequestModel):
        try:
            response = product_pool.call(
                lambda stub, timeout: stub.SearchItems(
                    product_pb2.SearchRequest(
                        category=req.item_category,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {
                "items": [
                    {
                        "item_id": item.item_id,
                        "name": item.name,
                        "price": item.price,
                        "quantity": item.quantity,
                    }
                    for item in response.items
                ]
            }
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/get_item")
    def get_item(req: ItemRequestModel):
        try:
            item = product_pool.call(
                lambda stub, timeout: stub.GetItem(
                    product_pb2.ItemRequest(
                        item_id=req.item_id,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {
                "item_id": item.item_id,
                "name": item.name,
                "price": item.price,
                "quantity": item.quantity,
            }
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/add_to_cart")
    def add_to_cart(req: CartRequestModel):
        try:
            product_pool.call(
                lambda stub, timeout: stub.AddToCart(
                    product_pb2.CartRequest(
                        item_id=req.item_id,
                        quantity=req.quantity,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"status": "added_to_cart"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/remove_from_cart")
    def remove_from_cart(req: CartRequestModel):
        try:
            product_pool.call(
                lambda stub, timeout: stub.RemoveFromCart(
                    product_pb2.CartRequest(
                        item_id=req.item_id,
                        quantity=req.quantity,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"status": "removed_from_cart"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/save_cart")
    def save_cart(req: SessionRequest):
        try:
            product_pool.call(
                lambda stub, timeout: stub.SaveCart(
                    product_pb2.SessionRequest(session_token=req.session_token),
                    timeout=timeout,
                )
            )
            return {"status": "cart_saved"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/clear_cart")
    def clear_cart(req: SessionRequest):
        try:
            product_pool.call(
                lambda stub, timeout: stub.ClearCart(
                    product_pb2.SessionRequest(session_token=req.session_token),
                    timeout=timeout,
                )
            )
            return {"status": "cart_cleared"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/display_cart")
    def display_cart(req: SessionRequest):
        try:
            response = product_pool.call(
                lambda stub, timeout: stub.GetCart(
                    product_pb2.SessionRequest(session_token=req.session_token),
                    timeout=timeout,
                )
            )
            return {
                "cart": [
                    {
                        "item_id": item.item_id,
                        "quantity": item.quantity,
                    }
                    for item in response.items
                ]
            }
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/provide_feedback")
    def provide_feedback(req: FeedbackRequestModel):
        try:
            product_pool.call(
                lambda stub, timeout: stub.ProvideFeedback(
                    product_pb2.FeedbackRequest(
                        item_id=req.item_id,
                        feedback=req.feedback,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"status": "feedback_recorded"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/get_seller_rating")
    def get_seller_rating(req: SellerRatingRequest):
        try:
            response = customer_pool.call(
                lambda stub, timeout: stub.GetSellerRating(
                    customer_pb2.SessionSellerRequest(
                        seller_id=req.seller_id,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"thumbs_up": response.thumbs_up, "thumbs_down": response.thumbs_down}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/get_purchases")
    def get_purchases(req: SessionRequest):
        try:
            response = customer_pool.call(
                lambda stub, timeout: stub.GetBuyerPurchases(
                    customer_pb2.SessionRequest(session_token=req.session_token),
                    timeout=timeout,
                )
            )
            return {"item_ids": list(response.item_ids)}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/buyer/make_purchase")
    def make_purchase(req: PurchaseRequest):
        try:
            payment_result = soap_client.process_payment(
                req.card_name,
                req.card_number,
                req.expiration,
                req.security_code,
            )
            if payment_result != "Yes":
                raise HTTPException(status_code=400, detail="Payment declined")

            result = product_pool.call(
                lambda stub, timeout: stub.FinalizePurchase(
                    product_pb2.SessionRequest(session_token=req.session_token),
                    timeout=timeout,
                )
            )
            if not result.success:
                raise HTTPException(status_code=400, detail=result.message)

            return {"status": "purchase_completed"}
        except HTTPException:
            raise
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"financial service failure: {exc}")

    return app, frontend.host, frontend.port


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--frontend-index", required=True, type=int)
    args = parser.parse_args()

    built_app, host, port = build_app(args.config, args.frontend_index)
    uvicorn.run(built_app, host=host, port=port)