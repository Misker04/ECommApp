cd ~/ECommApp
export VM1_IP=10.224.78.177
export VM2_IP=10.224.79.234
export VM3_IP=10.224.79.55
export VM4_IP=10.224.76.94
cp config/pa3_4vm_example.yaml config/pa3_4vm.yaml
sed -i \
  -e "s/VM1_IP/${VM1_IP}/g" \
  -e "s/VM2_IP/${VM2_IP}/g" \
  -e "s/VM3_IP/${VM3_IP}/g" \
  -e "s/VM4_IP/${VM4_IP}/g" \
  config/pa3_4vm.yaml

- - - -
cd ~/ECommApp
pkill -f 'src.pa3' || true
pkill -f 'src.financial.soap_server' || true

- - - -
python3 ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml soap

python3 ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml customers
python3 ./scripts/run_pa3_vm.sh 2 config/pa3_4vm.yaml customers
python3 ./scripts/run_pa3_vm.sh 3 config/pa3_4vm.yaml customers
python3 ./scripts/run_pa3_vm.sh 4 config/pa3_4vm.yaml customers

python3 scripts/wait_for_pa3_ready.py --config config/pa3_4vm.yaml --target customer --timeout 60

- - - -
python3 ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml products
python3 ./scripts/run_pa3_vm.sh 2 config/pa3_4vm.yaml products
python3 ./scripts/run_pa3_vm.sh 3 config/pa3_4vm.yaml products
python3 ./scripts/run_pa3_vm.sh 4 config/pa3_4vm.yaml products

python3 scripts/wait_for_pa3_ready.py --config config/pa3_4vm.yaml --target product --timeout 60

- - - -
python3 ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml frontends
python3 ./scripts/run_pa3_vm.sh 2 config/pa3_4vm.yaml frontends
python3 ./scripts/run_pa3_vm.sh 3 config/pa3_4vm.yaml frontends
python3 ./scripts/run_pa3_vm.sh 4 config/pa3_4vm.yaml frontends

- - - -
U="seller$(date +%H%M%S)"
curl -s -X POST "http://${VM1_IP}:58080/seller/create_account" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${U}\",\"password\":\"pw\"}"

- - - -
python3 -m src.clients.seller_cli --config config/pa3_4vm.yaml
python3 -m src.clients.buyer_cli --config config/pa3_4vm.yaml

- - - -
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 2 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 3 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1

- - - -
mkdir -p eval
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label normal | tee eval/scenario1_normal.txt

- - - -
pkill -f 'src.pa3.frontend.seller_rest_server_pa3 --config config/pa3_4vm.yaml --frontend-index 1'
pkill -f 'src.pa3.frontend.buyer_rest_server_pa3 --config config/pa3_4vm.yaml --frontend-index 1'

- - - -
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label frontend_failed | tee eval/scenario1_frontend_failed.txt

- - - -
python3 scripts/find_product_leader.py --config config/pa3_4vm.yaml

pkill -f 'src.pa3.product_replica_server --config config/pa3_4vm.yaml --replica-id 0'
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_follower_failed | tee eval/scenario1_product_follower_failed.txt

- - - -
python3 scripts/find_product_leader.py --config config/pa3_4vm.yaml

pkill -f 'src.pa3.product_replica_server --config config/pa3_4vm.yaml --replica-id 1'
sleep 5
python3 scripts/find_product_leader.py --config config/pa3_4vm.yaml
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_leader_failed | tee eval/scenario1_product_leader_failed.txt



# PA3 ECommApp

This repository now contains a working PA3 layout for the distributed e-commerce app:

- client to frontend: REST
- frontend to backend: gRPC
- frontend to payment service: SOAP/WSDL
- customer replicas: rotating-sequencer atomic broadcast over UDP
- product replicas: Raft-style replicated log over TCP
- buyer and seller frontends: stateless replicas with client-side failover

## Authoritative PA3 structure

- `src/pa3/`
  contains the PA3 customer replicas, product replicas, rotating sequencer, Raft node, and PA3 frontends
- `src/clients/`
  contains the PA3-aware buyer CLI, seller CLI, and benchmark runner
- `config/pa3_local.yaml`
  contains the local Windows configuration for 5 customer replicas, 5 product replicas, 4 buyer frontends, 4 seller frontends, and SOAP
- `config/pa3_4vm_example.yaml`
  contains a shared 4-VM deployment template; replace `VM1_IP`..`VM4_IP` with your actual VM addresses
- `scripts/run_pa3_cluster.ps1`
  starts the local PA3 cluster on Windows
- `scripts/run_pa3_vm.py`
  starts only the service subset assigned to one VM in the 4-VM layout
- `scripts/run_pa3_vm.ps1` / `scripts/run_pa3_vm.sh`
  convenience wrappers for the 4-VM launcher on Windows and Linux
- `scripts/stop_pa3_cluster.ps1`
  stops PA3 listeners on Windows by the configured ports

Legacy PA1 and PA2 code is still present under `src/backend/`, `src/frontend/`, and `src/server/` for reference, but the PA3 implementation should use the `src/pa3/` and `src/clients/` paths above.

## Local Windows run

From the repository root in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv310\Scripts\Activate.ps1
.\scripts\stop_pa3_cluster.ps1 -Config config/pa3_local.yaml
.\scripts\run_pa3_cluster.ps1 -Config config/pa3_local.yaml
```

Smoke-test seller account creation:

```powershell
$u = "seller$((Get-Date).ToString('HHmmss'))"
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:58080/seller/create_account" `
  -ContentType "application/json" `
  -Body "{""username"":""$u"",""password"":""pw""}"
```

Start the CLIs in separate terminals:

```powershell
.\.venv310\Scripts\python.exe -m src.clients.seller_cli --config config/pa3_local.yaml
```

```powershell
.\.venv310\Scripts\python.exe -m src.clients.buyer_cli --config config/pa3_local.yaml
```

## Benchmark commands

```powershell
.\.venv310\Scripts\python.exe -m src.clients.bench.runner --config config/pa3_local.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1
```

```powershell
.\.venv310\Scripts\python.exe -m src.clients.bench.runner --config config/pa3_local.yaml --scenario 2 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1
```

```powershell
.\.venv310\Scripts\python.exe -m src.clients.bench.runner --config config/pa3_local.yaml --scenario 3 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1
```

## Four-VM deployment

Edit `config/pa3_4vm_example.yaml` first and replace:

- `VM1_IP`
- `VM2_IP`
- `VM3_IP`
- `VM4_IP`

The provided VM layout is:

- VM1: `soap`, `customer_0`, `product_0`, `seller_fe_0`, `buyer_fe_0`
- VM2: `customer_1`, `product_1`, `seller_fe_1`, `buyer_fe_1`
- VM3: `customer_2`, `customer_4`, `product_2`, `seller_fe_2`, `buyer_fe_2`
- VM4: `customer_3`, `product_3`, `product_4`, `seller_fe_3`, `buyer_fe_3`

Use phased startup in the VMs:

1. Start `soap` on VM1.
2. Start `customers` on all four VMs.
3. Wait for the customer cluster to become ready.
4. Start `products` on all four VMs.
5. Wait for the product cluster to become ready.
6. Start `frontends` on all four VMs.

Windows PowerShell startup:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv310\Scripts\Activate.ps1
.\scripts\run_pa3_vm.ps1 -VmId 1 -Phase soap -Config config/pa3_4vm_example.yaml
```

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv310\Scripts\Activate.ps1
.\scripts\run_pa3_vm.ps1 -VmId 1 -Phase customers -Config config/pa3_4vm_example.yaml
```

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv310\Scripts\Activate.ps1
.\scripts\run_pa3_vm.ps1 -VmId 2 -Phase customers -Config config/pa3_4vm_example.yaml
```

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv310\Scripts\Activate.ps1
.\scripts\run_pa3_vm.ps1 -VmId 3 -Phase customers -Config config/pa3_4vm_example.yaml
```

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv310\Scripts\Activate.ps1
.\scripts\run_pa3_vm.ps1 -VmId 4 -Phase customers -Config config/pa3_4vm_example.yaml
```

After all customer replicas are up, verify customer readiness from any VM:

```powershell
.\.venv310\Scripts\python.exe scripts\wait_for_pa3_ready.py --config config/pa3_4vm_example.yaml --target customer --timeout 60
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 1 -Phase products -Config config/pa3_4vm_example.yaml
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 2 -Phase products -Config config/pa3_4vm_example.yaml
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 3 -Phase products -Config config/pa3_4vm_example.yaml
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 4 -Phase products -Config config/pa3_4vm_example.yaml
```

After all product replicas are up, verify product readiness from any VM:

```powershell
.\.venv310\Scripts\python.exe scripts\wait_for_pa3_ready.py --config config/pa3_4vm_example.yaml --target product --timeout 60
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 1 -Phase frontends -Config config/pa3_4vm_example.yaml
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 2 -Phase frontends -Config config/pa3_4vm_example.yaml
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 3 -Phase frontends -Config config/pa3_4vm_example.yaml
```

```powershell
.\scripts\run_pa3_vm.ps1 -VmId 4 -Phase frontends -Config config/pa3_4vm_example.yaml
```

Linux startup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 1 config/pa3_4vm_example.yaml soap
```

```bash
source .venv/bin/activate
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 1 config/pa3_4vm_example.yaml customers
```

```bash
source .venv/bin/activate
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 2 config/pa3_4vm_example.yaml customers
```

```bash
source .venv/bin/activate
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 3 config/pa3_4vm_example.yaml customers
```

```bash
source .venv/bin/activate
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 4 config/pa3_4vm_example.yaml customers
```

After all customer replicas are up, verify customer readiness from any VM:

```bash
python3 scripts/wait_for_pa3_ready.py --config config/pa3_4vm_example.yaml --target customer --timeout 60
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 1 config/pa3_4vm_example.yaml products
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 2 config/pa3_4vm_example.yaml products
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 3 config/pa3_4vm_example.yaml products
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 4 config/pa3_4vm_example.yaml products
```

After all product replicas are up, verify product readiness from any VM:

```bash
python3 scripts/wait_for_pa3_ready.py --config config/pa3_4vm_example.yaml --target product --timeout 60
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 1 config/pa3_4vm_example.yaml frontends
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 2 config/pa3_4vm_example.yaml frontends
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 3 config/pa3_4vm_example.yaml frontends
```

```bash
PYTHON=.venv/bin/python ./scripts/run_pa3_vm.sh 4 config/pa3_4vm_example.yaml frontends
```

Then run the CLIs or benchmark from any machine that can reach all four frontend replicas:

```bash
python3 -m src.clients.seller_cli --config config/pa3_4vm_example.yaml
```

```bash
python3 -m src.clients.buyer_cli --config config/pa3_4vm_example.yaml
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm_example.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1
```

## Replication notes

Customer replica request, sequence, and retransmit messages carry:

- per-origin contiguous request knowledge
- contiguous sequence knowledge
- delivered sequence knowledge

That metadata is used for:

- loss recovery
- majority receipt detection
- safe in-order delivery at all replicas

Product replicas use a classroom-oriented Raft implementation with:

- leader election
- append entries replication
- deterministic replicated state-machine application
