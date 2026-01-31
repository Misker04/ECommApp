from __future__ import annotations

import argparse
import asyncio
import shlex

from src.common.config import load_config
from src.clients.client_base import MarketplaceClient

HELP = """Buyer CLI commands:
  create_account <buyer_name> <password>
  login <buyer_name_or_buyer_id> <password>
  logout

  search <item_category> [kw1,kw2,kw3]
  get_item <item_id>                         # item_id like "2:1"
  add_item_to_cart <item_id> <quantity>
  remove_item_from_cart <item_id> <quantity>
  save_cart
  clear_cart
  display_cart

  provide_feedback <item_id> <up|down>
  get_seller_rating <seller_id>
  get_buyer_purchases
  make_purchase                              # not implemented

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
        line = input("buyer> ").strip()
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
            # Allow using name OR numeric ID as the username field.
            maybe_id = _maybe_int(args[0])
            username = str(maybe_id) if maybe_id is not None else args[0]
            resp = await client.request("Login", {"username": username, "password": args[1]})
            if resp.get("ok") and resp.get("data", {}).get("session_token"):
                client.session_token = resp["data"]["session_token"]

        elif cmd == "logout" and len(args) == 0:
            resp = await client.request("Logout", {})
            client.session_token = None

        elif cmd == "search" and len(args) in {1, 2}:
            category = int(args[0])
            payload = {"item_category": category}
            if len(args) == 2:
                payload["keywords"] = args[1]
            resp = await client.request("SearchItemsForSale", payload)

        elif cmd == "get_item" and len(args) == 1:
            resp = await client.request("GetItem", {"item_id": args[0]})

        elif cmd == "add_item_to_cart" and len(args) == 2:
            resp = await client.request("AddItemToCart", {"item_id": args[0], "quantity": int(args[1])})

        elif cmd == "remove_item_from_cart" and len(args) == 2:
            resp = await client.request("RemoveItemFromCart", {"item_id": args[0], "quantity": int(args[1])})

        elif cmd == "save_cart" and len(args) == 0:
            resp = await client.request("SaveCart", {})

        elif cmd == "clear_cart" and len(args) == 0:
            resp = await client.request("ClearCart", {})

        elif cmd == "display_cart" and len(args) == 0:
            resp = await client.request("DisplayCart", {})

        elif cmd == "provide_feedback" and len(args) == 2:
            vote = args[1].strip().lower()
            resp = await client.request("ProvideFeedback", {"item_id": args[0], "vote": vote})

        elif cmd == "get_seller_rating" and len(args) == 1:
            resp = await client.request("GetSellerRating", {"seller_id": int(args[0])})

        elif cmd == "get_buyer_purchases" and len(args) == 0:
            resp = await client.request("GetBuyerPurchases", {})

        elif cmd == "make_purchase" and len(args) == 0:
            resp = await client.request("MakePurchase", {})

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
    host = cfg.frontend_buyer.host
    port = cfg.frontend_buyer.port

    client = MarketplaceClient(host, port, role="buyer")
    await client.connect()
    try:
        await repl(client)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
