from __future__ import annotations

import argparse
import random
import statistics
import time
import secrets
import concurrent.futures
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.clients.client_base import MarketplaceClient
from src.clients.runtime_config import frontend_base_urls

RUN_TAG = secrets.token_hex(3)

# ----------------------------
# Benchmark / Evaluation runner (PA2 - REST version)
# ----------------------------
#
# - each run: each client invokes 1000 API functions
# - scenarios: (1,1), (10,10), (100,100) sellers/buyers
#
# Seller API coverage:
#   CreateAccount, Login, Logout,
#   RegisterItemForSale, ChangeItemPrice, UpdateUnitsForSale,
#   DisplayItemsForSale, GetSellerRating
#
# Buyer API coverage:
#   CreateAccount, Login, Logout,
#   SearchItemsForSale, GetItem, AddItemToCart, RemoveItemFromCart,
#   DisplayCart, SaveCart, ClearCart,
#   ProvideFeedback, GetSellerRating, GetBuyerPurchases,
#   MakePurchase


@dataclass
class Stats:
    latencies: List[float] = field(default_factory=list)
    ok: int = 0
    err: int = 0

    def add(self, dt: float, ok: bool) -> None:
        self.latencies.append(dt)
        if ok:
            self.ok += 1
        else:
            self.err += 1

    @property
    def count(self) -> int:
        return len(self.latencies)

    @property
    def avg(self) -> float:
        return statistics.fmean(self.latencies) if self.latencies else 0.0

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0.0
        xs = sorted(self.latencies)
        k = int(0.95 * (len(xs) - 1))
        return xs[k]


# --------------------------------------------------
# Timed REST call helper
# --------------------------------------------------

def timed_call(
    client: MarketplaceClient,
    endpoint: str,
    payload: Dict[str, Any],
    stats: Stats,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        resp = client._post(endpoint, payload)
        ok = True
    except Exception:
        resp = {}
        ok = False
    t1 = time.perf_counter()
    stats.add(t1 - t0, ok)
    return resp


def _mk_kw(rng: random.Random) -> List[str]:
    return [f"k{rng.randint(1, 5)}"]


# --------------------------------------------------
# Seller workload
# Full API: CreateAccount, Login, RegisterItem, ChangePrice,
#           UpdateQuantity, DisplayItems, GetSellerRating, Logout
# --------------------------------------------------

def seller_workload(
    base_url: str,
    seller_idx: int,
    run_idx: int,
    ops_per_client: int,
    items_per_seller: int,
    rng_seed: int,
    shared_item_ids: List[str],
    shared_seller_ids: List[int],
    shared_lock: threading.Lock,
) -> Tuple[Stats, List[str]]:

    rng = random.Random(rng_seed)
    stats = Stats()
    created_item_ids: List[str] = []

    client = MarketplaceClient(base_url)
    username = f"seller_{RUN_TAG}_r{run_idx}_{seller_idx}"

    # 1) CreateAccount
    ca_resp = timed_call(
        client, "seller/create_account",
        {"username": username, "password": "pw"},
        stats,
    )

    # 2) Login
    resp = timed_call(
        client, "seller/login",
        {"username": username, "password": "pw"},
        stats,
    )
    session_token = resp.get("session_token", "")
    seller_id = resp.get("seller_id")
    client.set_session(session_token)

    # Publish seller_id to shared pool for buyers to use GetSellerRating
    if seller_id:
        with shared_lock:
            shared_seller_ids.append(int(seller_id))

    # 3) RegisterItemForSale
    for j in range(items_per_seller):
        category = rng.randint(1, 3)
        name = f"item_s{seller_idx}_r{run_idx}_{j}"
        price = float(rng.randint(10, 100))
        qty = rng.randint(5000, 12000)
        rr = timed_call(
            client, "seller/register_item",
            {
                "name": name,
                "category": category,
                "price": price,
                "quantity": qty,
                "session_token": session_token,
            },
            stats,
        )
        item_id = rr.get("item_id")
        if item_id:
            created_item_ids.append(item_id)

    # Publish item_ids to shared pool for buyers
    if created_item_ids:
        with shared_lock:
            shared_item_ids.extend(created_item_ids)

    # 4) Remaining ops — weighted mix of all seller APIs
    used_ops = 2 + items_per_seller
    remaining = ops_per_client - used_ops - 1  # reserve 1 for Logout

    for _ in range(max(0, remaining)):
        p = rng.random()

        if p < 0.50:
            # DisplayItemsForSale
            timed_call(
                client, "seller/display_items",
                {"session_token": session_token},
                stats,
            )

        elif p < 0.75 and created_item_ids:
            # ChangeItemPrice
            iid = rng.choice(created_item_ids)
            new_price = float(rng.randint(5, 200))
            timed_call(
                client, "seller/change_price",
                {"item_id": iid, "new_price": new_price, "session_token": session_token},
                stats,
            )

        elif p < 0.90 and created_item_ids:
            # UpdateUnitsForSale
            iid = rng.choice(created_item_ids)
            timed_call(
                client, "seller/update_quantity",
                {"item_id": iid, "quantity": 1, "session_token": session_token},
                stats,
            )

        else:
            # GetSellerRating (seller checks their own rating)
            if seller_id:
                timed_call(
                    client, "seller/get_rating",
                    {"session_token": session_token, "seller_id": int(seller_id)},
                    stats,
                )
            else:
                timed_call(
                    client, "seller/display_items",
                    {"session_token": session_token},
                    stats,
                )

    # 5) Logout
    timed_call(
        client, "seller/logout",
        {"session_token": session_token},
        stats,
    )

    return stats, created_item_ids


# --------------------------------------------------
# Buyer workload
# Full API: CreateAccount, Login, Logout,
#           SearchItemsForSale, GetItem, AddItemToCart,
#           RemoveItemFromCart, DisplayCart, SaveCart, ClearCart,
#           ProvideFeedback, GetSellerRating, GetBuyerPurchases,
#           MakePurchase
# --------------------------------------------------

def buyer_workload(
    base_url: str,
    buyer_idx: int,
    run_idx: int,
    ops_per_client: int,
    rng_seed: int,
    shared_item_ids: List[str],
    shared_seller_ids: List[int],
) -> Stats:

    rng = random.Random(rng_seed)
    stats = Stats()

    client = MarketplaceClient(base_url)
    username = f"buyer_{RUN_TAG}_r{run_idx}_{buyer_idx}"

    # 1) CreateAccount
    timed_call(
        client, "buyer/create_account",
        {"username": username, "password": "pw"},
        stats,
    )

    # 2) Login
    resp = timed_call(
        client, "buyer/login",
        {"username": username, "password": "pw"},
        stats,
    )
    session_token = resp.get("session_token", "")
    client.set_session(session_token)

    # 3) Remaining ops — weighted mix of ALL buyer APIs
    used_ops = 2
    remaining = ops_per_client - used_ops - 1  # reserve 1 for Logout

    # Track whether we've added anything to cart (for MakePurchase to work)
    cart_has_items = False

    for _ in range(max(0, remaining)):
        p = rng.random()
        iid: Optional[str] = rng.choice(shared_item_ids) if shared_item_ids else None
        sid: Optional[int] = rng.choice(shared_seller_ids) if shared_seller_ids else None

        if p < 0.22:
            # SearchItemsForSale
            cat = rng.randint(1, 3)
            timed_call(
                client, "buyer/search",
                {"item_category": cat, "session_token": session_token},
                stats,
            )

        elif p < 0.35 and iid:
            # GetItem
            timed_call(
                client, "buyer/get_item",
                {"item_id": iid, "session_token": session_token},
                stats,
            )

        elif p < 0.50 and iid:
            # AddItemToCart
            timed_call(
                client, "buyer/add_to_cart",
                {"item_id": iid, "quantity": 1, "session_token": session_token},
                stats,
            )
            cart_has_items = True

        elif p < 0.60 and iid:
            # RemoveItemFromCart
            timed_call(
                client, "buyer/remove_from_cart",
                {"item_id": iid, "quantity": 1, "session_token": session_token},
                stats,
            )

        elif p < 0.68:
            # DisplayCart
            timed_call(
                client, "buyer/display_cart",
                {"session_token": session_token},
                stats,
            )

        elif p < 0.74:
            # SaveCart
            timed_call(
                client, "buyer/save_cart",
                {"session_token": session_token},
                stats,
            )

        elif p < 0.79:
            # ClearCart
            timed_call(
                client, "buyer/clear_cart",
                {"session_token": session_token},
                stats,
            )
            cart_has_items = False

        elif p < 0.86 and iid:
            # ProvideFeedback
            vote = "up" if rng.random() < 0.7 else "down"
            timed_call(
                client, "buyer/provide_feedback",
                {"item_id": iid, "feedback": vote, "session_token": session_token},
                stats,
            )

        elif p < 0.91 and sid:
            # GetSellerRating
            timed_call(
                client, "buyer/get_seller_rating",
                {"seller_id": sid, "session_token": session_token},
                stats,
            )

        elif p < 0.95:
            # GetBuyerPurchases
            timed_call(
                client, "buyer/get_purchases",
                {"session_token": session_token},
                stats,
            )

        else:
            # MakePurchase (PA2) — only if cart likely has items
            # Uses dummy card; SOAP will approve ~90% of the time
            if cart_has_items and iid:
                # Ensure something is in cart before purchasing
                timed_call(
                    client, "buyer/add_to_cart",
                    {"item_id": iid, "quantity": 1, "session_token": session_token},
                    stats,
                )
                timed_call(
                    client, "buyer/make_purchase",
                    {
                        "session_token": session_token,
                        "card_name": "Bench User",
                        "card_number": "4111111111111111",
                        "expiration": "12/30",
                        "security_code": "123",
                    },
                    stats,
                )
                cart_has_items = False
            else:
                # Fallback to search if cart is empty
                timed_call(
                    client, "buyer/search",
                    {"item_category": rng.randint(1, 3), "session_token": session_token},
                    stats,
                )

    # 4) Logout
    timed_call(
        client, "buyer/logout",
        {"session_token": session_token},
        stats,
    )

    return stats


# --------------------------------------------------
# Run one benchmark round
# --------------------------------------------------

def run_one(
    buyer_base_url: str,
    seller_base_url: str,
    n_buyers: int,
    n_sellers: int,
    ops_per_client: int,
    items_per_seller: int,
    seed: int,
    run_idx: int,
) -> Tuple[float, float, Stats]:

    shared_item_ids: List[str] = []
    shared_seller_ids: List[int] = []
    shared_lock = threading.Lock()

    seller_futures = []
    buyer_futures = []

    t0 = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_sellers + n_buyers) as executor:

        for i in range(n_sellers):
            f = executor.submit(
                seller_workload,
                seller_base_url,
                i,
                run_idx,
                ops_per_client,
                items_per_seller,
                seed + 1000 + i,
                shared_item_ids,
                shared_seller_ids,
                shared_lock,
            )
            seller_futures.append(f)

        for i in range(n_buyers):
            f = executor.submit(
                buyer_workload,
                buyer_base_url,
                i,
                run_idx,
                ops_per_client,
                seed + 2000 + i,
                shared_item_ids,
                shared_seller_ids,
            )
            buyer_futures.append(f)

    t1 = time.perf_counter()

    # Aggregate
    all_stats = Stats()
    for f in seller_futures:
        st, _ = f.result()
        all_stats.latencies.extend(st.latencies)
        all_stats.ok += st.ok
        all_stats.err += st.err
    for f in buyer_futures:
        st = f.result()
        all_stats.latencies.extend(st.latencies)
        all_stats.ok += st.ok
        all_stats.err += st.err

    duration = t1 - t0
    total_ops = (n_buyers + n_sellers) * ops_per_client
    throughput = (total_ops / duration) if duration > 0 else 0.0
    avg_resp = all_stats.avg

    return avg_resp, throughput, all_stats


# --------------------------------------------------
# Main
# --------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--scenario", type=int, choices=[1, 2, 3], default=1)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--ops_per_client", type=int, default=1000)
    ap.add_argument("--items_per_seller", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--warmup", type=int, default=1, help="number of warmup runs (not counted)")
    args = ap.parse_args()

    if args.scenario == 1:
        n_sellers, n_buyers = 1, 1
    elif args.scenario == 2:
        n_sellers, n_buyers = 10, 10
    else:
        n_sellers, n_buyers = 100, 100

    buyer_base_url = frontend_base_urls(args.config, "buyer")
    seller_base_url = frontend_base_urls(args.config, "seller")

    # Warmup runs (not counted in results)
    for w in range(args.warmup):
        run_one(
            buyer_base_url,
            seller_base_url,
            n_buyers=n_buyers,
            n_sellers=n_sellers,
            ops_per_client=min(200, args.ops_per_client),
            items_per_seller=max(1, min(2, args.items_per_seller)),
            seed=args.seed + 9999 + w,
            run_idx=-(w + 1),
        )

    run_avgs: List[float] = []
    run_throughputs: List[float] = []

    for r in range(args.runs):
        avg_resp, throughput, st = run_one(
            buyer_base_url,
            seller_base_url,
            n_buyers=n_buyers,
            n_sellers=n_sellers,
            ops_per_client=args.ops_per_client,
            items_per_seller=args.items_per_seller,
            seed=args.seed + r * 17,
            run_idx=r,
        )
        run_avgs.append(avg_resp)
        run_throughputs.append(throughput)
        print(
            f"run {r+1}/{args.runs}: avg_resp={avg_resp:.6f}s "
            f"p50={st.p50:.6f}s p95={st.p95:.6f}s "
            f"throughput={throughput:.2f} ops/s"
        )

    avg_of_avgs = statistics.fmean(run_avgs) if run_avgs else 0.0
    avg_throughput = statistics.fmean(run_throughputs) if run_throughputs else 0.0

    print("\n=== PA3 Report Numbers ===")
    print(f"scenario={args.scenario} sellers={n_sellers} buyers={n_buyers}")
    print(f"average_response_time_over_{args.runs}_runs={avg_of_avgs:.6f}s")
    print(f"average_throughput_over_{args.runs}_runs={avg_throughput:.2f} ops/s")


if __name__ == "__main__":
    main()
