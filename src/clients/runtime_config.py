from __future__ import annotations

from src.common.config import load_config
from src.pa3.config import load_pa3_config


def frontend_base_urls(config_path: str, role: str) -> list[str]:
    normalized_role = str(role).strip().lower()
    if normalized_role not in {"buyer", "seller"}:
        raise ValueError("role must be 'buyer' or 'seller'")

    try:
        cfg = load_pa3_config(config_path)
    except Exception:
        cfg = load_config(config_path)
        endpoint = cfg.frontend_buyer if normalized_role == "buyer" else cfg.frontend_seller
        return [f"http://{endpoint.host}:{endpoint.port}"]

    replicas = cfg.buyer_frontend_replicas if normalized_role == "buyer" else cfg.seller_frontend_replicas
    return [f"http://{replica.host}:{replica.port}" for replica in replicas]
