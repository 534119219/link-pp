from __future__ import annotations

import json
from types import SimpleNamespace

from handoff.protocol import sentinel


def test_checkout_sentinel_uses_chatgpt_origin_and_validates_context(monkeypatch):
    captured = {}
    token = json.dumps(
        {
            "p": "gAAAAABproof",
            "t": "turnstile",
            "c": "challenge",
            "id": "device-test",
            "flow": "checkout_session_approval",
        },
        separators=(",", ":"),
    )

    def run(_args, **kwargs):
        captured.update(json.loads(kwargs["input"]))
        return SimpleNamespace(
            stdout=json.dumps({"main": token, "so": ""}).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(sentinel.subprocess, "run", run)
    main, so = sentinel.mint_sentinel_sync(
        flow="checkout_session_approval",
        device_id="device-test",
        user_agent="Chrome test",
        proxy="socks5h://proxy.test:1080",
        page_url="https://chatgpt.com/checkout/openai_ie/cs_test",
        language="en-GB",
        timezone="Europe/London",
        cookie_header="oai-did=device-test",
    )

    assert json.loads(main)["c"] == "challenge"
    assert so == ""
    assert captured["sentinelOrigin"] == "https://chatgpt.com"
    assert captured["cookieHeader"] == "oai-did=device-test"
    assert captured["proxy"] == "socks5h://proxy.test:1080"

