from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import grpc


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pa3.config import load_pa3_config
from src.pa3.frontend.grpc_pool import GrpcReplicaPool, GrpcTarget
from src.proto import customer_pb2, customer_pb2_grpc
from src.proto import product_pb2, product_pb2_grpc


def wait_for_customer(cfg, timeout_s: float) -> None:
    pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.seller_grpc_port) for r in cfg.customer_replicas],
        customer_pb2_grpc.CustomerServiceStub,
        connect_timeout_s=1.5,
        call_timeout_s=min(12.0, max(4.0, timeout_s / 6.0)),
    )
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool.call(
                lambda stub, timeout: stub.Barrier(
                    customer_pb2.BarrierRequest(nonce=uuid.uuid4().hex),
                    timeout=timeout,
                )
            )
            print("customer cluster ready")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"customer cluster did not become ready within {timeout_s:.1f}s: {last_error}")


def wait_for_product(cfg, timeout_s: float) -> None:
    pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.grpc_port) for r in cfg.product_replicas],
        product_pb2_grpc.ProductServiceStub,
        connect_timeout_s=1.0,
        call_timeout_s=min(8.0, max(3.0, timeout_s / 8.0)),
    )
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pool.call(
                lambda stub, timeout: stub.Barrier(
                    product_pb2.BarrierRequest(nonce=uuid.uuid4().hex),
                    timeout=timeout,
                )
            )
            print("product cluster ready")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"product cluster did not become ready within {timeout_s:.1f}s: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--target", choices=["customer", "product", "all"], default="all")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    cfg = load_pa3_config(args.config)

    if args.target in {"customer", "all"}:
        wait_for_customer(cfg, args.timeout)

    if args.target in {"product", "all"}:
        wait_for_product(cfg, args.timeout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
