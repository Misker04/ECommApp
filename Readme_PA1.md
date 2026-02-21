# Assignment 1 - E-Commerce System

This project implements the Assignment‑1 required components for an e-commerce system using **socket-based TCP/IP** for all interprocess communication.

## System design and assumptions
The system is split into six TCP-connected components: Buyer CLI, Seller CLI, Buyer Frontend, Seller Frontend, CustomerDB, and ProductDB.
Clients keep a separate TCP connection per process, but sessions are identified by a `session_token` returned at login.
Buyer/Seller Frontends are stateless routers: they validate requests and forward them to the appropriate DB backend over TCP.
CustomerDB stores accounts, active sessions (with last-activity timestamps), carts, saved carts, and purchase history.
ProductDB stores items, per-category item-id allocation, quantities, and feedback (thumbs up/down).
All messages use a length-prefixed JSON protocol (4-byte big-endian length + UTF-8 JSON payload).
Passwords are stored/transmitted in cleartext for now.
All state is in-memory in the DB servers (restarts clear state).

## Current state
What Works: 
- All Seller APIs and Buyer APIs required (including search, cart ops, feedback, and ratings), plus session timeout after 5 minutes of inactivity.
- Stateless frontend semantics (frontends do not retain per-user state; reconnects do not reset sessions/carts as long as DB servers stay up).
- Concurrent evaluation via the benchmark runner for scenarios 1/2/3 with per-call latency and run throughput reporting.

Not implemented: 
- `MakePurchase`
- Security (login, finance etc.)


## Components implemented (6)
Each component can run as a separate process on a different machine (different IP/port):
1. Client-side Buyer CLI (`src/clients/buyer_cli.py`)
2. Client-side Seller CLI (`src/clients/seller_cli.py`)
3. Server-side Buyer Interface (stateless frontend) (`src/frontend/buyer_frontend_server.py`)
4. Server-side Seller Interface (stateless frontend) (`src/frontend/seller_frontend_server.py`)
5. Customer Database backend (`src/backend/customer_db_server.py`)
6. Product Database backend (`src/backend/product_db_server.py`)

## Search semantics ("best" match)
`SearchItemsForSale(item_category, keywords<=5)` uses these semantics:
- Filter candidates to items in the given category with `item_quantity > 0`.
- If no keywords are provided: return **all** candidates in that category.
- If keywords are provided:
  - Normalize to lowercase.
  - For each item, compute **match score** = number of query keywords that match either:
    1. the item’s `keywords` list (case-insensitive exact match), or
    2. a token in the item name (case-insensitive exact match, tokenized on non-alphanumerics).
  - Return items with score > 0.
  - Sort by: match score (desc), (thumbs_up - thumbs_down) (desc), sale_price (asc), item_id (asc).

This is implemented in `src/backend/product_db_server.py`.

## Session model & timeout
- Login returns a `session_token`.
- Every request after login includes `session_token`.
- Session state is stored in **CustomerDB**, not in the frontends.
- **Session timeout**: if no activity for **5 minutes** (configurable), the session is automatically invalidated.

## Stateless frontend requirement
The Buyer/Seller frontend servers do **not** store persistent per-user or cross-request state in memory:
- No carts in frontend memory.
- No session/login state in frontend memory.
- No item metadata caching in frontend memory.

All durable state (sessions, carts, accounts, items) lives in the backend DB servers.

## Run locally
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1: Customer DB
python -m src.backend.customer_db_server --config config/local.yaml

# Terminal 2: Product DB
python -m src.backend.product_db_server --config config/local.yaml

# Terminal 3: Seller frontend
python -m src.frontend.seller_frontend_server --config config/local.yaml

# Terminal 4: Buyer frontend
python -m src.frontend.buyer_frontend_server --config config/local.yaml

# Terminal 5: Seller CLI
python -m src.clients.seller_cli --config config/local.yaml

# Terminal 6: Buyer CLI
python -m src.clients.buyer_cli --config config/local.yaml
```

## Protocol
All communication is a custom TCP protocol:
- 4-byte big-endian unsigned length prefix
- UTF-8 JSON object payload

Request:
```json
{
  "req_id": "string",
  "role": "buyer" | "seller",
  "action": "CreateAccount" | "Login" | "SearchItemsForSale" | ...,
  "data": { }
}
```

Response:
```json
{
  "req_id": "string",
  "ok": true,
  "error": null,
  "data": { }
}
```
