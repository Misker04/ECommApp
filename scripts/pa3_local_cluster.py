from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.config import load_config


def _runtime_dir(config_path: str) -> Path:
    cfg = load_config(config_path)
    runtime_dir = cfg.storage.data_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _write_runtime_file(runtime_dir: Path, name: str, replica_id: int, pid: int, extra: dict | None = None) -> None:
    payload = {"name": name, "replica_id": replica_id, "pid": pid}
    if extra:
        payload.update(extra)
    (runtime_dir / f"{name}-{replica_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _spawn(cmd: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_cluster(config_path: str) -> None:
    cfg = load_config(config_path)
    runtime_dir = _runtime_dir(config_path)
    root = ROOT
    py = sys.executable

    soap = _spawn([py, "-m", "src.financial.soap_server", "--config", config_path], root)
    _write_runtime_file(runtime_dir, "soap", 0, soap.pid)

    for replica_id, _replica in enumerate(cfg.backend_customer_db.buyer_targets()):
        proc = _spawn([py, "-m", "src.backend.customer_grpc_server", "--config", config_path, "--replica-id", str(replica_id)], root)
        _write_runtime_file(runtime_dir, "customer-db", replica_id, proc.pid)

    for replica_id, _replica in enumerate(cfg.backend_product_db.targets()):
        proc = _spawn([py, "-m", "src.backend.product_grpc_server", "--config", config_path, "--replica-id", str(replica_id)], root)
        _write_runtime_file(runtime_dir, "product-db", replica_id, proc.pid, {"is_leader": False})

    for replica_id, _replica in enumerate(cfg.frontend_seller.targets()):
        proc = _spawn([py, "run_seller_server.py", "--config", config_path, "--replica-id", str(replica_id)], root)
        _write_runtime_file(runtime_dir, "seller-frontend", replica_id, proc.pid)

    for replica_id, _replica in enumerate(cfg.frontend_buyer.targets()):
        proc = _spawn([py, "run_buyer_server.py", "--config", config_path, "--replica-id", str(replica_id)], root)
        _write_runtime_file(runtime_dir, "buyer-frontend", replica_id, proc.pid)

    print(f"started cluster; runtime files in {runtime_dir}")
    time.sleep(2.0)


def stop_cluster(config_path: str) -> None:
    runtime_dir = _runtime_dir(config_path)
    for runtime_file in runtime_dir.glob("*.json"):
        try:
            info = json.loads(runtime_file.read_text(encoding="utf-8"))
            os.kill(int(info["pid"]), signal.SIGTERM)
        except Exception:
            pass
    print("stop signal sent to all known replica processes")


def status_cluster(config_path: str) -> None:
    runtime_dir = _runtime_dir(config_path)
    for runtime_file in sorted(runtime_dir.glob("*.json")):
        try:
            info = json.loads(runtime_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        print(f"{runtime_file.name}: {info}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("command", choices=["start", "stop", "status"])
    args = ap.parse_args()

    if args.command == "start":
        start_cluster(args.config)
    elif args.command == "stop":
        stop_cluster(args.config)
    else:
        status_cluster(args.config)


if __name__ == "__main__":
    main()
