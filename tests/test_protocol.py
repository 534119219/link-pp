from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from handoff import gateway as gateway_module
from handoff.countries import get_country
from handoff.gateway import (
    CheckoutArtifact,
    LiveProtocolGateway,
    resolve_paypal_approval_url,
)
from handoff.proxies import parse_proxy_lines
from handoff.protocol import stripe_checkout as stripe


@pytest.mark.parametrize(
    "field",
    (
        "stripe_publishable_key",
        "publishable_key",
        "publishableKey",
        "stripePublishableKey",
        "key",
    ),
)
def test_checkout_response_publishable_key_is_captured(monkeypatch, field):
    publishable_key = "pk_live_dynamicShard123"

    class Response:
        status_code = 200
        text = json.dumps(
            {
                "checkout_session_id": "cs_live_dynamic",
                "processor_entity": "openai_llc",
                field: publishable_key,
            }
        )

    class Http:
        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(stripe, "_warmup_chatgpt_page", lambda *_args, **_kwargs: None)
    context = {"stale": "value"}
    session_id, error = stripe.create_chatgpt_order(
        Http(),
        "test-at",
        checkout_context=context,
    )

    assert error is None
    assert session_id == "cs_live_dynamic"
    assert context["publishable_key"] == publishable_key
    assert "stale" not in context


def test_stripe_flow_uses_supplied_publishable_key_without_probe(monkeypatch):
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "usd"}
    monkeypatch.setattr(
        stripe,
        "verify_pk",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fixed-key probe must not run")),
    )
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda _http, _session, pk, *_args: calls.append(("init", pk))
        or ({"total_summary": {"due": 0}}, "version", context),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda _http, pk, *_args: calls.append(("elements", pk)) or {},
    )
    monkeypatch.setattr(stripe, "update_tax_region", lambda *_args: {})
    monkeypatch.setattr(stripe, "snapshot_billing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda _http, pk, *_args: calls.append(("confirm", pk))
        or {
            "next_action": {
                "redirect_to_url": {
                    "url": "https://pm-redirects.stripe.com/authorize/dynamic"
                }
            }
        },
    )

    redirect, returned_key, _context = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_live_dynamic",
        billing={"name": "Owner", "address": {"country": "US"}},
        publishable_key="pk_live_dynamicShard123",
    )

    assert redirect.endswith("/dynamic")
    assert returned_key == "pk_live_dynamicShard123"
    assert calls == [
        ("init", "pk_live_dynamicShard123"),
        ("elements", "pk_live_dynamicShard123"),
        ("confirm", "pk_live_dynamicShard123"),
    ]


def test_gateway_keeps_checkout_publishable_key_through_zero_check(monkeypatch):
    calls = []

    class Http:
        def close(self):
            calls.append("close")

    monkeypatch.setattr(stripe, "build_http", lambda _proxy: Http())
    monkeypatch.setattr(gateway_module, "preflight_checkout_route", lambda **_kwargs: None)

    def create_order(*_args, **kwargs):
        kwargs["checkout_context"].update(
            {
                "processor_entity": "openai_llc",
                "publishable_key": "pk_live_dynamicShard123",
            }
        )
        return "cs_live_dynamic", None

    monkeypatch.setattr(stripe, "create_chatgpt_order_with_retry", create_order)
    monkeypatch.setattr(
        stripe,
        "verify_pk",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fixed-key probe must not run")),
    )
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda _http, _session, pk, *_args: calls.append(("init", pk)) or ({}, "version", {}),
    )
    monkeypatch.setattr(stripe, "update_chatgpt_checkout_promotion", lambda *_args, **_kwargs: {})

    def verify_zero(*_args, **kwargs):
        calls.append(("zero", kwargs["publishable_key"]))

    monkeypatch.setattr(stripe, "verify_promo_checkout_zero", verify_zero)
    artifact = LiveProtocolGateway().create_checkout(
        access_token="test-at",
        country=get_country("US"),
        proxy=parse_proxy_lines("checkout.example:1000")[0],
        device_id="device-test",
        promo_proxy=parse_proxy_lines("promo.example:2000")[0],
        promo_country=get_country("JP"),
        log=lambda _message: None,
    )

    assert artifact.publishable_key == "pk_live_dynamicShard123"
    assert calls[:2] == [
        ("init", "pk_live_dynamicShard123"),
        ("zero", "pk_live_dynamicShard123"),
    ]
    artifact.close_transport()


def test_explicit_card_only_checkout_stops_before_confirm_or_approve(monkeypatch):
    calls = []
    context = {
        "payment_method_types": ["card"],
        "checkout_amount": 0,
        "currency": "eur",
    }
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: (
            {"total_summary": {"due": 0}, "payment_method_types": ["card"]},
            "version",
            context,
        ),
    )
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: (_ for _ in ()).throw(AssertionError("confirm must not run")),
    )
    monkeypatch.setattr(
        stripe,
        "approve_submission",
        lambda *_args, **_kwargs: calls.append("approve") or {"result": "approved"},
    )
    with pytest.raises(stripe.PayPalFundingUnavailableError, match="未提供 PayPal"):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_test",
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            country="DE",
            processor_entity="openai_ie",
            chatgpt_http=object(),
            access_token="test-at",
        )
    assert calls == []


def test_approve_blocked_raises_immediately(monkeypatch):
    """When approve returns blocked, raise immediately without polling."""
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(stripe, "init_checkout", lambda *_args: ({"total_summary": {"due": 0}}, "version", context))
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: {"submission_attempt": {"state": "requires_approval"}},
    )
    monkeypatch.setattr(
        stripe,
        "approve_submission",
        lambda *_args, **_kwargs: calls.append("approve") or {"result": "blocked"},
    )
    monkeypatch.setattr(
        stripe,
        "poll_redirect_after_approve",
        lambda *_args, **_kwargs: calls.append("poll") or "",
    )
    with pytest.raises(RuntimeError, match="blocked"):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_test",
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            country="DE",
            processor_entity="openai_ie",
            chatgpt_http=object(),
            access_token="test-at",
        )
    # Should NOT have polled — blocked means immediate failure
    assert calls == ["approve"]


def test_approve_blocked_error_message_contains_blocked(monkeypatch):
    """Verify the error message is descriptive when approve is blocked."""
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(stripe, "init_checkout", lambda *_args: ({"total_summary": {"due": 0}}, "version", context))
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: {"submission_attempt": {"state": "requires_approval"}},
    )
    monkeypatch.setattr(
        stripe,
        "approve_submission",
        lambda *_args, **_kwargs: {"result": "blocked"},
    )
    with pytest.raises(RuntimeError, match="blocked"):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_test",
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            country="DE",
            processor_entity="openai_ie",
            chatgpt_http=object(),
            access_token="test-at",
        )


def test_approval_sentinel_is_prefetched_and_reconfirm_runs_before_poll(monkeypatch):
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: calls.append("init")
        or (
            {"total_summary": {"due": 0}, "payment_method_types": ["paypal"]},
            "version",
            context,
        ),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda *_args: calls.append("elements")
        or {"payment_method_specs": [{"type": "paypal"}]},
    )
    monkeypatch.setattr(stripe, "update_tax_region", lambda *_args: calls.append("tax"))
    monkeypatch.setattr(
        stripe,
        "snapshot_billing",
        lambda *_args, **_kwargs: calls.append("snapshot"),
    )
    monkeypatch.setattr(
        stripe,
        "prepare_approve_sentinel_headers",
        lambda *_args, **_kwargs: calls.append("prepare")
        or {"OpenAI-Sentinel-Token": "prefetched", "OAI-Telemetry": "[1,null]"},
    )
    confirms = [
        {"submission_attempt": {"state": "requires_approval"}},
        {
            "next_action": {
                "redirect_to_url": {
                    "url": "https://pm-redirects.stripe.com/authorize/reconfirm"
                }
            }
        },
    ]

    def confirm(*_args):
        calls.append("confirm")
        return confirms.pop(0)

    monkeypatch.setattr(stripe, "confirm_payment", confirm)

    def approve(*_args, **kwargs):
        calls.append("approve")
        assert kwargs["prepared_headers"]["OpenAI-Sentinel-Token"] == "prefetched"
        return {"result": "approved"}

    monkeypatch.setattr(stripe, "approve_submission", approve)
    monkeypatch.setattr(
        stripe,
        "poll_redirect_after_approve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("poll must not run before successful re-confirm")
        ),
    )

    redirect, _pk, _ctx = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_live_test",
        billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
        country="DE",
        processor_entity="openai_ie",
        chatgpt_http=object(),
        access_token="test-at",
        device_id="device-test",
        sentinel_proxy="socks5h://proxy.test:1080",
    )

    assert redirect.endswith("/reconfirm")
    assert calls == [
        "init",
        "elements",
        "tax",
        "snapshot",
        "prepare",
        "confirm",
        "approve",
        "confirm",
    ]


def test_approve_uses_telemetry_and_session_aligned_sentinel_context(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = '{"result":"approved"}'

        def json(self):
            return {"result": "approved"}

    class Http:
        def post(self, _url, **kwargs):
            captured["headers"] = kwargs["headers"]
            return Response()

    monkeypatch.setattr(
        stripe,
        "_warmup_chatgpt_page",
        lambda *_args, **kwargs: captured.setdefault("page_url", kwargs["page_url"]),
    )
    monkeypatch.setattr(
        stripe,
        "_session_cookie_header",
        lambda _http: "oai-did=device-test; __cf_bm=cf-test",
    )

    def mint(flow, context, _log):
        captured["flow"] = flow
        captured["context"] = context
        return "sentinel-main", "sentinel-so"

    monkeypatch.setattr(stripe, "_mint_sentinel", mint)
    result = stripe.approve_submission(
        Http(),
        "test-at",
        "cs_live_test",
        "openai_llc",
        lambda _message: None,
        device_id="device-test",
        sentinel_proxy="socks5h://proxy.test:1080",
        country="US",
    )

    assert result == {"result": "approved"}
    assert captured["page_url"].endswith("/openai_llc/cs_live_test")
    assert captured["flow"] == "checkout_session_approval"
    assert captured["context"].device_id == "device-test"
    assert captured["context"].country == "US"
    assert captured["context"].page_url == captured["page_url"]
    assert "__cf_bm=cf-test" in captured["context"].cookie_header
    assert captured["headers"]["OAI-Telemetry"] == "[1,null]"
    assert captured["headers"]["OpenAI-Sentinel-Token"] == "sentinel-main"
    assert captured["headers"]["OpenAI-Sentinel-So-Token"] == "sentinel-so"


def test_promo_update_retries_transient_503_without_changing_concurrency(monkeypatch):
    statuses = [503, 429, 200]
    sleeps = []
    logs = []

    class Response:
        text = "temporary"

        def __init__(self, status):
            self.status_code = status

        def json(self):
            return {"success": True}

    class Http:
        def post(self, *_args, **_kwargs):
            return Response(statuses.pop(0))

    monkeypatch.setattr(stripe.random, "uniform", lambda *_args: 0.2)
    monkeypatch.setattr(stripe.time, "sleep", sleeps.append)
    result = stripe.update_chatgpt_checkout_promotion(
        Http(),
        "test-at",
        "cs_live_test",
        processor_entity="openai_llc",
        country="US",
        log=logs.append,
    )

    assert result == {"success": True}
    assert sleeps == [1.7, 3.2]
    assert len([message for message in logs if "临时限流" in message]) == 2


def test_approve_poll_stops_immediately_on_current_paypal_generic_decline(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "setup_intent": {
                    "last_setup_error": {
                        "code": "setup_attempt_failed",
                        "decline_code": "generic_decline",
                        "payment_method": {"id": "pm_current", "type": "paypal"},
                    }
                }
            }

    class Http:
        def get(self, *_args, **_kwargs):
            calls.append("get")
            return Response()

    monkeypatch.setattr(stripe.time, "sleep", lambda _seconds: None)
    with pytest.raises(stripe.PayPalRiskDeclinedError, match="generic_decline"):
        stripe.poll_redirect_after_approve(
            Http(),
            "pk_test",
            "cs_live_test",
            lambda _message: None,
            payment_method_id="pm_current",
        )
    assert calls == ["get"]


def test_stale_generic_decline_from_previous_payment_method_is_ignored(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "submission_attempt": {"state": "requires_approval"},
                "setup_intent": {
                    "last_setup_error": {
                        "code": "setup_attempt_failed",
                        "decline_code": "generic_decline",
                        "payment_method": {"id": "pm_previous", "type": "paypal"},
                    }
                },
            }

    class Http:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(stripe.time, "sleep", lambda _seconds: None)
    assert (
        stripe.poll_redirect_after_approve(
            Http(),
            "pk_test",
            "cs_live_test",
            lambda _message: None,
            max_attempts=1,
            payment_method_id="pm_current",
        )
        == ""
    )


def test_paypal_confirm_logs_complete_redacted_response(monkeypatch):
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    confirm_data = {
        "id": "ppage_test",
        "object": "checkout.session",
        "account": "acct_new_shard",
        "client_secret": "seti_secret_value",
        "billing_details": {"email": "owner@example.com", "name": "Owner"},
        "diagnostic_padding": "x" * 1200,
        "next_action": {
            "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/test"}
        },
    }
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: ({"total_summary": {"due": 0}}, "version", context),
    )
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(stripe, "create_paypal_payment_method", lambda *_args: "pm_test")
    monkeypatch.setattr(stripe, "confirm_payment", lambda *_args: confirm_data)
    logs = []
    redirect, _pk, _context = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_live_test",
        billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
        country="DE",
        processor_entity="openai_ie",
        access_token="test-at",
        log=logs.append,
    )
    diagnostic = next(item for item in logs if "confirm 完整响应（已脱敏）" in item)
    assert redirect.endswith("/test")
    assert len(diagnostic) > 1200
    assert "ppage_test" in diagnostic
    assert "acct_new_shard" in diagnostic
    assert "x" * 1200 in diagnostic
    assert "seti_secret_value" not in diagnostic
    assert "owner@example.com" not in diagnostic


def test_resolves_pm_redirect_to_paypal_ba_url():
    responses = [
        SimpleNamespace(headers={"location": "https://www.paypal.com/agreements/approve?ba_token=BA-ABC123"}, text="")
    ]

    class Http:
        def get(self, *_args, **_kwargs):
            return responses.pop(0)

    approval, token = resolve_paypal_approval_url(
        Http(), "https://pm-redirects.stripe.com/authorize/test"
    )
    assert token == "BA-ABC123"
    assert approval.endswith("ba_token=BA-ABC123")


def test_paypal_confirm_uses_inline_payment_method_data():
    captured = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"next_action": {}}

    class Http:
        def post(self, url, **kwargs):
            captured.append((url, kwargs["data"]))
            return Response()

    billing = {
        "name": "Owner",
        "email": "owner@example.com",
        "address": {
            "country": "DE",
            "line1": "Friedrichstrasse 10",
            "city": "Berlin",
            "postal_code": "10117",
            "state": "BE",
        },
    }
    base_context = {
        "billing": billing,
        "checkout_amount": 0,
        "currency": "eur",
        "stripe_js_id": "stripe-js",
        "client_session_id": "client-session",
        "elements_session_id": "elements-session",
        "elements_session_config_id": "elements-config",
        "config_id": "checkout-config",
        "processor_entity": "openai_ie",
        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test",
        "return_url": (
            "https://chatgpt.com/checkout/verify?stripe_session_id=cs_test"
            "&processor_entity=openai_ie&plan_type=plus"
        ),
        "guid": "guid",
        "muid": "muid",
        "sid": "sid",
    }
    init_data = {"total_summary": {"due": 0}}

    stripe.confirm_payment(
        Http(),
        "pk_test",
        "cs_test",
        "pm_created_but_not_referenced",
        init_data,
        "version",
        dict(base_context),
        stripe._profile("DE"),
        lambda _message: None,
    )
    paypal_data = captured[-1][1]
    assert paypal_data["eid"] == "NA"
    assert paypal_data["client_attribution_metadata[client_session_id]"] == "client-session"
    assert paypal_data["client_attribution_metadata[merchant_integration_version]"] == "custom_checkout"
    assert paypal_data["link_brand"] == "link"
    assert paypal_data["consent[terms_of_service]"] == "accepted"
    assert paypal_data["return_url"].startswith("https://pay.openai.com/c/pay/cs_test?")
    assert "success_return_url=" in paypal_data["return_url"]
    assert paypal_data["payment_method_data[type]"] == "paypal"
    assert paypal_data["payment_method_data[billing_details][address][country]"] == "DE"
    assert (
        paypal_data[
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]"
        ]
        == "expressCheckout"
    )
    assert "payment_method" not in paypal_data

def test_paypal_stripe_flow_updates_tax_and_snapshot_before_confirm(monkeypatch):
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: calls.append("verify") or "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: calls.append("init")
        or ({"total_summary": {"due": 0}}, "version", context),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda *_args: calls.append("elements") or {},
    )
    monkeypatch.setattr(
        stripe,
        "update_tax_region",
        lambda *_args: calls.append("tax") or {},
    )
    monkeypatch.setattr(
        stripe,
        "snapshot_billing",
        lambda *_args, **_kwargs: calls.append("snapshot"),
    )
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: calls.append("confirm")
        or {
            "next_action": {
                "redirect_to_url": {
                    "url": "https://pm-redirects.stripe.com/authorize/test"
                }
            }
        },
    )

    redirect, _pk, _context = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_test",
        billing={"name": "Owner", "address": {"country": "DE"}},
        country="DE",
        processor_entity="openai_ie",
        require_zero_amount=True,
        chatgpt_http=object(),
        access_token="test-at",
    )

    assert redirect.endswith("/test")
    assert calls == [
        "verify",
        "init",
        "elements",
        "tax",
        "snapshot",
        "confirm",
    ]


def test_gateway_stops_after_extracting_paypal_ba_link(monkeypatch):
    calls = []

    class Http:
        def close(self):
            calls.append("close")

    http = Http()
    monkeypatch.setattr(stripe, "build_http", lambda _proxy: calls.append("build") or http)
    monkeypatch.setattr(
        stripe,
        "verify_proxy_exit_country",
        lambda *_args, **_kwargs: {
            "ip": "203.0.113.10",
            "country": "DE",
            "source": "test",
        },
    )
    monkeypatch.setattr(stripe, "verify_chatgpt_account", lambda *_args, **_kwargs: {})
    def stripe_confirm(*_args, **kwargs):
        assert kwargs["publishable_key"] == "pk_live_dynamicShard123"
        calls.append("stripe_confirm")
        return (
            "https://pm-redirects.stripe.com/authorize/test",
            "pk_test",
            {"payment_method_types": ["paypal"]},
        )

    monkeypatch.setattr(stripe, "stripe_to_paypal_redirect", stripe_confirm)
    monkeypatch.setattr(
        gateway_module,
        "resolve_paypal_approval_url",
        lambda *_args, **_kwargs: calls.append("resolve_ba")
        or (
            "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
            "BA-TEST",
        ),
    )

    result = LiveProtocolGateway().attempt_provider(
        artifact=CheckoutArtifact(
            session_id="cs_test",
            processor_entity="openai_ie",
            checkout_country="DE",
            currency="EUR",
            checkout_url="https://chatgpt.com/checkout/openai_ie/cs_test",
            amount=0,
            publishable_key="pk_live_dynamicShard123",
        ),
        access_token="test-at",
        country=get_country("DE"),
        billing={"name": "Owner", "address": {"country": "DE"}},
        proxy=parse_proxy_lines("proxy.example:1000")[0],
        device_id="device",
        log=lambda _message: None,
    )

    assert calls == [
        "build",
        "stripe_confirm",
        "resolve_ba",
        "close",
    ]
    assert result.paypal_approve_url.endswith("BA-TEST")
    assert result.ba_token == "BA-TEST"
