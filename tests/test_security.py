from __future__ import annotations

import json

import pytest

from conftest import make_token
from handoff.security import (
    normalize_access_token,
    sanitize_diagnostic_payload,
    sanitize_message,
    token_profile,
)


def test_extracts_billing_identity_without_verifying_or_persisting_token():
    token = make_token(email="billing@example.com", name="Billing Owner")
    profile = token_profile(f"Bearer {token}")
    assert profile.email == "billing@example.com"
    assert profile.name == "Billing Owner"
    assert normalize_access_token(f"Bearer {token}") == token
    assert token not in json.dumps(profile.__dict__ if hasattr(profile, "__dict__") else {"email": profile.email})


def test_sanitizer_removes_at_proxy_credentials_and_ba_token():
    token = make_token()
    message = (
        f"Bearer {token} socks5://user:secret@proxy.example:1080 "
        "https://paypal.com/agreements/approve?ba_token=BA-ABC123"
    )
    safe = sanitize_message(message, access_token=token)
    assert token not in safe
    assert "secret" not in safe
    assert "BA-ABC123" not in safe
    assert "[AT]" in safe


def test_sanitizer_removes_explicit_pix_tax_id_secret():
    safe = sanitize_message(
        "Stripe rejected billing_details[tax_id]=52998224725",
        secrets=("52998224725",),
    )
    assert "52998224725" not in safe
    assert "[SECRET]" in safe


def test_diagnostic_payload_preserves_structure_and_removes_sensitive_fields():
    payload = {
        "id": "ppage_test",
        "account": "acct_test",
        "client_secret": "seti_secret_value",
        "billing_details": {"email": "owner@example.com", "name": "Owner"},
        "next_action": {
            "redirect_to_url": {
                "url": (
                    "https://paypal.example/approve?ba_token=BA-SECRET123"
                    "&client_secret=query-secret"
                )
            }
        },
        "items": [{"status": "requires_approval"}],
    }
    sanitized = sanitize_diagnostic_payload(payload)
    serialized = json.dumps(sanitized)
    assert "ppage_test" in serialized
    assert "acct_test" in serialized
    assert "requires_approval" in serialized
    assert "seti_secret_value" not in serialized
    assert "owner@example.com" not in serialized
    assert "BA-SECRET123" not in serialized
    assert "query-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert set(sanitized["billing_details"]) == {"email", "name"}


@pytest.mark.parametrize("raw", ["", "not-a-jwt", "a.b.c"])
def test_rejects_invalid_tokens(raw):
    with pytest.raises(ValueError):
        token_profile(raw)
