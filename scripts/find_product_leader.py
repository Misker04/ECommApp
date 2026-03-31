from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path

import grpc


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pa3.config import load_pa3_config
from src.proto import product_pb2, product_pb2_grpc


NOT_LEADER_RE = re.compile(r"NOT_LEADER:(-?\d+)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()

    cfg = load_pa3_config(args.config)

    inferred_leader: int | None = None

    for replica in cfg.product_replicas:
        address = f"{replica.host}:{replica.grpc_port}"
        try:
            with grpc.insecure_channel(address) as channel:
                grpc.channel_ready_future(channel).result(timeout=min(1.0, args.timeout))
                stub = product_pb2_grpc.ProductServiceStub(channel)
                stub.Barrier(
                    product_pb2.BarrierRequest(nonce=uuid.uuid4().hex),
                    timeout=args.timeout,
                )
            print(f"replica_id={replica.id} address={address} status=LEADER")
            inferred_leader = replica.id
        except grpc.RpcError as exc:
            detail = exc.details() or exc.code().name
            match = NOT_LEADER_RE.search(detail)
            if match:
                leader_hint = int(match.group(1))
                inferred_leader = leader_hint if leader_hint >= 0 else inferred_leader
                print(
                    f"replica_id={replica.id} address={address} "
                    f"status=FOLLOWER leader_hint={leader_hint}"
                )
            else:
                print(
                    f"replica_id={replica.id} address={address} "
                    f"status=ERROR code={exc.code().name} detail={detail}"
                )
        except Exception as exc:
            print(f"replica_id={replica.id} address={address} status=ERROR detail={exc}")

    if inferred_leader is not None:
        print(f"current_product_leader={inferred_leader}")
        return 0

    print("current_product_leader=unknown")
    return 1


if __name__ == "__main__":
    sys.exit(main())
