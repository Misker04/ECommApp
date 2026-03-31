from __future__ import annotations

import argparse

import grpc
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.pa3.config import load_pa3_config
from src.pa3.frontend.grpc_pool import BackendUnavailableError, GrpcReplicaPool, GrpcTarget
from src.proto import customer_pb2, customer_pb2_grpc
from src.proto import product_pb2, product_pb2_grpc


class AccountRequest(BaseModel):
    username: str
    password: str


class SessionRequest(BaseModel):
    session_token: str


class RegisterItemRequestModel(BaseModel):
    session_token: str
    name: str
    category: int
    price: float
    quantity: int


class ChangePriceRequestModel(BaseModel):
    session_token: str
    item_id: str
    new_price: float


class UpdateQuantityRequestModel(BaseModel):
    session_token: str
    item_id: str
    quantity: int


class SellerRatingRequest(BaseModel):
    session_token: str
    seller_id: int


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
    frontend = cfg.seller_frontend_replicas[frontend_index]

    customer_pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.seller_grpc_port) for r in cfg.customer_replicas],
        customer_pb2_grpc.CustomerServiceStub,
    )
    product_pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.grpc_port) for r in cfg.product_replicas],
        product_pb2_grpc.ProductServiceStub,
    )

    app = FastAPI(title="PA3 Seller Frontend")

    @app.post("/seller/create_account")
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
            return {"seller_id": response.user_id}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/seller/login")
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
            return {"session_token": response.session_token, "seller_id": response.user_id}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/seller/logout")
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

    @app.post("/seller/register_item")
    def register_item(req: RegisterItemRequestModel):
        try:
            response = product_pool.call(
                lambda stub, timeout: stub.RegisterItem(
                    product_pb2.RegisterItemRequest(
                        name=req.name,
                        category=req.category,
                        price=req.price,
                        quantity=req.quantity,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"item_id": response.item_id}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/seller/change_price")
    def change_price(req: ChangePriceRequestModel):
        try:
            product_pool.call(
                lambda stub, timeout: stub.ChangePrice(
                    product_pb2.ChangePriceRequest(
                        item_id=req.item_id,
                        new_price=req.new_price,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"status": "price_updated"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/seller/update_quantity")
    def update_quantity(req: UpdateQuantityRequestModel):
        try:
            product_pool.call(
                lambda stub, timeout: stub.UpdateQuantity(
                    product_pb2.UpdateQuantityRequest(
                        item_id=req.item_id,
                        quantity=req.quantity,
                        session_token=req.session_token,
                    ),
                    timeout=timeout,
                )
            )
            return {"status": "quantity_updated"}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    @app.post("/seller/display_items")
    def display_items(req: SessionRequest):
        try:
            response = product_pool.call(
                lambda stub, timeout: stub.DisplayItemsForSale(
                    product_pb2.SessionRequest(session_token=req.session_token),
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

    @app.post("/seller/get_rating")
    def get_rating(req: SellerRatingRequest):
        try:
            response = customer_pool.call(
                lambda stub, timeout: stub.GetSellerRating(
                    customer_pb2.SessionSellerRequest(
                        session_token=req.session_token,
                        seller_id=req.seller_id,
                    ),
                    timeout=timeout,
                )
            )
            return {"thumbs_up": response.thumbs_up, "thumbs_down": response.thumbs_down}
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except grpc.RpcError as exc:
            _raise_http_from_grpc(exc)

    return app, frontend.host, frontend.port


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--frontend-index", required=True, type=int)
    args = parser.parse_args()

    built_app, host, port = build_app(args.config, args.frontend_index)
    uvicorn.run(built_app, host=host, port=port)