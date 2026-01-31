# Online Marketplace (Assignment 1)

This codebase implements the Assignment‑1 required components for an online marketplace using **socket-based TCP/IP** for **all interprocess communication**.

## Components implemented (6)
Each component can run as a separate process on a different machine (different IP/port):
1. Client-side Buyer CLI (`src/clients/buyer_cli.py`)
2. Client-side Seller CLI (`src/clients/seller_cli.py`)
3. Server-side Buyer Interface (stateless frontend) (`src/frontend/buyer_frontend_server.py`)
4. Server-side Seller Interface (stateless frontend) (`src/frontend/seller_frontend_server.py`)
5. Customer Database backend (`src/backend/customer_db_server.py`)
6. Product Database backend (`src/backend/product_db_server.py`)

> Financial transactions / MakePurchase are intentionally **not implemented** in Assignment 1 (see Requirements).

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

## Protocol (length-prefixed JSON)
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

The frontends accept both the assignment’s CamelCase names and snake_case equivalents.
