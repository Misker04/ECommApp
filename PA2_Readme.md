# 🛒 PA2 – Distributed Marketplace (gRPC + REST + SOAP)

This project extends **PA1 (TCP-based architecture)** into a multi-protocol distributed system using:

- **gRPC** – Customer & Product backends  
- **REST (FastAPI)** – Buyer & Seller frontends  
- **SOAP** – Financial service  
- **Performance Runner** – Benchmarking scenarios  

---

#  System Architecture

Buyer (REST) → Customer (gRPC)  
Seller (REST) → Product (gRPC)  
Both → SOAP Financial Service  

---

#  Requirements

- Python **3.10 or 3.11** (recommended)
- Virtual environment
- Dependencies installed via `requirements.txt`
- 4 Virtual Machines (for full distributed deployment)

>  Local testing works with `127.0.0.1`  
> For VM deployment, update IP addresses in `config/local.yaml`.

---

#  Setup Instructions


##  Step 1 – Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Step 2 – Generate Protobuf Files

Before running any backend server:

```bash
bash generate_proto.sh
```

This generates:

- `customer_pb2.py`
- `customer_pb2_grpc.py`
- `product_pb2.py`
- `product_pb2_grpc.py`

 If you modify `.proto` files, regenerate them.

---

#  Deployment Architecture (5 VMs)

| VM  | Service                |
|-----|------------------------|
| VM1 | Customer gRPC Backend  |
| VM2 | Product gRPC Backend   |
| VM3 or any used VM | SOAP Financial Service |
| VM4 | Seller REST Frontend   |
| VM5 | Buyer REST Frontend    |

Performance runner and Soap service can run on any VM.

---

#  Service Execution Order (IMPORTANT)

Services **must** be started in the following order:

---

##  1️⃣ VM1 – Customer gRPC Backend

```bash
python3 -m src.backend.customer_grpc_server --config config/local.yaml
```

---

##  2️⃣ VM2 – Product gRPC Backend

```bash
python3 -m src.backend.product_grpc_server --config config/local.yaml
```

---

##  3️⃣ VM3 – SOAP Financial Service (Can be VM1/VM2/VM4/VM5)

```bash
python3 -m src.financial.soap_server --config config/local.yaml
```

> SOAP may run on any VM but must be running before purchases.

---

##  4️⃣ VM4 – Seller REST Frontend

```bash
python3 run_seller_server.py --config config/local.yaml
```

---

##  5️⃣ VM5 – Buyer REST Frontend

```bash
python3 run_buyer_server.py --config config/local.yaml
```

---

# Step 6 – Run Performance Benchmark

After all services are running:

```bash
python -m src.clients.bench.runner --config config/local.yaml --scenario 1
```

Available scenarios:

```bash
--scenario 1
--scenario 2
--scenario 3
```

---

#  Running Everything Locally (Single Machine)

If testing locally:

1. Keep all hosts as `127.0.0.1` in `config/local.yaml`
2. Open **5 separate terminals**
3. Run services in the required order
4. Run performance runner last

---

#  VM Deployment Configuration

Before deploying on VMs:

1. Open `config/local.yaml`
2. Replace `127.0.0.1` with actual VM IP addresses
3. ---

## How to Get the VM IP Address

On **each VM**, run the following command:

```bash
hostname -I | awk '{print $1}'
```
4. Ensure:
   - Required ports are open
   - Firewalls allow communication
   - gRPC ports (50051, 50052) are accessible
   - REST ports are accessible
   - SOAP service port is accessible

---

#  Important Notes

- gRPC backends **must start before** REST frontends.
- SOAP service must be running before `make_purchase`.
- Always regenerate proto files after modifying `.proto`.
- Python 3.12 may cause issues with Spyne (SOAP library).
- If services fail, verify IP addresses and port availability.

---

#  Complete Execution Summary

```text
1️⃣ Clone Repo
2️⃣ Create Virtual Environment
3️⃣ Install Dependencies
4️⃣ Generate Protobuf Files
5️⃣ Start Customer gRPC
6️⃣ Start Product gRPC
7️⃣ Start SOAP Service
8️⃣ Start Seller REST
9️⃣ Start Buyer REST
🔟 Run Performance Runner
```

---

# Project Goal

This project demonstrates:

- Multi-protocol distributed communication
- Service orchestration across VMs
- gRPC microservices
- REST frontend integration
- SOAP financial transaction handling
- Performance benchmarking

---

## 👩‍💻 Distributed Systems – PA2
Multi-Protocol Marketplace Architecture
