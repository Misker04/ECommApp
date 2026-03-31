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
- `scripts/run_pa3_cluster.ps1`
  starts the local PA3 cluster on Windows
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
