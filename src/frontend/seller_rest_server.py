from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import grpc

from src.proto import customer_pb2, customer_pb2_grpc
from src.proto import product_pb2, product_pb2_grpc

import argparse
from src.common.config import load_config

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
args, _unknown = parser.parse_known_args()

cfg = load_config(args.config)
app = FastAPI()

# -------------------------------------------------
# VM Configurable gRPC Connections
# Seller account ops → Customer DB port 50053
# Product ops        → Product DB port 50052
# -------------------------------------------------

customer_channels = [
    grpc.insecure_channel(f"{replica.host}:{replica.port}")
    for replica in cfg.backend_customer_db.seller_targets()
]
product_channels = [
    grpc.insecure_channel(f"{replica.host}:{replica.port}")
    for replica in cfg.backend_product_db.targets()
]

customer_stubs = [customer_pb2_grpc.CustomerServiceStub(channel) for channel in customer_channels]
product_stubs = [product_pb2_grpc.ProductServiceStub(channel) for channel in product_channels]


def _call_any(stubs, method_name: str, request):
    last_error = None
    for stub in stubs:
        try:
            return getattr(stub, method_name)(request)
        except grpc.RpcError as exc:
            detail = (exc.details() or "").lower() if hasattr(exc, "details") else ""
            code = exc.code() if hasattr(exc, "code") else None
            last_error = exc
            if "not the leader" in detail or code == grpc.StatusCode.UNAVAILABLE:
                continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("no backend stubs configured")

# -------------------------------------------------
# Request Models
# -------------------------------------------------

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

# -------------------------------------------------
# Account APIs
# -------------------------------------------------

@app.post("/seller/create_account")
def create_account(req: AccountRequest):
    try:
        response = _call_any(customer_stubs, "CreateAccount",
            customer_pb2.CreateAccountRequest(
                username=req.username, password=req.password
            )
        )
        return {"seller_id": response.user_id}
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())


@app.post("/seller/login")
def login(req: AccountRequest):
    try:
        response = _call_any(customer_stubs, "Login",
            customer_pb2.LoginRequest(
                username=req.username, password=req.password
            )
        )
        return {"session_token": response.session_token, "seller_id": response.user_id}
    except grpc.RpcError as e:
        raise HTTPException(status_code=401, detail=e.details())


@app.post("/seller/logout")
def logout(req: SessionRequest):
    try:
        _call_any(customer_stubs, "Logout",
            customer_pb2.SessionRequest(session_token=req.session_token)
        )
        return {"status": "logged_out"}
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())


# -------------------------------------------------
# Seller Product Management
# -------------------------------------------------

@app.post("/seller/register_item")
def register_item(req: RegisterItemRequestModel):
    try:
        response = _call_any(product_stubs, "RegisterItem",
            product_pb2.RegisterItemRequest(
                name=req.name, category=req.category,
                price=req.price, quantity=req.quantity,
                session_token=req.session_token
            )
        )
        return {"item_id": response.item_id}
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())


@app.post("/seller/change_price")
def change_price(req: ChangePriceRequestModel):
    try:
        _call_any(product_stubs, "ChangePrice",
            product_pb2.ChangePriceRequest(
                item_id=req.item_id, new_price=req.new_price,
                session_token=req.session_token
            )
        )
        return {"status": "price_updated"}
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())


@app.post("/seller/update_quantity")
def update_quantity(req: UpdateQuantityRequestModel):
    try:
        _call_any(product_stubs, "UpdateQuantity",
            product_pb2.UpdateQuantityRequest(
                item_id=req.item_id, quantity=req.quantity,
                session_token=req.session_token
            )
        )
        return {"status": "quantity_updated"}
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())


@app.post("/seller/display_items")
def display_items(req: SessionRequest):
    try:
        response = _call_any(product_stubs, "DisplayItemsForSale",
            product_pb2.SessionRequest(session_token=req.session_token)
        )
        return {
            "items": [
                {"item_id": item.item_id, "name": item.name,
                 "price": item.price, "quantity": item.quantity}
                for item in response.items
            ]
        }
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())


@app.post("/seller/get_rating")
def get_rating(req: SellerRatingRequest):
    try:
        response = _call_any(customer_stubs, "GetSellerRating",
            customer_pb2.SessionSellerRequest(
                session_token=req.session_token,
                seller_id=req.seller_id
            )
        )
        return {"thumbs_up": response.thumbs_up, "thumbs_down": response.thumbs_down}
    except grpc.RpcError as e:
        raise HTTPException(status_code=400, detail=e.details())
