from __future__ import annotations

from src.clients.client_base import MarketplaceClient


class FailoverMarketplaceClient(MarketplaceClient):
    """PA3-compatible alias for the shared REST client with frontend failover."""
