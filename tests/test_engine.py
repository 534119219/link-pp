from __future__ import annotations

from dataclasses import dataclass

import pytest

from handoff.countries import get_country
from handoff.engine import (
    DEFAULT_CHECKOUT_ATTEMPTS,
    DEFAULT_PROVIDER_ATTEMPTS,
    FlowExhaustedError,
    HandoffEngine,
    RunSpec,
    _short_reason,
    positive_attempts,
)
from handoff.gateway import CheckoutArtifact, ProviderResult
from handoff.proxies import ProxyPool, parse_proxy_lines
from handoff.protocol.stripe_checkout import CheckoutPreflightError
from handoff.security import TokenProfile


@dataclass
class FakeGateway:
    provider_behavior: object = None
    checkout_behavior: object = None

    def __post_init__(self):
        self.checkout_calls = []
        self.provider_calls = []

    def create_checkout(self, **kwargs):
        self.checkout_calls.append(kwargs)
        if callable(self.checkout_behavior):
            custom = self.checkout_behavior(len(self.checkout_calls), kwargs)
            if custom is not None:
                return custom
        number = len(self.checkout_calls)
        return CheckoutArtifact(
            session_id=f"cs_live_{number}",
            processor_entity="openai_llc",
            checkout_country=kwargs["country"].code,
            currency=kwargs["country"].currency,
            checkout_url=f"https://chatgpt.com/checkout/openai_llc/cs_live_{number}",
        )

    def attempt_provider(self, **kwargs):
        self.provider_calls.append(kwargs)
        if callable(self.provider_behavior):
            return self.provider_behavior(len(self.provider_calls), kwargs)
        raise RuntimeError("manual_approval approve blocked: result=blocked")


def make_spec(*, checkout_attempts=5, provider_attempts=10):
    return RunSpec(
        access_token="secret-at",
        token_profile=TokenProfile("owner@example.com", "Owner", "acct"),
        checkout_country=get_country("BR"),
        promo_country=get_country("DE"),
        checkout_proxies=ProxyPool(parse_proxy_lines("checkout-a:1001\ncheckout-b:1002")),
        promo_proxies=ProxyPool(parse_proxy_lines("promo-a:2001\npromo-b:2002")),
        checkout_attempts=checkout_attempts,
        provider_attempts=provider_attempts,
    )


def success_result():
    return ProviderResult(
        stripe_redirect_url="https://pm-redirects.stripe.com/authorize/test",
        paypal_approve_url="https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
        ba_token="BA-TEST",
    )


def test_defaults_and_blocked_checkout_rotation():
    assert DEFAULT_CHECKOUT_ATTEMPTS == 5
    assert DEFAULT_PROVIDER_ATTEMPTS == 10
    gateway = FakeGateway()
    with pytest.raises(FlowExhaustedError):
        HandoffEngine(gateway).run(
            make_spec(), emit=lambda *_item: None, is_cancelled=lambda: False
        )
    assert len(gateway.checkout_calls) == 5
    assert len(gateway.provider_calls) == 5
    assert [call["proxy"].host for call in gateway.provider_calls] == [
        "checkout-a", "checkout-b", "checkout-a", "checkout-b", "checkout-a"
    ]


def test_provider_success_returns_links_without_payment_state():
    def behavior(number, _kwargs):
        if number < 3:
            raise TimeoutError("proxy timed out")
        return success_result()

    gateway = FakeGateway(provider_behavior=behavior)
    result = HandoffEngine(gateway).run(
        make_spec(checkout_attempts=2, provider_attempts=4),
        emit=lambda *_item: None,
        is_cancelled=lambda: False,
    )
    assert len(gateway.checkout_calls) == 1
    assert len(gateway.provider_calls) == 3
    assert result.paypal_approve_url.endswith("BA-TEST")
    assert result.stripe_redirect_url.endswith("/test")
    assert "payment_completed" not in result.to_dict()


def test_preflight_failure_does_not_consume_checkout_and_first_provider_reuses_proxy():
    def checkout_behavior(number, _kwargs):
        if number == 1:
            raise CheckoutPreflightError("proxy unavailable", code="PROXY_UNAVAILABLE")
        return None

    gateway = FakeGateway(
        checkout_behavior=checkout_behavior,
        provider_behavior=lambda _number, _kwargs: success_result(),
    )
    result = HandoffEngine(gateway).run(
        make_spec(checkout_attempts=1, provider_attempts=1),
        emit=lambda *_item: None,
        is_cancelled=lambda: False,
    )
    assert result.checkout_attempt == 1
    assert [call["proxy"].host for call in gateway.checkout_calls] == [
        "checkout-a", "checkout-b"
    ]
    assert gateway.provider_calls[0]["proxy"].host == "checkout-b"


def test_new_checkout_uses_new_device_id():
    gateway = FakeGateway()
    with pytest.raises(FlowExhaustedError):
        HandoffEngine(gateway).run(
            make_spec(checkout_attempts=3, provider_attempts=1),
            emit=lambda *_item: None,
            is_cancelled=lambda: False,
        )
    assert len({call["device_id"] for call in gateway.checkout_calls}) == 3


def test_access_token_is_redacted_from_logs_and_errors():
    token = "secret-at"

    def behavior(_number, _kwargs):
        raise RuntimeError(f"network failed Bearer {token}")

    logs = []
    with pytest.raises(FlowExhaustedError) as captured:
        HandoffEngine(FakeGateway(provider_behavior=behavior)).run(
            make_spec(checkout_attempts=1, provider_attempts=1),
            emit=lambda *item: logs.append(item),
            is_cancelled=lambda: False,
        )
    assert token not in repr(logs) + str(captured.value)


def test_attempt_validation_and_short_reasons():
    assert positive_attempts("3", default=1) == 3
    with pytest.raises(ValueError):
        positive_attempts(0, default=1)
    assert _short_reason(TimeoutError("read timed out"), "") == "代理连接失败"
    assert _short_reason(RuntimeError("manual_approval approve blocked"), "") == "审批被拒绝"
    assert _short_reason(RuntimeError("decline_code=generic_decline"), "") == "风控拒绝（generic_decline）"
    assert (
        _short_reason(
            RuntimeError("无法确认 Stripe publishable_key（404 resource_missing）"),
            "",
        )
        == "Stripe 分片 key 不匹配"
    )
