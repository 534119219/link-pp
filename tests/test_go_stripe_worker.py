from __future__ import annotations

import json
from pathlib import Path

import pytest

from handoff.protocol import go_stripe_worker as bridge
from handoff.protocol import stripe_checkout as stripe


def test_default_worker_path_is_project_relative():
    project_root = Path(bridge.__file__).resolve().parents[2]
    assert Path(bridge.DEFAULT_WORKER_PATH) == project_root / "bin" / "stripe-worker"


def worker_args(log):
    return {
        "session_id": "cs_live_test",
        "publishable_key": "pk_live_test",
        "proxy_url": "socks5://user:pass@proxy.example:1080",
        "access_token": "eyJheader.eyJpayload.signature",
        "cookie_header": "oai-did=cookie-secret",
        "device_id": "device-test",
        "country": "DE",
        "currency": "EUR",
        "browser_locale": "de-DE",
        "browser_timezone": "Europe/Berlin",
        "processor_entity": "openai_ie",
        "checkout_url": "https://chatgpt.com/checkout/openai_ie/cs_live_test",
        "billing": {"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
        "approve_headers": {"OpenAI-Sentinel-Token": "sentinel-secret"},
        "apply_promo": True,
        "log": log,
    }


def test_bridge_returns_redirect_and_sanitizes_diagnostics(monkeypatch):
    secret_text = "eyJheader.eyJpayload.signature oai-did=cookie-secret sentinel-secret owner@example.com socks5://user:pass@proxy.example:1080"
    output = {
        "ok": True,
        "code": "paypal_redirect_extracted",
        "redirect_url": "https://pm-redirects.stripe.com/authorize/test",
        "diagnostics": [{"kind": "go_test", "method": "POST", "route": "/test", "response": {"debug": secret_text}}],
    }

    class Completed:
        returncode = 0
        stdout = json.dumps(output).encode()
        stderr = b""

    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: Completed())
    logs = []
    redirect = bridge.run_go_stripe_worker(**worker_args(logs.append))

    assert redirect.endswith("/test")
    serialized = "\n".join(logs)
    for secret in ("eyJheader", "cookie-secret", "sentinel-secret", "owner@example.com", "user:pass"):
        assert secret not in serialized


def test_bridge_maps_promo_failures_and_paypal_unavailable(monkeypatch):
    class Completed:
        returncode = 0
        stderr = b""

    completed = Completed()
    completed.stdout = json.dumps({"ok": False, "code": "non_zero_amount", "message": "due=100"}).encode()
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: completed)
    with pytest.raises(stripe.PromoNotAppliedError):
        bridge.run_go_stripe_worker(**worker_args(lambda _message: None))

    completed.stdout = json.dumps({"ok": False, "code": "promo_update_failed", "message": "HTTP 409"}).encode()
    with pytest.raises(stripe.PromoNotAppliedError):
        bridge.run_go_stripe_worker(**worker_args(lambda _message: None))

    completed.stdout = json.dumps({"ok": False, "code": "paypal_unavailable", "message": "methods=card"}).encode()
    with pytest.raises(stripe.PayPalFundingUnavailableError):
        bridge.run_go_stripe_worker(**worker_args(lambda _message: None))
