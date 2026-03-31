Use `VM1` as the machine where you run readiness checks and all benchmarks.

**One-Time Sync On All 4 VMs**
Run this on `VM1`, `VM2`, `VM3`, and `VM4`:

```bash
cd ~/ECommApp
git fetch origin
git switch PA_3
git pull --ff-only origin PA_3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x scripts/run_pa3_vm.sh scripts/run_pa3_eval_case.sh
```

Create the same config on all 4 VMs:

```bash
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
```

**Cluster Start Sheet**
Run this fresh before every condition: `normal`, `frontend_failed`, `product_follower_failed`, `product_leader_failed`.

On all 4 VMs, stop old processes first:

```bash
cd ~/ECommApp
pkill -f 'src.pa3' || true
pkill -f 'src.financial.soap_server' || true
source .venv/bin/activate
```

bash ./scripts/restart_pa3_4vm.sh 1 config/pa3_4vm.yaml

On `VM1` only:

```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml soap
```

Customer phase:
On `VM1`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml customers
```

On `VM2`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 2 config/pa3_4vm.yaml customers
```

On `VM3`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 3 config/pa3_4vm.yaml customers
```

On `VM4`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 4 config/pa3_4vm.yaml customers
```

Back on `VM1`, wait for customer readiness:

```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/wait_for_pa3_ready.py --config config/pa3_4vm.yaml --target customer --timeout 60
```

Product phase:
On `VM1`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml products
```

On `VM2`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 2 config/pa3_4vm.yaml products
```

On `VM3`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 3 config/pa3_4vm.yaml products
```

On `VM4`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 4 config/pa3_4vm.yaml products
```

Back on `VM1`, wait for product readiness:

```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/wait_for_pa3_ready.py --config config/pa3_4vm.yaml --target product --timeout 60
```

Frontend phase:
On `VM1`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 1 config/pa3_4vm.yaml frontends
```

On `VM2`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 2 config/pa3_4vm.yaml frontends
```

On `VM3`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 3 config/pa3_4vm.yaml frontends
```

On `VM4`:
```bash
cd ~/ECommApp
source .venv/bin/activate
PYTHON=.venv/bin/python bash ./scripts/run_pa3_vm.sh 4 config/pa3_4vm.yaml frontends
```

Optional quick smoke test on `VM1`:

```bash
cd ~/ECommApp
U="seller$(date +%H%M%S)"
curl -s -X POST "http://10.224.78.177:58080/seller/create_account" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${U}\",\"password\":\"pw\"}"
```

Create the output folder once on `VM1`:

```bash
cd ~/ECommApp
mkdir -p eval
source .venv/bin/activate
```

**Failure Actions**
Use these after the cluster is up and before the benchmark command.

Frontend failure:
Run on `VM2`:

```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode frontend --frontend-index 1
```

Find current product leader:
Run on `VM1`:

```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/find_product_leader.py --config config/pa3_4vm.yaml
```

Product replica mapping:
- replica `0` is on `VM1`
- replica `1` is on `VM2`
- replica `2` is on `VM3`
- replica `3` is on `VM4`
- replica `4` is on `VM4`

Follower failure:
- if leader is `0`, run this on `VM2`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 1
```
- if leader is `1`, `2`, `3`, or `4`, run this on `VM1`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 0
```

Leader failure:
- if leader is `0`, run on `VM1`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 0
sleep 5
```
- if leader is `1`, run on `VM2`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 1
sleep 5
```
- if leader is `2`, run on `VM3`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 2
sleep 5
```
- if leader is `3`, run on `VM4`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 3
sleep 5
```
- if leader is `4`, run on `VM4`:
```bash
cd ~/ECommApp
source .venv/bin/activate
python3 scripts/fail_pa3_service.py --config config/pa3_4vm.yaml --mode product-replica --replica-id 4
sleep 5
```

**12 Benchmark Commands**
Run all of these on `VM1`.

Scenario 1:
```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label normal | tee eval/scenario1_normal.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label frontend_failed | tee eval/scenario1_frontend_failed.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_follower_failed | tee eval/scenario1_product_follower_failed.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 1 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_leader_failed | tee eval/scenario1_product_leader_failed.txt
```

Scenario 2:
```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 2 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label normal | tee eval/scenario2_normal.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 2 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label frontend_failed | tee eval/scenario2_frontend_failed.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 2 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_follower_failed | tee eval/scenario2_product_follower_failed.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 2 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_leader_failed | tee eval/scenario2_product_leader_failed.txt
```

Scenario 3:
```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 3 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label normal | tee eval/scenario3_normal.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 3 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label frontend_failed | tee eval/scenario3_frontend_failed.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 3 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_follower_failed | tee eval/scenario3_product_follower_failed.txt
```

```bash
python3 -m src.clients.bench.runner --config config/pa3_4vm.yaml --scenario 3 --runs 10 --ops_per_client 1000 --items_per_seller 5 --warmup 1 --label product_leader_failed | tee eval/scenario3_product_leader_failed.txt
```

**Exact Run Order For One Condition**
1. restart cluster on all 4 VMs using the cluster start sheet
2. on `VM1`, confirm customer and product readiness
3. if needed, trigger the failure on the target VM
4. run exactly one benchmark command on `VM1`
5. save the output file
6. restart the cluster again before the next condition
