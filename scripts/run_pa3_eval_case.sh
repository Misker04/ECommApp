#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <config> <scenario> [label] [ssh-user]"
  echo "Labels: normal frontend_failed product_follower_failed product_leader_failed"
  exit 1
fi

CFG=$1
SCENARIO=$2
LABEL=${3:-normal}
SSH_USER=${4:-}
RUNS=${RUNS:-10}
OPS_PER_CLIENT=${OPS_PER_CLIENT:-1000}
ITEMS_PER_SELLER=${ITEMS_PER_SELLER:-5}
WARMUP=${WARMUP:-1}
FRONTEND_INDEX=${FRONTEND_INDEX:-1}
LEADER_FAILOVER_WAIT=${LEADER_FAILOVER_WAIT:-5}

FAIL_ARGS=()
if [[ -n "$SSH_USER" ]]; then
  FAIL_ARGS+=(--ssh-user "$SSH_USER")
fi

case "$LABEL" in
  normal)
    ;;
  frontend_failed)
    python3 scripts/fail_pa3_service.py --config "$CFG" --mode frontend --frontend-index "$FRONTEND_INDEX" "${FAIL_ARGS[@]}"
    ;;
  product_follower_failed)
    python3 scripts/fail_pa3_service.py --config "$CFG" --mode product-follower "${FAIL_ARGS[@]}"
    ;;
  product_leader_failed)
    python3 scripts/fail_pa3_service.py --config "$CFG" --mode product-leader "${FAIL_ARGS[@]}"
    sleep "$LEADER_FAILOVER_WAIT"
    ;;
  *)
    echo "Unknown label: $LABEL"
    exit 1
    ;;
esac

mkdir -p eval
python3 -m src.clients.bench.runner \
  --config "$CFG" \
  --scenario "$SCENARIO" \
  --runs "$RUNS" \
  --ops_per_client "$OPS_PER_CLIENT" \
  --items_per_seller "$ITEMS_PER_SELLER" \
  --warmup "$WARMUP" \
  --label "$LABEL" | tee "eval/scenario${SCENARIO}_${LABEL}.txt"
