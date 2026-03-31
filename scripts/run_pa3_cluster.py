from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import grpc

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pa3.config import load_pa3_config
from src.pa3.frontend.grpc_pool import GrpcReplicaPool, GrpcTarget
from src.proto import customer_pb2, customer_pb2_grpc
from src.proto import product_pb2, product_pb2_grpc


def _python_executable(root: Path) -> str:
    return str(root / ".venv310" / "Scripts" / "python.exe")


def _start_process(root: Path, log_dir: Path, name: str, args: list[str]) -> subprocess.Popen:
    stdout_path = log_dir / f"{name}.out.log"
    stderr_path = log_dir / f"{name}.err.log"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [_python_executable(root), "-u", *args],
        cwd=str(root),
        stdout=stdout_path.open("w", encoding="utf-8"),
        stderr=stderr_path.open("w", encoding="utf-8"),
        env=env,
    )
    print(f"Started {name} (PID {proc.pid})", flush=True)
    return proc


def _wait_for_customer(cfg, timeout_s: float) -> None:
    pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.seller_grpc_port) for r in cfg.customer_replicas],
        customer_pb2_grpc.CustomerServiceStub,
        connect_timeout_s=0.5,
        call_timeout_s=2.0,
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
            print("customer cluster ready", flush=True)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"customer cluster did not become ready within {timeout_s:.1f}s: {last_error}")


def _wait_for_product(cfg, timeout_s: float) -> None:
    pool = GrpcReplicaPool(
        [GrpcTarget(r.host, r.grpc_port) for r in cfg.product_replicas],
        product_pb2_grpc.ProductServiceStub,
        connect_timeout_s=0.5,
        call_timeout_s=2.0,
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
            print("product cluster ready", flush=True)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"product cluster did not become ready within {timeout_s:.1f}s: {last_error}")


def _wait_for_frontend(host: str, port: int, timeout_s: float, label: str) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                print(f"{label} ready", flush=True)
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"{label} did not become ready within {timeout_s:.1f}s: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    root = _repo_root()
    cfg = load_pa3_config(args.config)
    log_dir = root / "logs" / "pa3"
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"Logs: {log_dir}", flush=True)

    _start_process(root, log_dir, "soap", ["-m", "src.financial.soap_server", "--config", args.config])
    time.sleep(1)

    for replica_id in range(5):
        _start_process(
            root,
            log_dir,
            f"customer_{replica_id}",
            ["-m", "src.pa3.customer_replica_server", "--config", args.config, "--replica-id", str(replica_id)],
        )

    _wait_for_customer(cfg, 30.0)

    for replica_id in range(5):
        _start_process(
            root,
            log_dir,
            f"product_{replica_id}",
            ["-m", "src.pa3.product_replica_server", "--config", args.config, "--replica-id", str(replica_id)],
        )

    _wait_for_product(cfg, 30.0)

    for frontend_index in range(4):
        _start_process(
            root,
            log_dir,
            f"seller_fe_{frontend_index}",
            ["-m", "src.pa3.frontend.seller_rest_server_pa3", "--config", args.config, "--frontend-index", str(frontend_index)],
        )

    for frontend_index in range(4):
        _start_process(
            root,
            log_dir,
            f"buyer_fe_{frontend_index}",
            ["-m", "src.pa3.frontend.buyer_rest_server_pa3", "--config", args.config, "--frontend-index", str(frontend_index)],
        )

    _wait_for_frontend(cfg.seller_frontend_replicas[0].host, cfg.seller_frontend_replicas[0].port, 15.0, "seller frontend")
    _wait_for_frontend(cfg.buyer_frontend_replicas[0].host, cfg.buyer_frontend_replicas[0].port, 15.0, "buyer frontend")

    print("PA3 processes launched.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
