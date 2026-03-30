from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml

@dataclass(frozen=True)
class ReplicaEndpoint:
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class BackendCustomerDBConfig:
    host: str
    port: int
    seller_port: int
    replicas: tuple[ReplicaEndpoint, ...] = ()
    seller_replicas: tuple[ReplicaEndpoint, ...] = ()

    def buyer_targets(self) -> List[ReplicaEndpoint]:
        return list(self.replicas or (ReplicaEndpoint(self.host, self.port),))

    def seller_targets(self) -> List[ReplicaEndpoint]:
        return list(self.seller_replicas or (ReplicaEndpoint(self.host, self.seller_port),))

@dataclass(frozen=True)
class EndpointConfig:
    host: str
    port: int
    replicas: tuple[ReplicaEndpoint, ...] = ()

    def targets(self) -> List[ReplicaEndpoint]:
        return list(self.replicas or (ReplicaEndpoint(self.host, self.port),))


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class SessionConfig:
    timeout_seconds: int


@dataclass(frozen=True)
class FeatureConfig:
    enable_make_purchase: bool


@dataclass(frozen=True)
class AppConfig:
    frontend_buyer: EndpointConfig
    frontend_seller: EndpointConfig
    backend_customer_db: BackendCustomerDBConfig
    backend_product_db: EndpointConfig
    soap: EndpointConfig
    session: SessionConfig
    features: FeatureConfig
    storage: StorageConfig
    logging: LoggingConfig


def _replicas(raw_replicas: Any, default_host: str, default_port: int) -> tuple[ReplicaEndpoint, ...]:
    replicas: List[ReplicaEndpoint] = []
    for entry in raw_replicas or []:
        if not isinstance(entry, dict):
            continue
        replicas.append(
            ReplicaEndpoint(
                host=str(entry.get("host", default_host)),
                port=int(entry.get("port", default_port)),
            )
        )
    return tuple(replicas)


def _endpoint(raw: Dict[str, Any], key: str, default_port: int) -> EndpointConfig:
    d = raw.get(key, {}) or {}
    host = str(d.get("host", "127.0.0.1"))
    port = int(d.get("port", default_port))
    return EndpointConfig(
        host=host,
        port=port,
        replicas=_replicas(d.get("replicas"), host, port),
    )


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    raw: Dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    session_raw = raw.get("session", {}) or {}
    features_raw = raw.get("features", {}) or {}
    storage_raw = raw.get("storage", {}) or {}
    logging_raw = raw.get("logging", {}) or {}

    customer_raw = raw.get("backend_customer_db", {}) or {}
    customer_host = str(customer_raw.get("host", "127.0.0.1"))
    customer_port = int(customer_raw.get("port", 50051))
    customer_seller_port = int(customer_raw.get("seller_port", 50053))

    return AppConfig(
        frontend_buyer=_endpoint(raw, "frontend_buyer", 8090),
        frontend_seller=_endpoint(raw, "frontend_seller", 8080),
        backend_customer_db=BackendCustomerDBConfig(
            host=customer_host,
            port=customer_port,
            seller_port=customer_seller_port,
            replicas=_replicas(customer_raw.get("replicas"), customer_host, customer_port),
            seller_replicas=_replicas(customer_raw.get("seller_replicas"), customer_host, customer_seller_port),
        ),
        backend_product_db=_endpoint(raw, "backend_product_db", 50052),
        soap=_endpoint(raw, "soap", 8000),
        session=SessionConfig(timeout_seconds=int(session_raw.get("timeout_seconds", 300))),
        features=FeatureConfig(enable_make_purchase=bool(features_raw.get("enable_make_purchase", True))),
        storage=StorageConfig(data_dir=Path(storage_raw.get("data_dir", "./data"))),
        logging=LoggingConfig(level=str(logging_raw.get("level", "INFO"))),
    )
