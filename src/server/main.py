from __future__ import annotations

import argparse
import asyncio

from src.common.config import load_config
from src.backend.customer_db_server import run_customer_db
from src.backend.product_db_server import run_product_db
from src.frontend.buyer_frontend_server import run_server as run_buyer_frontend
from src.frontend.seller_frontend_server import run_server as run_seller_frontend


async def run_all(config_path: str) -> None:
    cfg = load_config(config_path)

    # Start backends (DBs)
    t1 = asyncio.create_task(
        run_customer_db(cfg.backend_customer_db.host, cfg.backend_customer_db.port, cfg.session.timeout_seconds)
    )
    t2 = asyncio.create_task(run_product_db(cfg.backend_product_db.host, cfg.backend_product_db.port))

    # Start stateless frontends
    t3 = asyncio.create_task(run_seller_frontend(config_path))
    t4 = asyncio.create_task(run_buyer_frontend(config_path))

    await asyncio.gather(t1, t2, t3, t4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    asyncio.run(run_all(args.config))


if __name__ == "__main__":
    main()
