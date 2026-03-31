#!/usr/bin/env bash
set -euo pipefail

CFG=${1:-config/pa3_local.yaml}
PYTHON=${PYTHON:-python}

PIDS=()

cleanup() {
  echo "Stopping PA3 cluster..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# Start SOAP service first so purchase calls have a live dependency.
$PYTHON -m src.financial.soap_server --config "$CFG" >"soap.log" 2>&1 &
PIDS+=($!)

sleep 1

# Start customer replicas
for rid in 0 1 2 3 4; do
  $PYTHON -m src.pa3.customer_replica_server --config "$CFG" --replica-id "$rid" >"customer_${rid}.log" 2>&1 &
  PIDS+=($!)
done

# Start product replicas
for rid in 0 1 2 3 4; do
  $PYTHON -m src.pa3.product_replica_server --config "$CFG" --replica-id "$rid" >"product_${rid}.log" 2>&1 &
  PIDS+=($!)
done

# Give DB replicas a moment to bind ports
sleep 3

# Start seller and buyer frontends
for idx in 0 1 2 3; do
  $PYTHON -m src.pa3.frontend.seller_rest_server_pa3 --config "$CFG" --frontend-index "$idx" >"seller_fe_${idx}.log" 2>&1 &
  PIDS+=($!)

  $PYTHON -m src.pa3.frontend.buyer_rest_server_pa3 --config "$CFG" --frontend-index "$idx" >"buyer_fe_${idx}.log" 2>&1 &
  PIDS+=($!)
done

echo "PA3 local cluster started"
echo "Logs: customer_*.log product_*.log seller_fe_*.log buyer_fe_*.log"

wait
