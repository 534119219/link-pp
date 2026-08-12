from __future__ import annotations

import base64
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_token(*, email: str = "owner@example.com", name: str = "Owner") -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = encode({"alg": "none", "typ": "JWT"})
    payload = encode(
        {
            "https://api.openai.com/profile": {"email": email, "name": name},
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-test"},
        }
    )
    return f"{header}.{payload}.test-signature"
