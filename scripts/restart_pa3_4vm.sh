#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <vm-id> [config]"
  echo "Example: PYTHON=.venv/bin/python bash scripts/restart_pa3_vm.sh 1 config/pa3_4vm.yaml"
  exit 1
fi

VM_ID=$1
CFG=${2:-config/pa3_4vm.yaml}
PYTHON=${PYTHON:-python3}
CUSTOMER_TIMEOUT=${CUSTOMER_TIMEOUT:-120}
PRODUCT_TIMEOUT=${PRODUCT_TIMEOUT:-120}
STOP_FIRST=${STOP_FIRST:-1}
WAIT_FOR_CLUSTER=${WAIT_FOR_CLUSTER:-0}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

log() {
  printf '[VM%s restart] %s\n' "$VM_ID" "$1"
}

if [[ "$STOP_FIRST" == "1" ]]; then
  log "stopping local PA3 services"
  pkill -f 'src.pa3' || true
  pkill -f 'src.financial.soap_server' || true
  sleep 1
fi

if [[ "$VM_ID" == "1" ]]; then
  log "starting soap"
  "$PYTHON" scripts/run_pa3_vm.py --config "$CFG" --vm-id "$VM_ID" --phase soap
fi

log "starting local customer replicas"
"$PYTHON" scripts/run_pa3_vm.py --config "$CFG" --vm-id "$VM_ID" --phase customers

if [[ "$WAIT_FOR_CLUSTER" == "1" ]]; then
  log "waiting for customer cluster readiness"
  "$PYTHON" scripts/wait_for_pa3_ready.py --config "$CFG" --target customer --timeout "$CUSTOMER_TIMEOUT"
fi

log "starting local product replicas"
"$PYTHON" scripts/run_pa3_vm.py --config "$CFG" --vm-id "$VM_ID" --phase products

if [[ "$WAIT_FOR_CLUSTER" == "1" ]]; then
  log "waiting for product cluster readiness"
  "$PYTHON" scripts/wait_for_pa3_ready.py --config "$CFG" --target product --timeout "$PRODUCT_TIMEOUT"
fi

log "starting local frontends"
"$PYTHON" scripts/run_pa3_vm.py --config "$CFG" --vm-id "$VM_ID" --phase frontends

if [[ "$WAIT_FOR_CLUSTER" == "1" ]]; then
  log "restart complete"
else
  log "local services launched; run wait_for_pa3_ready.py separately once all VMs are up"
fi
