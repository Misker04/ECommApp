### VM Setup

#### VM1
```bash
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 0
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 0
python3 run_seller_server.py --config config/local.yaml --replica-id 0
python3 run_buyer_server.py --config config/local.yaml --replica-id 0
```

#### VM2
```bash
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 1
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 1
python3 run_seller_server.py --config config/local.yaml --replica-id 1
python3 run_buyer_server.py --config config/local.yaml --replica-id 1
```

#### VM3
```bash
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 2
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 2
python3 run_seller_server.py --config config/local.yaml --replica-id 2
python3 run_buyer_server.py --config config/local.yaml --replica-id 2
```

#### VM4
```bash
python3 -m src.financial.soap_server --config config/local.yaml

python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 3
python3 -m src.backend.customer_grpc_server --config config/local.yaml --replica-id 4

python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 3
python3 -m src.backend.product_grpc_server --config config/local.yaml --replica-id 4

python3 run_seller_server.py --config config/local.yaml --replica-id 3
python3 run_buyer_server.py --config config/local.yaml --replica-id 3
```

---

## Startup Sequence


1. Start **SOAP service** on `VM4`
2. Start all **Customer DB replicas** across all VMs
3. Start all **Product DB replicas** across all VMs
4. Wait a few seconds for **Raft leader election**
5. Start all **Seller frontend replicas**
6. Start all **Buyer frontend replicas**
7. Run CLI or benchmarks from any machine that can reach the frontend replica IPs

In another terminal, run the benchmark:
```bash
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode normal
```

Run the other benchmark modes the same way:
```bash
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode frontend_fail
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode product_follower_fail
python3 -m src.clients.bench.runner --config config/local.yaml --scenario 1 --failure-mode product_leader_fail
```

Repeat for:
--scenario 2
--scenario 3
