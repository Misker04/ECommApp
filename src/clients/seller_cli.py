from __future__ import annotations

import argparse
import shlex

from src.common.config import load_config
from src.clients.client_base import MarketplaceClient


HELP = """Seller CLI commands:
  create_account <seller_name> <password>
  login <seller_name> <password>
  logout

  register_item <name> <category> <price> <quantity>
  change_price <item_id> <new_price>      # item_id format: 2:1
  update_quantity <item_id> <quantity>
  display_items
  get_rating <seller_id>

  help
  quit
"""


def repl(client: MarketplaceClient) -> None:
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

        try:

            # -------------------------
            # Account
            # -------------------------

            if cmd == "create_account" and len(args) == 2:
                resp = client._post(
                    "seller/create_account",
                    {"username": args[0], "password": args[1]}
                )
                print(resp)

            elif cmd == "login" and len(args) == 2:
                resp = client._post(
                    "seller/login",
                    {"username": args[0], "password": args[1]}
                )

                # FIX: server returns "session_token", not "session_id"
                if "session_token" in resp:
                    client.set_session(resp["session_token"])

                print(resp)

            elif cmd == "logout":
                resp = client._post(
                    "seller/logout",
                    {"session_token": client.session_id}
                )
                client.clear_session()
                print(resp)

            # -------------------------
            # Product Management
            # -------------------------

            elif cmd == "register_item" and len(args) == 4:
                resp = client._post(
                    "seller/register_item",
                    {
                        "name": args[0],
                        "category": int(args[1]),
                        "price": float(args[2]),
                        "quantity": int(args[3]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "change_price" and len(args) == 2:
                resp = client._post(
                    "seller/change_price",
                    {
                        "item_id": args[0],
                        "new_price": float(args[1]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "update_quantity" and len(args) == 2:
                resp = client._post(
                    "seller/update_quantity",
                    {
                        "item_id": args[0],
                        "quantity": int(args[1]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "display_items":
                resp = client._post(
                    "seller/display_items",
                    {"session_token": client.session_id}
                )
                print(resp)

            elif cmd == "get_rating" and len(args) == 1:
                # FIX: endpoint is "seller/get_rating", and seller_id is required
                resp = client._post(
                    "seller/get_rating",
                    {
                        "session_token": client.session_id,
                        "seller_id": int(args[0])
                    }
                )
                print(resp)

            else:
                print("Unknown command or invalid args. Type 'help' for usage.")

        except Exception as e:
            print("Error:", e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    base_urls = [
        f"http://{replica.host}:{replica.port}"
        for replica in cfg.frontend_seller.targets()
    ]

    client = MarketplaceClient(base_urls)
    repl(client)


if __name__ == "__main__":
    main()
