# PA3 - Replicated Online Marketplace

This project is the PA3 version of our PA2 marketplace. The external APIs stay the same as PA2: client to frontend uses REST, frontend to backend uses gRPC, and payment uses SOAP/WSDL. The main PA3 change is that the server side is now replicated so the system is not dependent on only one frontend or one backend process.

## System Design and Assumptions

- The buyer and seller frontends are each replicated on 4 REST servers, and clients keep a list of replicas and retry another one if the current replica fails.
- The customer database is replicated on 5 servers using a rotating sequencer atomic broadcast protocol built on UDP.
- Every customer update is first broadcast as a `Request`, then ordered by the rotating sequencer through a `Sequence` message, and missing messages are recovered with `Retransmit` requests.
- Request IDs follow the format `<sender_id, local_seq_num>` and global sequence numbers start from `0`.
- The product database is replicated on 5 servers using Raft with leader election, log replication, heartbeats, and majority commit.
- Only the current product leader handles client-facing writes, and followers redirect or force retry through another replica.
- The buyer and seller frontends are stateless, so no replication protocol is needed for them beyond client-side failover.
- All replicas run as separate processes and communicate through the network stack, even when multiple replicas are placed on the same VM.
- We assume unreliable communication for customer/product replication, crash failures for product replicas, and restart failed frontend replicas on the same IP/port.

## Current State

What works:

- Buyer and seller frontend replication is working with client-side failover across replica lists.
- Customer DB replication is implemented with the rotating sequencer atomic broadcast layer over UDP.
- Product DB replication is implemented with Raft-based leader election and replication.
- The PA2 request flow still works with REST, gRPC, and SOAP.
- The benchmark runner supports scenarios `1`, `2`, and `3`, and also supports the PA3 failure modes.

What is not fully complete / simplified:

- Backend state is still in memory, so restarting a backend replica resets that replica's local state.

## Deployment Setup

This project is configured to run on 4 VMs. In our setup, replicas of the same component are spread across different machines, and every replica runs as its own process.

| VM | Services |
|---|---|
| VM1 | Seller frontend replica 0, Buyer frontend replica 0, Customer DB replica 0, Product DB replica 0 |
| VM2 | Seller frontend replica 1, Buyer frontend replica 1, Customer DB replica 1, Product DB replica 1 |
| VM3 | Seller frontend replica 2, Buyer frontend replica 2, Customer DB replica 2, Product DB replica 2 |
| VM4 | Seller frontend replica 3, Buyer frontend replica 3, Customer DB replicas 3 and 4, Product DB replicas 3 and 4, SOAP server |

The active replica addresses and ports are already listed in `config/local.yaml`. If the deployment changes, update the host entries there before running the system.

## Requirements

- Python 3.10 or 3.11 
- Virtual environment
- Dependencies from `requirements.txt`
- At least 4 VMs for cloud deployment

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Generate protobuf files:

```bash
bash generate_proto.sh
```

## Run Order

Start the services in this order:

1. SOAP server
2. All 5 customer DB replicas
3. All 5 product DB replicas
4. All 4 seller frontend replicas
5. All 4 buyer frontend replicas
6. Buyer/seller clients or benchmark runner

## Commands

Start SOAP:

```bash
python3 -m src.financial.soap_server --config config/local.yaml
```

Start customer DB replicas:

```bash
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 0
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 1
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 2
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 3
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 4
```

Start product DB replicas:

```bash
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 0
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 1
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 2
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 3
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 4
```

Start seller frontend replicas:

```bash
python3 run_seller_server.py --config config/local.yaml --replica-id 0
python3 run_seller_server.py --config config/local.yaml --replica-id 1
python3 run_seller_server.py --config config/local.yaml --replica-id 2
python3 run_seller_server.py --config config/local.yaml --replica-id 3
```

Start buyer frontend replicas:

```bash
python3 run_buyer_server.py --config config/local.yaml --replica-id 0
python3 run_buyer_server.py --config config/local.yaml --replica-id 1
python3 run_buyer_server.py --config config/local.yaml --replica-id 2
python3 run_buyer_server.py --config config/local.yaml --replica-id 3
```

Run clients:

```bash
python3 -m src.clients.seller_cli --config config/local.yaml
python3 -m src.clients.buyer_cli --config config/local.yaml
```

## Benchmark / Evaluation

Run the benchmark scenarios:

```bash
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 2
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 3
```

Run the PA3 failure cases:

```bash
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode normal
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode frontend_fail
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode product_follower_fail
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode product_leader_fail
```

Repeat the same for scenarios `2` and `3`.

