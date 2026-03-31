from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import yaml


@dataclass(frozen=True)
class FrontendReplicaConfig:
    host: str
    port: int


@dataclass(frozen=True)
class CustomerReplicaConfig:
    id: int
    host: str
    buyer_grpc_port: int
    seller_grpc_port: int
    udp_port: int


@dataclass(frozen=True)
class ProductReplicaConfig:
    id: int
    host: str
    grpc_port: int
    raft_port: int


@dataclass(frozen=True)
class SoapConfig:
    host: str
    port: int


@dataclass(frozen=True)
class Pa3Config:
    session_timeout_seconds: int
    customer_replicas: List[CustomerReplicaConfig]
    product_replicas: List[ProductReplicaConfig]
    buyer_frontend_replicas: List[FrontendReplicaConfig]
    seller_frontend_replicas: List[FrontendReplicaConfig]
    soap: SoapConfig


def _require_mapping(raw: object, name: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be a mapping")
    return raw


def _require_list(raw: object, name: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{name} must be a list")
    out: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{name}[{idx}] must be a mapping")
        out.append(item)
    return out


def _load_frontends(raw_list: list[dict]) -> List[FrontendReplicaConfig]:
    return [
        FrontendReplicaConfig(host=str(x["host"]), port=int(x["port"]))
        for x in raw_list
    ]


def _load_customer(raw_list: list[dict]) -> List[CustomerReplicaConfig]:
    return [
        CustomerReplicaConfig(
            id=int(x["id"]),
            host=str(x["host"]),
            buyer_grpc_port=int(x["buyer_grpc_port"]),
            seller_grpc_port=int(x["seller_grpc_port"]),
            udp_port=int(x["udp_port"]),
        )
        for x in raw_list
    ]


def _load_product(raw_list: list[dict]) -> List[ProductReplicaConfig]:
    return [
        ProductReplicaConfig(
            id=int(x["id"]),
            host=str(x["host"]),
            grpc_port=int(x["grpc_port"]),
            raft_port=int(x["raft_port"]),
        )
        for x in raw_list
    ]


def _ensure_unique_ids(items: Iterable[object], label: str) -> None:
    seen: set[int] = set()
    for item in items:
        item_id = int(getattr(item, "id"))
        if item_id in seen:
            raise ValueError(f"duplicate {label} id: {item_id}")
        seen.add(item_id)


def _ensure_unique_endpoints(endpoints: Iterable[tuple[str, int]], label: str) -> None:
    seen: set[tuple[str, int]] = set()
    for host, port in endpoints:
        key = (str(host), int(port))
        if key in seen:
            raise ValueError(f"duplicate {label} endpoint: {host}:{port}")
        seen.add(key)


def _validate_config(cfg: Pa3Config) -> None:
    if cfg.session_timeout_seconds <= 0:
        raise ValueError("session_timeout_seconds must be positive")

    if not cfg.customer_replicas:
        raise ValueError("customer_replicas must not be empty")
    if not cfg.product_replicas:
        raise ValueError("product_replicas must not be empty")
    if not cfg.buyer_frontend_replicas:
        raise ValueError("buyer_frontend_replicas must not be empty")
    if not cfg.seller_frontend_replicas:
        raise ValueError("seller_frontend_replicas must not be empty")

    _ensure_unique_ids(cfg.customer_replicas, "customer replica")
    _ensure_unique_ids(cfg.product_replicas, "product replica")

    _ensure_unique_endpoints(
        [(r.host, r.buyer_grpc_port) for r in cfg.customer_replicas],
        "customer buyer gRPC",
    )
    _ensure_unique_endpoints(
        [(r.host, r.seller_grpc_port) for r in cfg.customer_replicas],
        "customer seller gRPC",
    )
    _ensure_unique_endpoints(
        [(r.host, r.udp_port) for r in cfg.customer_replicas],
        "customer UDP",
    )
    _ensure_unique_endpoints(
        [(r.host, r.grpc_port) for r in cfg.product_replicas],
        "product gRPC",
    )
    _ensure_unique_endpoints(
        [(r.host, r.raft_port) for r in cfg.product_replicas],
        "product raft",
    )
    _ensure_unique_endpoints(
        [(r.host, r.port) for r in cfg.buyer_frontend_replicas],
        "buyer frontend",
    )
    _ensure_unique_endpoints(
        [(r.host, r.port) for r in cfg.seller_frontend_replicas],
        "seller frontend",
    )


def load_pa3_config(path: str | Path) -> Pa3Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = _require_mapping(raw, "root config")

    cfg = Pa3Config(
        session_timeout_seconds=int(raw.get("session_timeout_seconds", 300)),
        customer_replicas=_load_customer(_require_list(raw.get("customer_replicas"), "customer_replicas")),
        product_replicas=_load_product(_require_list(raw.get("product_replicas"), "product_replicas")),
        buyer_frontend_replicas=_load_frontends(_require_list(raw.get("buyer_frontend_replicas"), "buyer_frontend_replicas")),
        seller_frontend_replicas=_load_frontends(_require_list(raw.get("seller_frontend_replicas"), "seller_frontend_replicas")),
        soap=SoapConfig(
            host=str(_require_mapping(raw.get("soap"), "soap").get("host", "127.0.0.1")),
            port=int(_require_mapping(raw.get("soap"), "soap").get("port", 8000)),
        ),
    )
    _validate_config(cfg)
    return cfg