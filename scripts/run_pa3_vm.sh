#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <vm-id> [config] [phase]"
  exit 1
fi

VM_ID=$1
CFG=${2:-config/pa3_4vm_example.yaml}
PHASE=${3:-all}
PYTHON=${PYTHON:-python3}

"$PYTHON" scripts/run_pa3_vm.py --config "$CFG" --vm-id "$VM_ID" --phase "$PHASE"
