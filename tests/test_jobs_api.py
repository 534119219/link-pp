from __future__ import annotations

import json
import threading
import time

from conftest import make_token
from handoff.app import create_app
from handoff.gateway import CheckoutArtifact, ProviderResult
from handoff.jobs import Job


class SuccessGateway:
    def create_checkout(self, **kwargs):
        return CheckoutArtifact(
            session_id="cs_live_success",
            processor_entity="openai_llc",
            checkout_country=kwargs["country"].code,
            currency=kwargs["country"].currency,
            checkout_url="https://chatgpt.com/checkout/openai_llc/cs_live_success",
        )

    def attempt_provider(self, **_kwargs):
        return ProviderResult(
            stripe_redirect_url="https://pm-redirects.stripe.com/authorize/success",
            paypal_approve_url="https://www.paypal.com/agreements/approve?ba_token=BA-SUCCESS",
            ba_token="BA-SUCCESS",
        )


class DiagnosticGateway(SuccessGateway):
    def attempt_provider(self, **kwargs):
        response = {
            "id": "ppage_test",
            "client_secret": "must-not-be-written",
            "submission_attempt": {"state": "requires_approval"},
            "status": "open",
            "diagnostic_padding": "x" * 2000,
        }
        kwargs["log"]("confirm 完整响应（已脱敏）: " + json.dumps(response))
        return super().attempt_provider(**kwargs)


class RetryGateway(SuccessGateway):
    def __init__(self):
        self._lock = threading.Lock()
        self.calls = {}

    def create_checkout(self, **kwargs):
        token = kwargs["access_token"]
        with self._lock:
            count = self.calls.get(token, 0) + 1
            self.calls[token] = count
        if count == 1:
            raise RuntimeError("temporary failure")
        return super().create_checkout(**kwargs)


class SlowGateway(SuccessGateway):
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def create_checkout(self, **kwargs):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return super().create_checkout(**kwargs)
        finally:
            with self._lock:
                self.active -= 1


def payload(token):
    return {
        "access_token": token,
        "checkout_country": "BR",
        "promo_country": "DE",
        "checkout_proxy_scheme": "socks5",
        "promo_proxy_scheme": "socks5",
        "checkout_proxies": "checkout.example:1000:user:pass",
        "promo_proxies": "promo.example:2000:user:pass",
        "checkout_attempts": 2,
        "provider_attempts": 2,
    }


def wait_for_job(client, job_id):
    for _ in range(200):
        data = client.get(f"/api/jobs/{job_id}").get_json()
        if data["status"] in {"success", "failed", "cancelled"}:
            return data
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def wait_for_batch(client, batch_id):
    for _ in range(300):
        data = client.get(f"/api/batches/{batch_id}").get_json()
        if data["status"] in {"success", "failed", "cancelled", "partial"}:
            return data
        time.sleep(0.01)
    raise AssertionError("batch did not finish")


def test_meta_and_frontend_only_expose_link_extraction():
    app = create_app({"TESTING": True}, gateway=SuccessGateway())
    client = app.test_client()
    meta = client.get("/api/meta").get_json()
    assert meta["defaults"]["checkout_attempts"] == 5
    assert meta["defaults"]["provider_attempts"] == 10
    assert "link_types" not in meta
    html = client.get("/").get_data(as_text=True)
    assert "双国家 PayPal 提链" in html
    assert 'id="ckSearch"' in html
    assert 'id="pmSearch"' in html
    for forbidden in ("OTP", "Captcha", "手机号", "PIX", "协议支付", "PayPal User"):
        assert forbidden not in html
    app.extensions["job_manager"].shutdown()


def test_job_returns_links_without_payment_fields_or_secrets():
    token = make_token()
    app = create_app({"TESTING": True}, gateway=SuccessGateway())
    client = app.test_client()
    created = client.post("/api/jobs", json=payload(token))
    assert created.status_code == 202
    snapshot = wait_for_job(client, created.get_json()["job_id"])
    serialized = json.dumps(snapshot)
    assert snapshot["status"] == "success"
    assert snapshot["result"]["paypal_approve_url"].endswith("BA-SUCCESS")
    assert snapshot["result"]["stripe_redirect_url"].endswith("/success")
    for removed in ("payment_completed", "stripe_state", "paypal_user_id", "paypal_callback_url"):
        assert removed not in snapshot["result"]
    assert token not in serialized
    assert "checkout.example" not in serialized
    assert client.post(f"/api/jobs/{snapshot['id']}/otp", json={"otp": "123456"}).status_code == 404
    app.extensions["job_manager"].shutdown()


def test_batch_results_use_paypal_approve_url_and_mask_accounts():
    first = make_token(email="first.owner@example.com")
    second = make_token(email="second.owner@example.com")
    app = create_app({"TESTING": True, "JOB_WORKERS": 2}, gateway=SuccessGateway())
    client = app.test_client()
    request = payload(first)
    request.pop("access_token")
    request.update({"access_tokens": f"{first}\n{second}\n{first}", "batch_name": "提链批次"})
    created = client.post("/api/batches", json=request)
    assert created.status_code == 202
    assert created.get_json()["duplicate_count"] == 1
    snapshot = wait_for_batch(client, created.get_json()["id"])
    assert snapshot["counts"]["success"] == 2
    assert snapshot["jobs"][0]["label"].startswith("#001 · fi***@")
    csv_text = client.get(f"/api/batches/{snapshot['id']}/results.csv").get_data(as_text=True)
    assert "BA-SUCCESS" in csv_text
    assert "支付类型" not in csv_text
    assert first not in csv_text and second not in csv_text
    app.extensions["job_manager"].shutdown()


def test_batch_failed_items_retry_without_resubmitting_tokens():
    tokens = [make_token(email=f"retry{index}@example.com") for index in range(2)]
    app = create_app({"TESTING": True, "JOB_WORKERS": 2}, gateway=RetryGateway())
    client = app.test_client()
    request = payload(tokens[0])
    request.pop("access_token")
    request.update({"access_tokens": tokens, "checkout_attempts": 1, "provider_attempts": 1})
    batch_id = client.post("/api/batches", json=request).get_json()["id"]
    failed = wait_for_batch(client, batch_id)
    assert failed["status"] == "failed"
    retried = client.post(f"/api/batches/{batch_id}/retry")
    assert retried.status_code == 202
    succeeded = wait_for_batch(client, batch_id)
    assert succeeded["status"] == "success"
    assert all(job["attempt"] == 2 for job in succeeded["jobs"])
    app.extensions["job_manager"].shutdown()


def test_batch_concurrency_limit_is_enforced():
    tokens = [make_token(email=f"worker{index}@example.com") for index in range(5)]
    gateway = SlowGateway()
    app = create_app({"TESTING": True, "JOB_WORKERS": 5}, gateway=gateway)
    client = app.test_client()
    request = payload(tokens[0])
    request.pop("access_token")
    request.update({"access_tokens": tokens, "concurrency": 2})
    batch_id = client.post("/api/batches", json=request).get_json()["id"]
    wait_for_batch(client, batch_id)
    assert gateway.max_active == 2
    request["concurrency"] = 21
    assert client.post("/api/batches", json=request).status_code == 400
    app.extensions["job_manager"].shutdown()


def test_confirm_diagnostic_is_sanitized_and_saved(tmp_path):
    token = make_token()
    app = create_app(
        {"TESTING": True, "DIAGNOSTIC_DIR": str(tmp_path)},
        gateway=DiagnosticGateway(),
    )
    client = app.test_client()
    job_id = client.post("/api/jobs", json=payload(token)).get_json()["job_id"]
    snapshot = wait_for_job(client, job_id)
    assert snapshot["status"] == "success"
    stream = client.get(f"/api/jobs/{job_id}/events?after=0").get_data(as_text=True)
    assert "confirm 完整响应" not in stream
    assert "diagnostic_padding" not in stream
    records = [
        json.loads(line)
        for line in (tmp_path / f"{job_id}.jsonl").read_text().splitlines()
    ]
    record = next(item for item in records if item["kind"] == "confirm")
    full_log = next(
        item for item in records
        if item["kind"] == "log" and "confirm 完整响应" in item["message"]
    )
    assert record["response"]["client_secret"] == "[REDACTED]"
    assert record["response"]["diagnostic_padding"] == "x" * 2000
    assert "x" * 2000 in full_log["message"]
    assert "must-not-be-written" not in full_log["message"]
    assert token not in json.dumps(record)
    app.extensions["job_manager"].shutdown()


def test_confirm_diagnostic_write_failure_does_not_fail_extraction(monkeypatch, tmp_path):
    token = make_token()
    monkeypatch.setattr(
        Job,
        "_append_diagnostic",
        lambda self, **_kwargs: (_ for _ in ()).throw(
            OSError(30, "Read-only file system")
        ),
    )
    app = create_app(
        {"TESTING": True, "DIAGNOSTIC_DIR": str(tmp_path)},
        gateway=DiagnosticGateway(),
    )
    manager = app.extensions["job_manager"]
    client = app.test_client()
    job_id = client.post("/api/jobs", json=payload(token)).get_json()["job_id"]
    snapshot = wait_for_job(client, job_id)
    assert snapshot["status"] == "success"
    stream = client.get(f"/api/jobs/{job_id}/events?after=0").get_data(as_text=True)
    assert "confirm 完整响应" not in stream
    records = [
        json.loads(line)
        for line in (tmp_path / f"{job_id}.jsonl").read_text().splitlines()
    ]
    assert any(item["kind"] == "log" for item in records)
    assert not any(item["kind"] == "confirm" for item in records)
    manager.shutdown()


def test_api_rejects_bad_country_proxy_and_removed_payment_fields_are_ignored():
    token = make_token()
    app = create_app({"TESTING": True}, gateway=SuccessGateway())
    client = app.test_client()
    bad_country = payload(token)
    bad_country["checkout_country"] = "XX"
    assert client.post("/api/jobs", json=bad_country).status_code == 400
    bad_proxy = payload(token)
    bad_proxy["promo_proxies"] = "host:badport:user:very-secret"
    response = client.post("/api/jobs", json=bad_proxy)
    assert response.status_code == 400
    assert "very-secret" not in response.get_data(as_text=True)
    legacy = payload(token)
    legacy.update({"link_type": "pix", "paypal_phone": "+491234", "otp_timeout": 30})
    created = client.post("/api/jobs", json=legacy)
    result = wait_for_job(client, created.get_json()["job_id"])["result"]
    assert "link_type" not in result
    app.extensions["job_manager"].shutdown()
