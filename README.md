# PA3 - Replicated Online Marketplace

This project extends the PA2 marketplace to keep the same external APIs and transport choices while removing the main single-server bottlenecks. Client to frontend traffic is still REST, frontend to backend traffic is still gRPC, and payment authorization is still SOAP/WSDL. The buyer and seller frontends are replicated over four REST servers each. The customer database is replicated over five servers using a rotating sequencer atomic broadcast layer built on UDP. The product database is replicated over five servers using a small in-repo Raft layer that handles leader election, heartbeats, log replication, and commit/apply. The local config runs all replicas on one machine with different ports, but every replica is still a separate process and talks over the network stack. For a four-VM deployment, the main change is replacing the `127.0.0.1` entries in `config/local.yaml` with the VM IP addresses for the matching replicas while keeping the same replica lists and role separation.

## Current state

What works:
- Buyer and seller REST clients can use a list of frontend replicas and fail over to another replica when one is unavailable.
- Buyer and seller REST frontends now use lists of backend replicas instead of a single hard-coded backend target.
- Customer DB mutations go through a UDP rotating sequencer broadcast layer before they are applied locally.
- Product DB replicas run behind a lightweight Raft layer and only the leader serves gRPC requests.
- Existing PA2 buyer/seller logic, gRPC services, SOAP payment path, and benchmark runner are still used.

What is still simplified:
- The Raft implementation is intentionally lightweight and embedded in this repo instead of using a downloaded third-party package.
- The storage model is still in-memory, so restarting a backend replica clears that replica's local state.
- The performance report file is not auto-generated here; the benchmark runner prints the numbers you need to record.

## PA3 design summary

### 1. Rotating sequencer atomic broadcast for Customer DB
- Every customer mutation is first sent as a UDP `Request` message to all customer replicas.
- Request IDs use the assignment format `<sender_id, local_seq_num>`.
- Global sequence number `k` is assigned by replica `k mod n`.
- The sequencer only chooses requests whose earlier requests from the same sender were already assigned.
- Replicas deliver requests strictly in global order.
- Each message carries metadata about the highest contiguous request seen per sender and the highest sequence seen so replicas can detect gaps.
- Missing `Request` or `Sequence` traffic is recovered with UDP `Retransmit` messages.

### 2. Product DB replication with Raft
- Product DB replicas keep a replicated log.
- Followers reject client-facing gRPC work and the frontend retries another replica until it reaches the leader.
- The leader appends product/cart/feedback/purchase mutations to the log, replicates them to followers, and applies them after a majority commit.
- Leader election and heartbeats are handled over UDP between product replicas.

### 3. Frontend replication
- Buyer frontend has four replicas.
- Seller frontend has four replicas.
- The client base class now accepts a list of frontend URLs and retries another replica on connection failure.

## Core Files
- `src/replication/rotating_sequencer.py`: UDP rotating sequencer atomic broadcast for customer replicas.
- `src/replication/simple_raft.py`: lightweight Raft implementation for product replicas.
- `src/backend/customer_grpc_server.py`: customer replication integration.
- `src/backend/product_grpc_server.py`: product replication integration and leader-only request handling.
- `src/frontend/buyer_rest_server.py`: buyer frontend backend-failover logic.
- `src/frontend/seller_rest_server.py`: seller frontend backend-failover logic.
- `src/clients/client_base.py`: frontend replica failover from the client side.
- `config/local.yaml`: single-machine replica layout you can later map to VM IPs.

## Local single-machine deployment

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start the five customer replicas:

```bash
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 0
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 1
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 2
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 3
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 4
```

Start the five product replicas:

```bash
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 0
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 1
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 2
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 3
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 4
```

Start the four seller frontend replicas:

```bash
python run_seller_server.py --config config/local.yaml --replica-id 0
python run_seller_server.py --config config/local.yaml --replica-id 1
python run_seller_server.py --config config/local.yaml --replica-id 2
python run_seller_server.py --config config/local.yaml --replica-id 3
```

Start the four buyer frontend replicas:

```bash
python run_buyer_server.py --config config/local.yaml --replica-id 0
python run_buyer_server.py --config config/local.yaml --replica-id 1
python run_buyer_server.py --config config/local.yaml --replica-id 2
python run_buyer_server.py --config config/local.yaml --replica-id 3
```

Start SOAP:

```bash
python -m src.financial.soap_server --config config/local.yaml
```

Run clients:

```bash
python -m src.clients.seller_cli --config config/local.yaml
python -m src.clients.buyer_cli --config config/local.yaml
```

Run benchmarks:

```bash
python -m src.clients.bench.runner --config config/local.yaml --scenario 1
python -m src.clients.bench.runner --config config/local.yaml --scenario 2
python -m src.clients.bench.runner --config config/local.yaml --scenario 3
```

Run the PA3 failure cases:

```bash
python3 scripts/pa3_local_cluster.py --config config/local.yaml start

python -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode normal
python -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode frontend_fail
python -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode product_follower_fail
python -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode product_leader_fail

python -m src.clients.bench.runner --config config/local.yaml --scenario 2 --failure-mode normal
python -m src.clients.bench.runner --config config/local.yaml --scenario 2 --failure-mode frontend_fail
python -m src.clients.bench.runner --config config/local.yaml --scenario 2 --failure-mode product_follower_fail
python -m src.clients.bench.runner --config config/local.yaml --scenario 2 --failure-mode product_leader_fail

python -m src.clients.bench.runner --config config/local.yaml --scenario 3 --failure-mode normal
python -m src.clients.bench.runner --config config/local.yaml --scenario 3 --failure-mode frontend_fail
python -m src.clients.bench.runner --config config/local.yaml --scenario 3 --failure-mode product_follower_fail
python -m src.clients.bench.runner --config config/local.yaml --scenario 3 --failure-mode product_leader_fail

python3 scripts/pa3_local_cluster.py --config config/local.yaml stop
```

## Moving from one machine to four VMs

- Keep the same replica counts.
- Replace the `host` values in `config/local.yaml` with the correct VM IPs.
- Spread replicas of the same component across different VMs instead of leaving them all on `127.0.0.1`.
- Keep each replica as a separate process even if two replicas share a VM.
- If a frontend replica crashes, restart that replica on the same IP and port.
- If a customer or product replica moves to another machine, update the matching replica entry in the config file on every process that talks to it.

## 4-VM layout

Keep `config/local.yaml` as localhost for local development. When you move to the college VMs, the comments in that file show the intended target VM for each replica. A simple layout is:

- VM1:
  - seller frontend replica 0
  - buyer frontend replica 0
  - customer DB replica 0
  - product DB replica 0
- VM2:
  - seller frontend replica 1
  - buyer frontend replica 1
  - customer DB replica 1
  - product DB replica 1
- VM3:
  - seller frontend replica 2
  - buyer frontend replica 2
  - customer DB replica 2
  - product DB replica 2
- VM4:
  - seller frontend replica 3
  - buyer frontend replica 3
  - customer DB replicas 3 and 4
  - product DB replicas 3 and 4
  - SOAP server

## Per-VM run steps

On VM1 run:

```bash
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 0
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 0
python run_seller_server.py --config config/local.yaml --replica-id 0
python run_buyer_server.py --config config/local.yaml --replica-id 0
```

On VM2 run:

```bash
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 1
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 1
python run_seller_server.py --config config/local.yaml --replica-id 1
python run_buyer_server.py --config config/local.yaml --replica-id 1
```

On VM3 run:

```bash
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 2
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 2
python run_seller_server.py --config config/local.yaml --replica-id 2
python run_buyer_server.py --config config/local.yaml --replica-id 2
```

On VM4 run:

```bash
python -m src.financial.soap_server --config config/local.yaml
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 3
python -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 4
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 3
python -m src.backend.product_grpc_server --config config/local.yaml --replica-id 4
python run_seller_server.py --config config/local.yaml --replica-id 3
python run_buyer_server.py --config config/local.yaml --replica-id 3
```

Before using the VMs for real, replace only the `host` values in `config/local.yaml` with the VM IP addresses. The ports and replica ids can stay the same.

## Evaluation notes

- Average response time is measured per API over ten runs.
- Average throughput is measured over ten runs with each client issuing 1000 API calls per run.
- For PA3 you should capture results for normal execution, frontend replica failure, product follower failure, and product leader failure.
- Use the benchmark runner output as the source for the numbers you place in the performance report.
