from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.common.config import load_config
from src.clients.client_base import MarketplaceClient

import secrets
RUN_TAG = secrets.token_hex(3)

# ----------------------------
# Benchmark / Evaluation runner
# ----------------------------
#
# - each run: each client invokes 1000 API functions
# - scenarios: (1,1), (10,10), (100,100) sellers/buyers
#


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


async def timed_call(client: MarketplaceClient, action: str, payload: Dict[str, Any], stats: Stats) -> Dict[str, Any]:
    t0 = time.perf_counter()
    resp = await client.request(action, payload)
    t1 = time.perf_counter()
    stats.add(t1 - t0, bool(resp.get("ok")))
    return resp

def _mk_kw(rng: random.Random) -> List[str]:
    # matches setup keywords: k1..k5
    return [f"k{rng.randint(1, 5)}"]


def _mk_item_id_key(item_id: Any) -> Any:
    # item_id is usually {"category": int, "number": int}
    # Keep as-is (JSON-friendly) for requests.
    return item_id


async def seller_workload(
    host: str,
    port: int,
    seller_idx: int,
    run_idx: int,
    ops_per_client: int,
    items_per_seller: int,
    rng_seed: int,
    shared_item_ids: List[Dict[str, Any]],
    shared_item_ids_lock: asyncio.Lock,
) -> Tuple[Stats, List[Dict[str, Any]]]:
    """
    Each seller performs exactly ops_per_client API calls:
      1) CreateAccount
      2) Login
      3) RegisterItemForSale (items_per_seller times)
      4) Remaining ops: DisplayItemsForSale / ChangeItemPrice / UpdateUnitsForSale
      5) Logout (last call)
    """
    rng = random.Random(rng_seed)
    stats = Stats()
    created_item_ids: List[Dict[str, Any]] = []

    c = MarketplaceClient(host, port, role="seller")
    await c.connect()
    try:
        username = f"seller_{RUN_TAG}_r{run_idx}_{seller_idx}"
        await timed_call(c, "CreateAccount", {"username": username, "password": "pw"}, stats)
        r = await timed_call(c, "Login", {"username": username, "password": "pw"}, stats)
        c.session_token = r.get("data", {}).get("session_token")

        # Register a few items with large inventory to reduce "unavailable" errors in big scenarios.
        for j in range(items_per_seller):
            category = rng.randint(1, 3)
            name = f"item_s{seller_idx}_r{run_idx}_{j}"
            cond = "new" if (j % 2 == 0) else "used"
            price = float(rng.randint(10, 100))
            qty = rng.randint(5000, 12000)  # large so add-to-cart stays available
            rr = await timed_call(
                c,
                "RegisterItemForSale",
                {
                    "item_name": name,
                    "item_category": category,
                    "keywords": _mk_kw(rng),
                    "condition": cond,
                    "sale_price": price,
                    "item_quantity": qty,
                },
                stats,
            )
            if rr.get("ok") and rr.get("data", {}).get("item_id"):
                iid = rr["data"]["item_id"]
                created_item_ids.append(iid)

        # Publish created items to shared pool for buyers.
        if created_item_ids:
            async with shared_item_ids_lock:
                shared_item_ids.extend(created_item_ids)

        # avoid draining inventory too fast with UpdateUnitsForSale
        used_ops = 2 + items_per_seller
        remaining = ops_per_client - used_ops - 1  # reserve 1 for Logout
        for _ in range(max(0, remaining)):
            p = rng.random()
            if p < 0.65:
                await timed_call(c, "DisplayItemsForSale", {}, stats)
            elif p < 0.95 and created_item_ids:
                iid = _mk_item_id_key(rng.choice(created_item_ids))
                new_price = float(rng.randint(5, 200))
                await timed_call(c, "ChangeItemPrice", {"item_id": iid, "sale_price": new_price}, stats)
            else:
                # low-frequency inventory reduction
                if created_item_ids:
                    iid = _mk_item_id_key(rng.choice(created_item_ids))
                    await timed_call(c, "UpdateUnitsForSale", {"item_id": iid, "remove_quantity": 1}, stats)
                else:
                    await timed_call(c, "DisplayItemsForSale", {}, stats)

        await timed_call(c, "Logout", {}, stats)
    finally:
        await c.close()

    return stats, created_item_ids


async def buyer_workload(
    host: str,
    port: int,
    buyer_idx: int,
    run_idx: int,
    ops_per_client: int,
    rng_seed: int,
    shared_item_ids: List[Dict[str, Any]],
) -> Stats:
    """
    Each buyer performs exactly ops_per_client API calls:
      1) CreateAccount
      2) Login
      3) Remaining ops: Search / GetItem / AddItemToCart / RemoveItemFromCart / DisplayCart /
                        SaveCart / ClearCart / ProvideFeedback / GetSellerRating
      4) Logout (last call)
    """
    rng = random.Random(rng_seed)
    stats = Stats()

    c = MarketplaceClient(host, port, role="buyer")
    await c.connect()
    try:
        username = f"buyer_{RUN_TAG}_r{run_idx}_{buyer_idx}"
        await timed_call(c, "CreateAccount", {"username": username, "password": "pw"}, stats)
        r = await timed_call(c, "Login", {"username": username, "password": "pw"}, stats)
        c.session_token = r.get("data", {}).get("session_token")

        used_ops = 2
        remaining = ops_per_client - used_ops - 1  # reserve 1 for Logout

        for _ in range(max(0, remaining)):
            p = rng.random()

            # pick an item (best-effort). Buyers can run before sellers have published items;
            # in that case, fall back to Search/DisplayCart.
            iid: Optional[Dict[str, Any]] = rng.choice(shared_item_ids) if shared_item_ids else None

            if p < 0.30:
                cat = rng.randint(1, 3)
                await timed_call(c, "SearchItemsForSale", {"item_category": cat, "keywords": _mk_kw(rng)}, stats)

            elif p < 0.50 and iid:
                await timed_call(c, "GetItem", {"item_id": _mk_item_id_key(iid)}, stats)

            elif p < 0.70 and iid:
                await timed_call(c, "AddItemToCart", {"item_id": _mk_item_id_key(iid), "quantity": 1}, stats)

            elif p < 0.80 and iid:
                await timed_call(c, "RemoveItemFromCart", {"item_id": _mk_item_id_key(iid), "quantity": 1}, stats)

            elif p < 0.88:
                await timed_call(c, "DisplayCart", {}, stats)

            elif p < 0.93:
                await timed_call(c, "SaveCart", {}, stats)

            elif p < 0.96:
                await timed_call(c, "ClearCart", {}, stats)

            elif p < 0.99 and iid:
                vote = "up" if rng.random() < 0.7 else "down"
                await timed_call(c, "ProvideFeedback", {"item_id": _mk_item_id_key(iid), "vote": vote}, stats)

            else:
                await timed_call(c, "SearchItemsForSale", {"item_category": rng.randint(1, 3), "keywords": []}, stats)

        await timed_call(c, "Logout", {}, stats)
    finally:
        await c.close()

    return stats


async def run_one(
    buyers_host: str,
    buyers_port: int,
    sellers_host: str,
    sellers_port: int,
    n_buyers: int,
    n_sellers: int,
    ops_per_client: int,
    items_per_seller: int,
    seed: int,
    run_idx: int,
) -> Tuple[float, float, Stats]:
    """
    Returns: (avg_response_time_seconds, throughput_ops_per_sec, aggregated_stats)
    """
    # shared pool of item ids produced by sellers for buyers to touch
    shared_item_ids: List[Dict[str, Any]] = []
    lock = asyncio.Lock()

    # Start sellers and buyers concurrently to simulate real load.
    # Buyers do not require sellers to finish; search will return whatever exists.
    seller_tasks = [
        seller_workload(
            sellers_host,
            sellers_port,
            seller_idx=i,
            run_idx=run_idx,
            ops_per_client=ops_per_client,
            items_per_seller=items_per_seller,
            rng_seed=seed + 1000 + i,
            shared_item_ids=shared_item_ids,
            shared_item_ids_lock=lock,
        )
        for i in range(n_sellers)
    ]
    buyer_tasks = [
        buyer_workload(
            buyers_host,
            buyers_port,
            buyer_idx=i,
            run_idx=run_idx,
            ops_per_client=ops_per_client,
            rng_seed=seed + 2000 + i,
            shared_item_ids=shared_item_ids,
        )
        for i in range(n_buyers)
    ]

    t0 = time.perf_counter()
    seller_results = await asyncio.gather(*seller_tasks)
    buyer_stats = await asyncio.gather(*buyer_tasks)
    t1 = time.perf_counter()

    # Aggregate
    all_stats = Stats()
    for st, _created in seller_results:
        all_stats.latencies.extend(st.latencies)
        all_stats.ok += st.ok
        all_stats.err += st.err
    for st in buyer_stats:
        all_stats.latencies.extend(st.latencies)
        all_stats.ok += st.ok
        all_stats.err += st.err

    duration = t1 - t0
    total_ops = (n_buyers + n_sellers) * ops_per_client
    throughput = (total_ops / duration) if duration > 0 else 0.0

    # avg response time for this run = mean across all calls in this run
    avg_resp = all_stats.avg

    return avg_resp, throughput, all_stats


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

    # scenario mapping
    if args.scenario == 1:
        n_sellers, n_buyers = 1, 1
    elif args.scenario == 2:
        n_sellers, n_buyers = 10, 10
    else:
        n_sellers, n_buyers = 100, 100

    cfg = load_config(args.config)

    async def run_all():
        for w in range(args.warmup):
            await run_one(
                cfg.frontend_buyer.host,
                cfg.frontend_buyer.port,
                cfg.frontend_seller.host,
                cfg.frontend_seller.port,
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
            avg_resp, throughput, st = await run_one(
                cfg.frontend_buyer.host,
                cfg.frontend_buyer.port,
                cfg.frontend_seller.host,
                cfg.frontend_seller.port,
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

        print("\n=== A1 Report Numbers ===")
        print(f"scenario={args.scenario} sellers={n_sellers} buyers={n_buyers}")
        print(f"average_response_time_over_{args.runs}_runs={avg_of_avgs:.6f}s")
        print(f"average_throughput_over_{args.runs}_runs={avg_throughput:.2f} ops/s")

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
