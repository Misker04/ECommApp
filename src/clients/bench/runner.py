from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import secrets
import signal
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.clients.client_base import MarketplaceClient
from src.common.config import load_config

RUN_TAG = secrets.token_hex(3)


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

    def merge(self, other: "Stats") -> None:
        self.latencies.extend(other.latencies)
        self.ok += other.ok
        self.err += other.err

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


@dataclass
class RunStats:
    total: Stats = field(default_factory=Stats)
    per_endpoint: Dict[str, Stats] = field(default_factory=dict)

    def add(self, endpoint: str, dt: float, ok: bool) -> None:
        self.total.add(dt, ok)
        self.per_endpoint.setdefault(endpoint, Stats()).add(dt, ok)

    def merge(self, other: "RunStats") -> None:
        self.total.merge(other.total)
        for endpoint, stats in other.per_endpoint.items():
            self.per_endpoint.setdefault(endpoint, Stats()).merge(stats)


@dataclass
class ScenarioReport:
    avg_response_time: float
    throughput: float
    stats: RunStats


def timed_call(
    client: MarketplaceClient,
    endpoint: str,
    payload: Dict[str, Any],
    stats: RunStats,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        resp = client._post(endpoint, payload)
        ok = True
    except Exception:
        resp = {}
        ok = False
    dt = time.perf_counter() - t0
    stats.add(endpoint, dt, ok)
    return resp


def seller_workload(
    base_url: str | list[str],
    seller_idx: int,
    run_idx: int,
    ops_per_client: int,
    items_per_seller: int,
    rng_seed: int,
    shared_item_ids: List[str],
    shared_seller_ids: List[int],
    shared_lock: threading.Lock,
) -> Tuple[RunStats, List[str]]:
    rng = random.Random(rng_seed)
    stats = RunStats()
    created_item_ids: List[str] = []

    client = MarketplaceClient(base_url)
    username = f"seller_{RUN_TAG}_r{run_idx}_{seller_idx}"

    timed_call(client, "seller/create_account", {"username": username, "password": "pw"}, stats)

    resp = timed_call(client, "seller/login", {"username": username, "password": "pw"}, stats)
    session_token = resp.get("session_token", "")
    seller_id = resp.get("seller_id")
    client.set_session(session_token)

    if seller_id:
        with shared_lock:
            shared_seller_ids.append(int(seller_id))

    for j in range(items_per_seller):
        rr = timed_call(
            client,
            "seller/register_item",
            {
                "name": f"item_s{seller_idx}_r{run_idx}_{j}",
                "category": rng.randint(1, 3),
                "price": float(rng.randint(10, 100)),
                "quantity": rng.randint(5000, 12000),
                "session_token": session_token,
            },
            stats,
        )
        item_id = rr.get("item_id")
        if item_id:
            created_item_ids.append(item_id)

    if created_item_ids:
        with shared_lock:
            shared_item_ids.extend(created_item_ids)

    remaining = ops_per_client - (2 + items_per_seller) - 1
    for _ in range(max(0, remaining)):
        p = rng.random()
        if p < 0.50:
            timed_call(client, "seller/display_items", {"session_token": session_token}, stats)
        elif p < 0.75 and created_item_ids:
            timed_call(
                client,
                "seller/change_price",
                {
                    "item_id": rng.choice(created_item_ids),
                    "new_price": float(rng.randint(5, 200)),
                    "session_token": session_token,
                },
                stats,
            )
        elif p < 0.90 and created_item_ids:
            timed_call(
                client,
                "seller/update_quantity",
                {"item_id": rng.choice(created_item_ids), "quantity": 1, "session_token": session_token},
                stats,
            )
        else:
            endpoint = "seller/get_rating" if seller_id else "seller/display_items"
            payload = {"session_token": session_token, "seller_id": int(seller_id)} if seller_id else {"session_token": session_token}
            timed_call(client, endpoint, payload, stats)

    timed_call(client, "seller/logout", {"session_token": session_token}, stats)
    return stats, created_item_ids


def buyer_workload(
    base_url: str | list[str],
    buyer_idx: int,
    run_idx: int,
    ops_per_client: int,
    rng_seed: int,
    shared_item_ids: List[str],
    shared_seller_ids: List[int],
) -> RunStats:
    rng = random.Random(rng_seed)
    stats = RunStats()
    client = MarketplaceClient(base_url)
    username = f"buyer_{RUN_TAG}_r{run_idx}_{buyer_idx}"

    timed_call(client, "buyer/create_account", {"username": username, "password": "pw"}, stats)
    resp = timed_call(client, "buyer/login", {"username": username, "password": "pw"}, stats)
    session_token = resp.get("session_token", "")
    client.set_session(session_token)

    remaining = ops_per_client - 2 - 1
    cart_has_items = False

    for _ in range(max(0, remaining)):
        p = rng.random()
        iid = rng.choice(shared_item_ids) if shared_item_ids else None
        sid = rng.choice(shared_seller_ids) if shared_seller_ids else None

        if p < 0.22:
            timed_call(client, "buyer/search", {"item_category": rng.randint(1, 3), "session_token": session_token}, stats)
        elif p < 0.35 and iid:
            timed_call(client, "buyer/get_item", {"item_id": iid, "session_token": session_token}, stats)
        elif p < 0.50 and iid:
            timed_call(client, "buyer/add_to_cart", {"item_id": iid, "quantity": 1, "session_token": session_token}, stats)
            cart_has_items = True
        elif p < 0.60 and iid:
            timed_call(client, "buyer/remove_from_cart", {"item_id": iid, "quantity": 1, "session_token": session_token}, stats)
        elif p < 0.68:
            timed_call(client, "buyer/display_cart", {"session_token": session_token}, stats)
        elif p < 0.74:
            timed_call(client, "buyer/save_cart", {"session_token": session_token}, stats)
        elif p < 0.79:
            timed_call(client, "buyer/clear_cart", {"session_token": session_token}, stats)
            cart_has_items = False
        elif p < 0.86 and iid:
            vote = "up" if rng.random() < 0.7 else "down"
            timed_call(client, "buyer/provide_feedback", {"item_id": iid, "feedback": vote, "session_token": session_token}, stats)
        elif p < 0.91 and sid:
            timed_call(client, "buyer/get_seller_rating", {"seller_id": sid, "session_token": session_token}, stats)
        elif p < 0.95:
            timed_call(client, "buyer/get_purchases", {"session_token": session_token}, stats)
        else:
            if cart_has_items and iid:
                timed_call(client, "buyer/add_to_cart", {"item_id": iid, "quantity": 1, "session_token": session_token}, stats)
                timed_call(
                    client,
                    "buyer/make_purchase",
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
                timed_call(client, "buyer/search", {"item_category": rng.randint(1, 3), "session_token": session_token}, stats)

    timed_call(client, "buyer/logout", {"session_token": session_token}, stats)
    return stats


def _load_runtime_info(runtime_dir: Path, name: str, replica_id: int) -> Optional[dict]:
    path = runtime_dir / f"{name}-{replica_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _kill_pid(pid: int) -> bool:
    try:
        signal_name = signal.SIGTERM
        signal_name = getattr(signal, "SIGTERM")
        import os
        os.kill(pid, signal_name)
        return True
    except Exception:
        return False


def trigger_failure(config_path: str, failure_mode: str) -> List[str]:
    cfg = load_config(config_path)
    runtime_dir = cfg.storage.data_dir / "runtime"
    messages: List[str] = []

    if failure_mode == "normal":
        return messages

    if failure_mode == "frontend_fail":
        for name in ("buyer-frontend", "seller-frontend"):
            info = _load_runtime_info(runtime_dir, name, 0)
            if info and _kill_pid(int(info["pid"])):
                messages.append(f"killed {name} replica 0")
        return messages

    if failure_mode in {"product_follower_fail", "product_leader_fail"}:
        chosen: Optional[dict] = None
        for replica_id in range(5):
            info = _load_runtime_info(runtime_dir, "product-db", replica_id)
            if not info:
                continue
            is_leader = bool(info.get("is_leader", False))
            if failure_mode == "product_leader_fail" and is_leader:
                chosen = info
                break
            if failure_mode == "product_follower_fail" and not is_leader:
                chosen = info
                break
        if chosen and _kill_pid(int(chosen["pid"])):
            messages.append(f"killed product-db replica {chosen['replica_id']}")
        return messages

    return messages


def schedule_failure(config_path: str, failure_mode: str, delay_seconds: float) -> threading.Thread | None:
    if failure_mode == "normal":
        return None

    def _worker() -> None:
        time.sleep(delay_seconds)
        messages = trigger_failure(config_path, failure_mode)
        for message in messages:
            print(f"[failure] {message}")

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def run_one(
    buyer_base_url: str | list[str],
    seller_base_url: str | list[str],
    n_buyers: int,
    n_sellers: int,
    ops_per_client: int,
    items_per_seller: int,
    seed: int,
    run_idx: int,
) -> Tuple[float, float, RunStats]:
    shared_item_ids: List[str] = []
    shared_seller_ids: List[int] = []
    shared_lock = threading.Lock()
    seller_futures = []
    buyer_futures = []

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_sellers + n_buyers) as executor:
        for i in range(n_sellers):
            seller_futures.append(
                executor.submit(
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
            )
        for i in range(n_buyers):
            buyer_futures.append(
                executor.submit(
                    buyer_workload,
                    buyer_base_url,
                    i,
                    run_idx,
                    ops_per_client,
                    seed + 2000 + i,
                    shared_item_ids,
                    shared_seller_ids,
                )
            )
    duration = time.perf_counter() - t0

    all_stats = RunStats()
    for future in seller_futures:
        st, _ = future.result()
        all_stats.merge(st)
    for future in buyer_futures:
        st = future.result()
        all_stats.merge(st)

    total_ops = (n_buyers + n_sellers) * ops_per_client
    throughput = (total_ops / duration) if duration > 0 else 0.0
    return all_stats.total.avg, throughput, all_stats


def _scenario_sizes(scenario: int) -> Tuple[int, int]:
    if scenario == 1:
        return 1, 1
    if scenario == 2:
        return 10, 10
    return 100, 100


def _print_per_endpoint_report(stats_by_endpoint: Dict[str, Stats], runs: int) -> None:
    print("per_function_average_response_time:")
    for endpoint in sorted(stats_by_endpoint):
        stats = stats_by_endpoint[endpoint]
        print(
            f"  {endpoint}: avg={stats.avg:.6f}s "
            f"p50={stats.p50:.6f}s p95={stats.p95:.6f}s "
            f"ok={stats.ok} err={stats.err}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--scenario", type=int, choices=[1, 2, 3], default=1)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--ops_per_client", type=int, default=1000)
    ap.add_argument("--items_per_seller", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument(
        "--failure-mode",
        choices=["normal", "frontend_fail", "product_follower_fail", "product_leader_fail"],
        default="normal",
    )
    ap.add_argument("--failure-delay", type=float, default=3.0)
    args = ap.parse_args()

    n_sellers, n_buyers = _scenario_sizes(args.scenario)
    cfg = load_config(args.config)
    buyer_base_url = [f"http://{replica.host}:{replica.port}" for replica in cfg.frontend_buyer.targets()]
    seller_base_url = [f"http://{replica.host}:{replica.port}" for replica in cfg.frontend_seller.targets()]

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
    aggregate_by_endpoint: Dict[str, Stats] = {}

    for r in range(args.runs):
        failure_thread = schedule_failure(args.config, args.failure_mode, args.failure_delay) if r == 0 else None
        avg_resp, throughput, run_stats = run_one(
            buyer_base_url,
            seller_base_url,
            n_buyers=n_buyers,
            n_sellers=n_sellers,
            ops_per_client=args.ops_per_client,
            items_per_seller=args.items_per_seller,
            seed=args.seed + r * 17,
            run_idx=r,
        )
        if failure_thread is not None:
            failure_thread.join(timeout=args.failure_delay + 1.0)

        run_avgs.append(avg_resp)
        run_throughputs.append(throughput)
        for endpoint, stats in run_stats.per_endpoint.items():
            aggregate_by_endpoint.setdefault(endpoint, Stats()).merge(stats)

        print(
            f"run {r + 1}/{args.runs}: mode={args.failure_mode} "
            f"avg_resp={avg_resp:.6f}s p50={run_stats.total.p50:.6f}s "
            f"p95={run_stats.total.p95:.6f}s throughput={throughput:.2f} ops/s"
        )

    avg_of_avgs = statistics.fmean(run_avgs) if run_avgs else 0.0
    avg_throughput = statistics.fmean(run_throughputs) if run_throughputs else 0.0

    print("\n=== PA3 Report Numbers ===")
    print(f"scenario={args.scenario} sellers={n_sellers} buyers={n_buyers}")
    print(f"failure_mode={args.failure_mode}")
    print(f"average_response_time_over_{args.runs}_runs={avg_of_avgs:.6f}s")
    print(f"average_throughput_over_{args.runs}_runs={avg_throughput:.2f} ops/s")
    _print_per_endpoint_report(aggregate_by_endpoint, args.runs)


if __name__ == "__main__":
    main()
