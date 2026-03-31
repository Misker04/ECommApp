from __future__ import annotations

import argparse
import shlex

from src.clients.client_base import MarketplaceClient
from src.clients.runtime_config import frontend_base_urls


HELP = """Buyer CLI commands:
  create_account <buyer_name> <password>
  login <buyer_name> <password>
  logout

  search <item_category>
  get_item <item_id>              # format: 2:1
  add_item_to_cart <item_id> <quantity>
  remove_item_from_cart <item_id> <quantity>
  clear_cart
  display_cart
  save_cart

  provide_feedback <item_id> <up|down>
  get_seller_rating <seller_id>
  get_purchases
  make_purchase

  help
  quit
"""


def repl(client: MarketplaceClient) -> None:
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

        try:

            # -------------------------
            # Account
            # -------------------------

            if cmd == "create_account" and len(args) == 2:
                resp = client._post(
                    "buyer/create_account",
                    {"username": args[0], "password": args[1]}
                )
                print(resp)

            elif cmd == "login" and len(args) == 2:
                resp = client._post(
                    "buyer/login",
                    {"username": args[0], "password": args[1]}
                )

                # FIX: server returns "session_token", not "session_id"
                if "session_token" in resp:
                    client.set_session(resp["session_token"])

                print(resp)

            elif cmd == "logout":
                resp = client._post(
                    "buyer/logout",
                    {"session_token": client.session_id}
                )
                client.clear_session()
                print(resp)

            # -------------------------
            # Product Search
            # -------------------------

            elif cmd == "search" and len(args) == 1:
                resp = client._post(
                    "buyer/search",
                    {
                        "item_category": int(args[0]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "get_item" and len(args) == 1:
                resp = client._post(
                    "buyer/get_item",
                    {
                        "item_id": args[0],
                        "session_token": client.session_id
                    }
                )
                print(resp)

            # -------------------------
            # Cart
            # -------------------------

            elif cmd == "add_item_to_cart" and len(args) == 2:
                resp = client._post(
                    "buyer/add_to_cart",
                    {
                        "item_id": args[0],
                        "quantity": int(args[1]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "remove_item_from_cart" and len(args) == 2:
                resp = client._post(
                    "buyer/remove_from_cart",
                    {
                        "item_id": args[0],
                        "quantity": int(args[1]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "clear_cart":
                resp = client._post(
                    "buyer/clear_cart",
                    {"session_token": client.session_id}
                )
                print(resp)

            elif cmd == "display_cart":
                resp = client._post(
                    "buyer/display_cart",
                    {"session_token": client.session_id}
                )
                print(resp)

            elif cmd == "save_cart":
                resp = client._post(
                    "buyer/save_cart",
                    {"session_token": client.session_id}
                )
                print(resp)

            # -------------------------
            # Feedback & Ratings
            # -------------------------

            elif cmd == "provide_feedback" and len(args) == 2:
                vote = args[1].lower()
                if vote not in ("up", "down"):
                    print("Feedback must be 'up' or 'down'.")
                    continue
                resp = client._post(
                    "buyer/provide_feedback",
                    {
                        "item_id": args[0],
                        "feedback": vote,
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "get_seller_rating" and len(args) == 1:
                resp = client._post(
                    "buyer/get_seller_rating",
                    {
                        "seller_id": int(args[0]),
                        "session_token": client.session_id
                    }
                )
                print(resp)

            elif cmd == "get_purchases":
                resp = client._post(
                    "buyer/get_purchases",
                    {"session_token": client.session_id}
                )
                print(resp)

            # -------------------------
            # Purchase (PA2)
            # Prompts user for real card info
            # -------------------------

            elif cmd == "make_purchase":
                print("Enter payment details:")
                card_name = input("  Cardholder name: ").strip()
                card_number = input("  Card number: ").strip()
                expiration = input("  Expiration (MM/YY): ").strip()
                security_code = input("  Security code: ").strip()

                resp = client._post(
                    "buyer/make_purchase",
                    {
                        "session_token": client.session_id,
                        "card_name": card_name,
                        "card_number": card_number,
                        "expiration": expiration,
                        "security_code": security_code
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

    client = MarketplaceClient(frontend_base_urls(args.config, "buyer"))
    repl(client)


if __name__ == "__main__":
    main()
