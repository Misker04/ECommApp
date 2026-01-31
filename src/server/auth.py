from __future__ import annotations

import hashlib
import os
import secrets
from typing import Optional

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def new_session_token() -> str:
    return secrets.token_urlsafe(24)
