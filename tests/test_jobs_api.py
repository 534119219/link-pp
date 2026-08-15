from __future__ import annotations

import json
import threading
import time

from conftest import make_token
from handoff.app import create_app
from handoff.gateway import CheckoutArtifact, ProviderResult
from handoff.jobs import Job


class SuccessGateway:
    def __init__(self):
        self.checkout_calls = []

    def create_checkout(self, **kwargs):
        if not hasattr(self, "checkout_calls"):
            self.checkout_calls = []
        self.checkout_calls.append(kwargs)
        return CheckoutArtifact(
            session_id="oaics_success",
            processor_entity=kwargs["checkout_country"].processor_entity,
            country=kwargs["checkout_country"].code,
            currency=kwargs["checkout_country"].currency,
            checkout_url="https://chatgpt.com/checkout/openai_ie/oaics_success",
        )

    def attempt_provider(self, **_kwargs):
        return ProviderResult(
            provider_redirect_url="https://www.paypal.com/agreements/approve?ba_token=BA-SUCCESS",
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


class ProtocolDiagnosticGateway(SuccessGateway):
    def attempt_provider(self, **kwargs):
        kwargs["log"](
            "[protocol-diagnostic] "
            + json.dumps(
                {
                    "kind": "checkout_taxes",
                    "method": "POST",
                    "route": "/backend-api/payments/checkout/taxes",
                    "http_status": 200,
                    "request": {
                        "checkout_session_id": "oaics_success",
                        "checkout_email": "owner@example.com",
                        "billing_name": "Owner",
                        "billing_address": {
                            "line1": "private address",
                            "country": "DE",
                        },
                    },
                    "response": {
                        "checkout_session": {
                            "currency": "EUR",
                            "total_summary": {"due": 0},
                            "client_secret": "private-secret",
                        }
                    },
                    "response_headers": {"x-request-id": "req_fixture"},
                },
                separators=(",", ":"),
            )
        )
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


class NonzeroOaicsGateway(SuccessGateway):
    def create_checkout(self, **_kwargs):
        raise RuntimeError(
            "免费促销未实际生效 "
            "(session=oaics_nonzero, Checkout due=2300 EUR)"
        )


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


class ProxyDistributionGateway(SuccessGateway):
    def __init__(self):
        self._lock = threading.Lock()
        self.proxy_by_token = {}

    def create_checkout(self, **kwargs):
        with self._lock:
            self.proxy_by_token[kwargs["access_token"]] = kwargs["proxy"].host
        return super().create_checkout(**kwargs)


def payload(token):
    return {
        "access_token": token,
        "country": "BR",
        "proxy_scheme": "socks5",
        "proxies": "checkout.example:1000:user:pass",
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
    assert meta["defaults"]["country"] == "BR"
    assert meta["defaults"]["billing_country"] == "DE"
    assert meta["defaults"]["checkout_country"] == "DE"
    assert meta["defaults"]["batch_concurrency"] == 8
    assert meta["job_workers"] == 200
    assert "max_batch_concurrency" not in meta
    assert meta["defaults"]["stripe_checkout"] is False
    assert meta["defaults"]["stripe_engine"] == "python"
    assert meta["defaults"]["stripe_promo_strategy"] == "post_update"
    assert meta["stripe_engines"] == ["python", "go"]
    assert meta["stripe_promo_strategies"] == ["upfront", "post_update", "mixed"]
    brazil = next(item for item in meta["countries"] if item["code"] == "BR")
    assert brazil["checkout_country"] == "DE"
    assert brazil["checkout_currency"] == "EUR"
    vietnam = next(item for item in meta["countries"] if item["code"] == "VN")
    assert vietnam["currency"] == "VND"
    assert vietnam["checkout_country"] == "VN"
    assert vietnam["checkout_currency"] == "VND"
    assert "link_types" not in meta
    html = client.get("/").get_data(as_text=True)
    assert "PayPal 提链" in html
    assert 'id="ckSearch"' in html
    assert 'id="billingCode"' in html
    assert 'id="bBillingCode"' in html
    assert 'id="stripeCheckout"' in html
    assert 'id="bStripeCheckout"' in html
    assert 'id="stripeEngine"' in html
    assert 'id="bStripeEngine"' in html
    assert 'id="pmSearch"' not in html
    for forbidden in ("OTP", "Captcha", "手机号", "PIX", "协议支付", "PayPal User"):
        assert forbidden not in html
    app.extensions["job_manager"].shutdown()


def test_job_accepts_stripe_checkout_mode_and_rejects_non_boolean_value():
    token = make_token()
    gateway = SuccessGateway()
    app = create_app({"TESTING": True}, gateway=gateway)
    client = app.test_client()
    request = payload(token)
    request["stripe_checkout"] = True

    snapshot = wait_for_job(
        client,
        client.post("/api/jobs", json=request).get_json()["job_id"],
    )

    assert snapshot["status"] == "success"
    assert snapshot["config"]["stripe_checkout"] is True
    assert gateway.checkout_calls[0]["stripe_checkout"] is True
    request["stripe_checkout"] = {"unexpected": True}
    response = client.post("/api/jobs", json=request)
    assert response.status_code == 400
    assert response.get_json()["error"] == "stripe_checkout 必须是布尔值"
    app.extensions["job_manager"].shutdown()


def test_job_accepts_go_stripe_engine_and_rejects_invalid_combinations():
    token = make_token()
    gateway = SuccessGateway()
    app = create_app({"TESTING": True}, gateway=gateway)
    client = app.test_client()
    request = payload(token)
    request.update(stripe_checkout=True, stripe_engine="go")

    snapshot = wait_for_job(
        client,
        client.post("/api/jobs", json=request).get_json()["job_id"],
    )
    assert snapshot["config"]["stripe_engine"] == "go"
    assert gateway.checkout_calls[0]["stripe_engine"] == "go"

    request["stripe_checkout"] = False
    response = client.post("/api/jobs", json=request)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Go 引擎当前只支持 Stripe 链提炼"

    request.update(stripe_checkout=True, stripe_engine="rust")
    response = client.post("/api/jobs", json=request)
    assert response.status_code == 400
    assert response.get_json()["error"] == "stripe_engine 只支持 python 或 go"
    app.extensions["job_manager"].shutdown()


def test_job_accepts_and_validates_stripe_promo_strategy():
    token = make_token()
    gateway = SuccessGateway()
    app = create_app({"TESTING": True}, gateway=gateway)
    client = app.test_client()
    request = payload(token)
    request.update(stripe_checkout=True, stripe_promo_strategy="mixed")

    snapshot = wait_for_job(
        client,
        client.post("/api/jobs", json=request).get_json()["job_id"],
    )
    assert snapshot["config"]["stripe_promo_strategy"] == "mixed"
    assert gateway.checkout_calls[0]["stripe_promo_strategy"] == "mixed"

    request["stripe_promo_strategy"] = "unknown"
    response = client.post("/api/jobs", json=request)
    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "stripe_promo_strategy 只支持 upfront、post_update 或 mixed"
    )
    app.extensions["job_manager"].shutdown()


def test_job_allows_billing_country_independent_from_proxy_country():
    token = make_token()
    gateway = SuccessGateway()
    app = create_app({"TESTING": True}, gateway=gateway)
    client = app.test_client()
    request = payload(token)
    request["country"] = "BR"
    request["billing_country"] = "BR"

    snapshot = wait_for_job(
        client,
        client.post("/api/jobs", json=request).get_json()["job_id"],
    )

    assert snapshot["config"]["proxy_country"] == "BR"
    assert snapshot["config"]["billing_country"] == "BR"
    assert snapshot["config"]["checkout_country"] == "BR"
    assert snapshot["config"]["checkout_currency"] == "USD"
    assert gateway.checkout_calls[0]["proxy_country"].code == "BR"
    assert gateway.checkout_calls[0]["checkout_country"].code == "BR"
    assert gateway.checkout_calls[0]["billing"]["address"]["country"] == "BR"
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
    assert snapshot["result"]["provider_redirect_url"].endswith("BA-SUCCESS")
    assert snapshot["config"]["proxy_country"] == "BR"
    assert snapshot["config"]["checkout_country"] == "DE"
    assert snapshot["config"]["checkout_currency"] == "EUR"
    assert snapshot["result"]["proxy_country"] == "BR"
    assert snapshot["result"]["country"] == "DE"
    assert snapshot["result"]["currency"] == "EUR"
    for removed in ("payment_completed", "stripe_state", "paypal_user_id", "paypal_callback_url"):
        assert removed not in snapshot["result"]
    assert token not in serialized
    assert "checkout.example" not in serialized
    assert client.post(f"/api/jobs/{snapshot['id']}/otp", json={"otp": "123456"}).status_code == 404
    app.extensions["job_manager"].shutdown()


def test_job_exposes_distinct_oaics_nonzero_failure_reason():
    token = make_token()
    app = create_app({"TESTING": True}, gateway=NonzeroOaicsGateway())
    client = app.test_client()
    request = payload(token)
    request["checkout_attempts"] = 1
    snapshot = wait_for_job(
        client,
        client.post("/api/jobs", json=request).get_json()["job_id"],
    )

    assert snapshot["status"] == "failed"
    assert snapshot["failure_reason"] == (
        "已生成 OAICS，但前置优惠未生效（应付金额非 0）"
    )
    assert "普通 Stripe Checkout" not in snapshot["failure_reason"]
    app.extensions["job_manager"].shutdown()


def test_batch_results_use_paypal_approve_url_and_full_accounts():
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
    assert snapshot["jobs"][0]["label"] == "#001 · first.owner@example.com"
    csv_text = client.get(f"/api/batches/{snapshot['id']}/results.csv").get_data(as_text=True)
    assert "BA-SUCCESS" in csv_text
    assert "first.owner@example.com" in csv_text
    assert "second.owner@example.com" in csv_text
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


def test_batch_concurrency_above_twenty_is_not_capped():
    tokens = [make_token(email=f"worker{index}@example.com") for index in range(25)]
    gateway = SlowGateway()
    app = create_app({"TESTING": True, "JOB_WORKERS": 25}, gateway=gateway)
    client = app.test_client()
    request = payload(tokens[0])
    request.pop("access_token")
    request.update({"access_tokens": tokens, "concurrency": 25})
    batch_id = client.post("/api/batches", json=request).get_json()["id"]
    wait_for_batch(client, batch_id)
    assert gateway.max_active == 25
    request["concurrency"] = 0
    assert client.post("/api/batches", json=request).status_code == 400
    app.extensions["job_manager"].shutdown()


def test_batch_distributes_first_checkout_across_proxy_pool():
    tokens = [make_token(email=f"proxy{index}@example.com") for index in range(6)]
    gateway = ProxyDistributionGateway()
    app = create_app({"TESTING": True, "JOB_WORKERS": 6}, gateway=gateway)
    client = app.test_client()
    request = payload(tokens[0])
    request.pop("access_token")
    request.update(
        {
            "access_tokens": tokens,
            "concurrency": 6,
            "proxies": "a.example:1001\nb.example:1002\nc.example:1003",
        }
    )

    batch_id = client.post("/api/batches", json=request).get_json()["id"]
    wait_for_batch(client, batch_id)

    assert [gateway.proxy_by_token[token] for token in tokens] == [
        "a.example",
        "b.example",
        "c.example",
        "a.example",
        "b.example",
        "c.example",
    ]
    app.extensions["job_manager"].shutdown()


def test_compact_batch_snapshot_and_unchanged_revision_response():
    tokens = [make_token(email=f"compact{index}@example.com") for index in range(4)]
    app = create_app({"TESTING": True, "JOB_WORKERS": 4}, gateway=SuccessGateway())
    client = app.test_client()
    request = payload(tokens[0])
    request.pop("access_token")
    request.update({"access_tokens": tokens, "concurrency": 4})
    batch_id = client.post("/api/batches", json=request).get_json()["id"]
    wait_for_batch(client, batch_id)

    compact_response = client.get(f"/api/batches/{batch_id}?compact=1")
    compact = compact_response.get_json()
    assert compact["status"] == "success"
    assert compact["revision"] > 0
    assert len(compact["jobs"]) == 4
    assert set(compact["jobs"][0]) == {
        "attempt",
        "attempt_count",
        "batch_index",
        "failure_reason",
        "id",
        "label",
        "result_url",
        "status",
    }
    assert compact["jobs"][0]["result_url"].endswith("BA-SUCCESS")

    unchanged_response = client.get(
        f"/api/batches/{batch_id}?compact=1&after_revision={compact['revision']}"
    )
    assert unchanged_response.get_json() == {
        "id": batch_id,
        "revision": compact["revision"],
        "unchanged": True,
    }
    assert len(unchanged_response.data) < 120
    assert client.get(
        f"/api/batches/{batch_id}?after_revision=invalid"
    ).status_code == 400
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


def test_protocol_diagnostic_is_backend_only_structured_and_sanitized(tmp_path):
    token = make_token()
    app = create_app(
        {"TESTING": True, "DIAGNOSTIC_DIR": str(tmp_path)},
        gateway=ProtocolDiagnosticGateway(),
    )
    client = app.test_client()
    job_id = client.post("/api/jobs", json=payload(token)).get_json()["job_id"]
    snapshot = wait_for_job(client, job_id)
    assert snapshot["status"] == "success"

    stream = client.get(f"/api/jobs/{job_id}/events?after=0").get_data(as_text=True)
    assert "protocol-diagnostic" not in stream
    assert "private address" not in stream
    records = [
        json.loads(line)
        for line in (tmp_path / f"{job_id}.jsonl").read_text().splitlines()
    ]
    record = next(item for item in records if item["kind"] == "checkout_taxes")
    response = record["response"]
    serialized = json.dumps(record)
    assert response["http_status"] == 200
    assert response["response_headers"]["x-request-id"] == "req_fixture"
    assert response["request"]["checkout_email"] == "[REDACTED]"
    assert response["request"]["billing_name"] == "[REDACTED]"
    assert set(response["request"]["billing_address"].values()) == {"[REDACTED]"}
    assert response["response"]["checkout_session"]["total_summary"]["due"] == 0
    assert "owner@example.com" not in serialized
    assert "private-secret" not in serialized
    assert token not in serialized
    app.extensions["job_manager"].shutdown()


def test_api_rejects_bad_country_proxy_and_removed_payment_fields_are_ignored():
    token = make_token()
    app = create_app({"TESTING": True}, gateway=SuccessGateway())
    client = app.test_client()
    bad_country = payload(token)
    bad_country["country"] = "XX"
    assert client.post("/api/jobs", json=bad_country).status_code == 400
    bad_billing_country = payload(token)
    bad_billing_country["billing_country"] = "XX"
    assert client.post("/api/jobs", json=bad_billing_country).status_code == 400
    bad_proxy = payload(token)
    bad_proxy["proxies"] = "host:badport:user:very-secret"
    response = client.post("/api/jobs", json=bad_proxy)
    assert response.status_code == 400
    assert "very-secret" not in response.get_data(as_text=True)
    legacy = payload(token)
    legacy.update({"link_type": "pix", "paypal_phone": "+491234", "otp_timeout": 30})
    created = client.post("/api/jobs", json=legacy)
    result = wait_for_job(client, created.get_json()["job_id"])["result"]
    assert "link_type" not in result
    app.extensions["job_manager"].shutdown()
