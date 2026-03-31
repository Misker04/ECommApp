from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pa3.config import load_pa3_config


VM_LAYOUT: dict[int, list[tuple[str, int | None]]] = {
    1: [
        ("soap", None),
        ("customer", 0),
        ("product", 0),
        ("seller_fe", 0),
        ("buyer_fe", 0),
    ],
    2: [
        ("customer", 1),
        ("product", 1),
        ("seller_fe", 1),
        ("buyer_fe", 1),
    ],
    3: [
        ("customer", 2),
        ("customer", 4),
        ("product", 2),
        ("seller_fe", 2),
        ("buyer_fe", 2),
    ],
    4: [
        ("customer", 3),
        ("product", 3),
        ("product", 4),
        ("seller_fe", 3),
        ("buyer_fe", 3),
    ],
}

PHASE_KINDS: dict[str, set[str]] = {
    "all": {"soap", "customer", "product", "seller_fe", "buyer_fe"},
    "backends": {"soap", "customer", "product"},
    "soap": {"soap"},
    "customers": {"customer"},
    "products": {"product"},
    "frontends": {"seller_fe", "buyer_fe"},
}


def _python_executable(root: Path) -> str:
    current = Path(sys.executable)
    if current.is_file():
        return str(current)

    candidates = [
        root / ".venv310" / "Scripts" / "python.exe",
        root / ".venv310" / "bin" / "python",
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    override = os.environ.get("PYTHON")
    if override:
        override_path = Path(override)
        if override_path.is_dir():
            for name in ("python.exe", "python"):
                candidate = override_path / name
                if candidate.exists():
                    return str(candidate)
        if override_path.is_file():
            return str(override_path)
        return override
    return sys.executable


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


def _wait_for_listener(host: str, port: int, timeout_s: float, label: str) -> None:
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


def _service_command(kind: str, index: int | None, config_path: str) -> tuple[str, list[str], tuple[str, int] | None]:
    cfg = load_pa3_config(config_path)

    if kind == "soap":
        return (
            "soap",
            ["-m", "src.financial.soap_server", "--config", config_path],
            (cfg.soap.host, cfg.soap.port),
        )

    if kind == "customer":
        assert index is not None
        replica = cfg.customer_replicas[index]
        return (
            f"customer_{index}",
            ["-m", "src.pa3.customer_replica_server", "--config", config_path, "--replica-id", str(index)],
            (replica.host, replica.seller_grpc_port),
        )

    if kind == "product":
        assert index is not None
        replica = cfg.product_replicas[index]
        return (
            f"product_{index}",
            ["-m", "src.pa3.product_replica_server", "--config", config_path, "--replica-id", str(index)],
            (replica.host, replica.grpc_port),
        )

    if kind == "seller_fe":
        assert index is not None
        frontend = cfg.seller_frontend_replicas[index]
        return (
            f"seller_fe_{index}",
            ["-m", "src.pa3.frontend.seller_rest_server_pa3", "--config", config_path, "--frontend-index", str(index)],
            (frontend.host, frontend.port),
        )

    if kind == "buyer_fe":
        assert index is not None
        frontend = cfg.buyer_frontend_replicas[index]
        return (
            f"buyer_fe_{index}",
            ["-m", "src.pa3.frontend.buyer_rest_server_pa3", "--config", config_path, "--frontend-index", str(index)],
            (frontend.host, frontend.port),
        )

    raise ValueError(f"unknown service kind: {kind}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--vm-id", required=True, type=int, choices=sorted(VM_LAYOUT))
    parser.add_argument("--phase", choices=sorted(PHASE_KINDS), default="all")
    args = parser.parse_args()

    root = _repo_root()
    log_dir = root / "logs" / "pa3_vm" / f"vm{args.vm_id}"
    log_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"Starting PA3 {args.phase} services for VM{args.vm_id} with config {args.config}",
        flush=True,
    )
    print(f"Logs: {log_dir}", flush=True)

    listeners: list[tuple[str, int, str]] = []
    allowed_kinds = PHASE_KINDS[args.phase]

    for kind, index in VM_LAYOUT[args.vm_id]:
        if kind not in allowed_kinds:
            continue
        name, command, listener = _service_command(kind, index, args.config)
        _start_process(root, log_dir, name, command)
        if listener is not None:
            listeners.append((listener[0], listener[1], name))
        if kind == "soap":
            time.sleep(1)

    for host, port, label in listeners:
        _wait_for_listener(host, port, 20.0, label)

    print(f"VM{args.vm_id} services launched.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
