from __future__ import annotations

import argparse
import asyncio
import shlex

from src.common.config import load_config
from src.clients.client_base import MarketplaceClient

HELP = """Seller CLI commands:
  create_account <seller_name> <password>
  login <seller_name_or_seller_id> <password>
  logout

  get_seller_rating
  register_item_for_sale <item_name> <item_category> <condition(new|used)> <sale_price> <item_quantity> [kw1,kw2,...]
  change_item_price <item_id> <new_price>
  update_units_for_sale <item_id> <remove_quantity>
  display_items_for_sale

  ping
  help
  quit
"""


def _maybe_int(s: str):
    try:
        return int(s)
    except Exception:
        return None


async def repl(client: MarketplaceClient) -> None:
    print(HELP)
    while True:
        line = input("seller> ").strip()
        if not line:
            continue
        if line in {"q", "quit", "exit"}:
            return
        if line == "help":
            print(HELP)
            continue

        parts = shlex.split(line)
        cmd = parts[0]
        args = parts[1:]

        if cmd in {"create_account", "register"} and len(args) == 2:
            resp = await client.request("CreateAccount", {"username": args[0], "password": args[1]})

        elif cmd == "login" and len(args) == 2:
            # We pass either name or numeric id in the same field. The server will resolve.
            username = args[0]
            resp = await client.request("Login", {"username": username, "password": args[1]})
            if resp.get("ok") and resp.get("data", {}).get("session_token"):
                client.session_token = resp["data"]["session_token"]

        elif cmd == "logout" and len(args) == 0:
            resp = await client.request("Logout", {})
            client.session_token = None

        elif cmd == "get_seller_rating" and len(args) == 0:
            resp = await client.request("GetSellerRating", {})

        elif cmd == "register_item_for_sale" and len(args) >= 5:
            # Allow an optional trailing keywords token "kw1,kw2".
            keywords_token = args[5] if len(args) >= 6 else ""
            item_name = args[0]
            item_category = int(args[1])
            condition = args[2]
            sale_price = float(args[3])
            item_quantity = int(args[4])
            keywords = [k.strip() for k in keywords_token.split(",") if k.strip()] if keywords_token else []

            resp = await client.request(
                "RegisterItemForSale",
                {
                    "item_name": item_name,
                    "item_category": item_category,
                    "keywords": keywords,
                    "condition": condition,
                    "sale_price": sale_price,
                    "item_quantity": item_quantity,
                },
            )

        elif cmd == "change_item_price" and len(args) == 2:
            resp = await client.request("ChangeItemPrice", {"item_id": args[0], "new_price": float(args[1])})

        elif cmd == "update_units_for_sale" and len(args) == 2:
            resp = await client.request("UpdateUnitsForSale", {"item_id": args[0], "quantity": int(args[1])})

        elif cmd == "display_items_for_sale" and len(args) == 0:
            resp = await client.request("DisplayItemsForSale", {})

        elif cmd == "ping":
            resp = await client.request("ping", {})

        else:
            print("Unknown command or invalid args. Type 'help'.")
            continue

        print(resp)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    async with MarketplaceClient(cfg.frontend_seller.host, cfg.frontend_seller.port, role="seller") as client:
        await repl(client)


if __name__ == "__main__":
    asyncio.run(main())
