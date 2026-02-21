# Programming Assignment 2:  Distributed Marketplace (REST + gRPC + SOAP)

## System design & Assumptions
This system implements a distributed online marketplace deployed as independent services across multiple VMs. Buyer and Seller clients interact only with stateless **REST (FastAPI)** frontends, which forward requests to backend databases using **gRPC**. CustomerDB (gRPC) stores user accounts, sessions (via `session_token`), carts/saved carts, and purchase history, while ProductDB (gRPC) stores items, quantities, per-category item-id allocation, and feedback. Purchases invoke a third-party **SOAP/WSDL Financial Transactions** service that probabilistically approves (90%) or declines (10%) transactions, and the Buyer MakePurchase flow depends on this authorization. All communication between services is over the network (no co-location assumptions), and all persistent state is kept in backend services (frontends do not retain per-user state). Search uses “best keyword match” by counting keyword overlaps in item keywords/name tokens and sorting by match score, feedback, price, and item id. State is in-memory in the backends for this assignment, so restarting a backend clears state. Passwords are not hardened (no encryption/secure storage) since security is out of scope.

## Current state (what works / what not)
**Works**
- Buyer/Seller REST frontends and gRPC backends run on separate VMs (or locally) using the required service startup order.
- Core Seller APIs and Buyer APIs (search, cart ops, feedback/ratings) function end-to-end through REST → gRPC.
- Session model using `session_token` with validation handled by CustomerDB.
- `MakePurchase` invokes the SOAP financial service and completes/denies purchases based on authorization.
- Benchmark runner supports scenarios 1/2/3 and reports per-run latency and throughput.

**Not / limitations**
- All backend state is in-memory; restarting CustomerDB/ProductDB clears state (no persistence).
- Security is minimal (e.g., passwords not securely stored; transport security not configured).
- Purchase workflow is best-effort across services (no distributed transactions); under failures, partial completion may require manual rerun/retry depending on where the failure occurs.

---

## Requirements
- Python **3.10 / 3.11**
- `pip install -r requirements.txt`
- For protobuf generation: `bash generate_proto.sh`
- For full deployment: **5 VMs** (Customer gRPC, Product gRPC, SOAP, Seller REST, Buyer REST)

> For VM deployment, update IPs/ports in `config/local.yaml`.

---

## Generate protobuf files
```bash
bash generate_proto.sh
```

---

## Deployment (5 VMs) and run order
**VM1 — Customer gRPC**
```bash
python3 -m src.backend.customer_grpc_server --config config/local.yaml
```

**VM2 — Product gRPC**
```bash
python3 -m src.backend.product_grpc_server --config config/local.yaml
```

**VM3 — SOAP Financial Service**
```bash
python3 -m src.financial.soap_server --config config/local.yaml
```

**VM4 — Seller REST**
```bash
python3 run_seller_server.py --config config/local.yaml
```

**VM5 — Buyer REST**
```bash
python3 run_buyer_server.py --config config/local.yaml
```

---

## Run benchmark
```bash
python -m src.clients.bench.runner --config config/local.yaml --scenario 1
# --scenario 2
# --scenario 3
```

---

## Notes
- Start **gRPC backends before REST** frontends.
- Ensure ports are free/open on VMs (gRPC 50051/50052, REST ports, SOAP port).
- If `.proto` files change, regenerate stubs with `generate_proto.sh`.
