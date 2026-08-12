from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from .countries import CountryProfile, install_protocol_profiles
from .protocol import stripe_checkout as stripe
from .proxies import ProxyEndpoint


install_protocol_profiles(stripe)

LogFn = Callable[[str], None]
_PAYPAL_URL_RE = re.compile(
    r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>\"']+",
    re.IGNORECASE,
)
_BA_TOKEN_RE = re.compile(r"\bBA-[A-Z0-9-]+\b", re.IGNORECASE)


@dataclass(slots=True)
class CheckoutTransport:
    http: Any
    proxy_url: str
    claimed: bool = False

    def claim(self, proxy_url: str):
        if self.claimed or str(proxy_url or "") != self.proxy_url:
            return None
        self.claimed = True
        return self.http

    def close(self) -> None:
        self.claimed = True
        try:
            self.http.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class CheckoutArtifact:
    session_id: str
    processor_entity: str
    checkout_country: str
    currency: str
    checkout_url: str
    amount: int | None = None
    publishable_key: str = field(default="", repr=False)
    transport: CheckoutTransport | None = field(default=None, repr=False, compare=False)

    def close_transport(self) -> None:
        if self.transport is not None:
            self.transport.close()


@dataclass(frozen=True, slots=True)
class ProviderResult:
    stripe_redirect_url: str
    paypal_approve_url: str
    ba_token: str


def new_device_id() -> str:
    return str(uuid.uuid4())


def preflight_checkout_route(
    *,
    checkout_http,
    checkout_country: str,
    promo_http,
    promo_country: str,
    access_token: str,
    device_id: str,
    log: LogFn,
) -> None:
    checkout_info = stripe.verify_proxy_exit_country(checkout_http, checkout_country)
    log(f"主链路出口预检通过：{stripe.proxy_exit_log_label(checkout_info)}")
    promo_info = stripe.verify_proxy_exit_country(promo_http, promo_country)
    log(f"优惠出口预检通过：{stripe.proxy_exit_log_label(promo_info)}")
    stripe.verify_chatgpt_account(
        checkout_http,
        access_token,
        country=checkout_country,
        device_id=device_id,
    )
    log("ChatGPT /me 账号与连接预检通过")


def _ba_from_url(url: str) -> str:
    try:
        token = (parse_qs(urlsplit(url).query).get("ba_token") or [""])[0]
    except Exception:
        token = ""
    if token:
        return token
    match = _BA_TOKEN_RE.search(url)
    return match.group(0) if match else ""


def resolve_paypal_approval_url(
    http,
    redirect_url: str,
    *,
    max_hops: int = 6,
) -> tuple[str, str]:
    current = html.unescape(str(redirect_url or "").strip())
    if not current:
        raise RuntimeError("Stripe 未返回跳转地址")

    for _hop in range(max(1, max_hops) + 1):
        if "paypal.com/agreements/approve" in current.lower():
            token = _ba_from_url(current)
            if token:
                return current, token
        response = http.get(
            current,
            allow_redirects=False,
            headers={
                "User-Agent": stripe.CHROME_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=30,
        )
        location = str((getattr(response, "headers", {}) or {}).get("location") or "")
        body = html.unescape(str(getattr(response, "text", "") or ""))
        match = _PAYPAL_URL_RE.search(body)
        if match:
            approval = match.group(0).replace("\\u0026", "&").replace("\\/", "/")
            token = _ba_from_url(approval)
            if token:
                return approval, token
        token_match = _BA_TOKEN_RE.search(body)
        if token_match and "paypal" in body.lower():
            token = token_match.group(0)
            return f"https://www.paypal.com/agreements/approve?ba_token={token}", token
        if not location:
            break
        current = urljoin(current, html.unescape(location))

    raise RuntimeError("未能从 Stripe 跳转解析 PayPal BA 链接")


class LiveProtocolGateway:
    def create_checkout(
        self,
        *,
        access_token: str,
        country: CountryProfile,
        proxy: ProxyEndpoint,
        device_id: str,
        promo_proxy: ProxyEndpoint | None = None,
        promo_country: CountryProfile | None = None,
        log: LogFn,
    ) -> CheckoutArtifact:
        http = stripe.build_http(proxy.url)
        if promo_proxy is None:
            http.close()
            raise ValueError("需要单独的优惠 update 代理")
        promo_http = stripe.build_http(promo_proxy.url)
        keep_checkout_http = False
        context: dict[str, str] = {}
        try:
            update_country = (promo_country or country).code
            preflight_checkout_route(
                checkout_http=http,
                checkout_country=country.code,
                promo_http=promo_http,
                promo_country=update_country,
                access_token=access_token,
                device_id=device_id,
                log=log,
            )
            session_id, error = stripe.create_chatgpt_order_with_retry(
                http,
                access_token,
                country=country.code,
                currency=country.currency,
                device_id=device_id,
                sentinel_proxy=proxy.url,
                checkout_context=context,
                with_promo=False,
                max_attempts=3,
                log=log,
            )
            if not session_id:
                raise RuntimeError(f"创建 Checkout 失败: {error or '没有 session id'}")
            processor_entity = context.get("processor_entity") or country.processor_entity
            pk = context.get("publishable_key") or stripe.verify_pk(
                http,
                session_id,
                lambda _message: None,
            )
            stripe.init_checkout(
                http,
                session_id,
                pk,
                stripe._profile(country.code),
                lambda _message: None,
            )
            stripe.update_chatgpt_checkout_promotion(
                promo_http,
                access_token,
                session_id,
                processor_entity=processor_entity,
                country=update_country,
                device_id=device_id,
                billing_country=country.code,
                billing_currency=country.currency,
                log=log,
            )
            stripe.verify_promo_checkout_zero(
                http,
                session_id,
                country=country.code,
                publishable_key=pk,
                log=lambda _message: None,
            )
            checkout_url = context.get("checkout_url") or (
                f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
            )
            transport = CheckoutTransport(http=http, proxy_url=proxy.url)
            keep_checkout_http = True
            return CheckoutArtifact(
                session_id=session_id,
                processor_entity=processor_entity,
                checkout_country=country.code,
                currency=country.currency,
                checkout_url=checkout_url,
                amount=0,
                publishable_key=pk,
                transport=transport,
            )
        finally:
            for client in (promo_http, None if keep_checkout_http else http):
                if client is None:
                    continue
                try:
                    client.close()
                except Exception:
                    pass

    def attempt_provider(
        self,
        *,
        artifact: CheckoutArtifact,
        access_token: str,
        country: CountryProfile,
        billing: dict,
        proxy: ProxyEndpoint,
        device_id: str,
        check_cancelled: Callable[[], None] = lambda: None,
        log: LogFn,
    ) -> ProviderResult:
        http = artifact.transport.claim(proxy.url) if artifact.transport is not None else None
        reused_checkout_transport = http is not None
        if http is None:
            http = stripe.build_http(proxy.url)
        try:
            check_cancelled()
            if not reused_checkout_transport:
                info = stripe.verify_proxy_exit_country(http, country.code)
                log(f"提链出口预检通过：{stripe.proxy_exit_log_label(info)}")
                stripe.verify_chatgpt_account(
                    http,
                    access_token,
                    country=country.code,
                    device_id=device_id,
                )
                log("提链出口 ChatGPT /me 预检通过")
            else:
                log("首次提链复用 Checkout 主链路会话")
            redirect_url, _publishable_key, context = stripe.stripe_to_paypal_redirect(
                http,
                artifact.session_id,
                billing=billing,
                country=country.code,
                processor_entity=artifact.processor_entity,
                publishable_key=artifact.publishable_key,
                require_zero_amount=True,
                chatgpt_http=http,
                access_token=access_token,
                device_id=device_id,
                sentinel_proxy=proxy.url,
                log=lambda raw: log(raw.removeprefix("[stripe] ")),
            )
            check_cancelled()
            approval_url, ba_token = resolve_paypal_approval_url(http, redirect_url)
            return ProviderResult(
                stripe_redirect_url=redirect_url,
                paypal_approve_url=approval_url,
                ba_token=ba_token,
            )
        finally:
            try:
                http.close()
            except Exception:
                pass
