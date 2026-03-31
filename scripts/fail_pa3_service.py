from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

import grpc


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pa3.config import load_pa3_config
from src.proto import product_pb2, product_pb2_grpc


NOT_LEADER_RE = re.compile(r"NOT_LEADER:(-?\d+)")


def _run_kill(pattern: str, *, host: str | None, ssh_user: str | None) -> bool:
    if ssh_user:
        if not host:
            raise ValueError("remote host is required when ssh_user is provided")
        remote_cmd = f"pkill -f -- {shlex.quote(pattern)}"
        proc = subprocess.run(
            ["ssh", f"{ssh_user}@{host}", remote_cmd],
            capture_output=True,
            text=True,
        )
    elif os.name == "nt":
        escaped = pattern.replace("'", "''")
        ps = (
            "$procs = Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -and $_.CommandLine -match '{escaped}' }}; "
            "if (-not $procs) { exit 1 }; "
            "$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
            "exit 0"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
        )
    else:
        proc = subprocess.run(
            ["pkill", "-f", "--", pattern],
            capture_output=True,
            text=True,
        )

    if proc.returncode in {0, 1}:
        return proc.returncode == 0
    raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"pkill failed with {proc.returncode}")


def _find_product_leader(config_path: str, timeout: float) -> int:
    cfg = load_pa3_config(config_path)
    inferred_leader: int | None = None

    for replica in cfg.product_replicas:
        address = f"{replica.host}:{replica.grpc_port}"
        try:
            with grpc.insecure_channel(address) as channel:
                grpc.channel_ready_future(channel).result(timeout=min(1.0, timeout))
                stub = product_pb2_grpc.ProductServiceStub(channel)
                stub.Barrier(
                    product_pb2.BarrierRequest(nonce=uuid.uuid4().hex),
                    timeout=timeout,
                )
            return replica.id
        except grpc.RpcError as exc:
            detail = exc.details() or exc.code().name
            match = NOT_LEADER_RE.search(detail)
            if match:
                leader_hint = int(match.group(1))
                if leader_hint >= 0:
                    inferred_leader = leader_hint
        except Exception:
            continue

    if inferred_leader is None:
        raise RuntimeError("could not determine current product leader")
    return inferred_leader


def _product_host_by_id(config_path: str, replica_id: int) -> str:
    cfg = load_pa3_config(config_path)
    replica = next(r for r in cfg.product_replicas if r.id == replica_id)
    return replica.host


def _frontend_host_by_index(config_path: str, frontend_index: int) -> str:
    cfg = load_pa3_config(config_path)
    seller = cfg.seller_frontend_replicas[frontend_index]
    buyer = cfg.buyer_frontend_replicas[frontend_index]
    if seller.host != buyer.host:
        raise RuntimeError(
            f"frontend index {frontend_index} seller host {seller.host} "
            f"does not match buyer host {buyer.host}"
        )
    return seller.host


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--mode",
        required=True,
        choices=["frontend", "product-replica", "product-follower", "product-leader"],
    )
    parser.add_argument("--frontend-index", type=int)
    parser.add_argument("--replica-id", type=int)
    parser.add_argument("--ssh-user")
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()

    if args.mode == "frontend":
        if args.frontend_index is None:
            raise SystemExit("--frontend-index is required for --mode frontend")
        host = _frontend_host_by_index(args.config, args.frontend_index) if args.ssh_user else None
        seller_pattern = (
            rf"src\.pa3\.frontend\.seller_rest_server_pa3.*--frontend-index\s+{args.frontend_index}\b"
        )
        buyer_pattern = (
            rf"src\.pa3\.frontend\.buyer_rest_server_pa3.*--frontend-index\s+{args.frontend_index}\b"
        )
        seller_hit = _run_kill(seller_pattern, host=host, ssh_user=args.ssh_user)
        buyer_hit = _run_kill(buyer_pattern, host=host, ssh_user=args.ssh_user)
        print(
            f"frontend_failed frontend_index={args.frontend_index} "
            f"seller_killed={seller_hit} buyer_killed={buyer_hit}"
        )
        return 0

    if args.mode == "product-replica":
        if args.replica_id is None:
            raise SystemExit("--replica-id is required for --mode product-replica")
        host = _product_host_by_id(args.config, args.replica_id) if args.ssh_user else None
        pattern = rf"src\.pa3\.product_replica_server.*--replica-id\s+{args.replica_id}\b"
        hit = _run_kill(pattern, host=host, ssh_user=args.ssh_user)
        print(f"product_replica_failed replica_id={args.replica_id} killed={hit}")
        return 0

    leader_id = _find_product_leader(args.config, args.timeout)
    cfg = load_pa3_config(args.config)

    if args.mode == "product-leader":
        target_id = leader_id
    else:
        non_leaders = [r.id for r in cfg.product_replicas if r.id != leader_id]
        if not non_leaders:
            raise RuntimeError("no follower replica available to fail")
        target_id = non_leaders[0]

    host = _product_host_by_id(args.config, target_id) if args.ssh_user else None
    pattern = rf"src\.pa3\.product_replica_server.*--replica-id\s+{target_id}\b"
    hit = _run_kill(pattern, host=host, ssh_user=args.ssh_user)
    print(
        f"{args.mode.replace('-', '_')}_failed "
        f"leader_id={leader_id} target_replica_id={target_id} killed={hit}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
