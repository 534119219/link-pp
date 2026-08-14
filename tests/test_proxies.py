from __future__ import annotations

import pytest

from handoff.proxies import ProxyInputError, ProxyPool, parse_proxy_lines


def test_parses_supported_url_schemes_and_aliases():
    endpoints = parse_proxy_lines(
        "\n".join(
            [
                "socks5://user:pass@proxy.example:1080",
                "socket5://user:pass@proxy.example:1081",
                "http://proxy.example:8080",
                "https://user:pass@proxy.example:8443",
            ]
        )
    )
    assert [item.scheme for item in endpoints] == ["socks5h", "socks5h", "http", "https"]
    assert endpoints[0].safe_label == "socks5h://proxy.example:1080"
    assert "user" not in endpoints[0].safe_label


def test_parses_host_port_user_password_in_required_order():
    endpoint = parse_proxy_lines(
        "us.rrp.example:10000:USER-zone-custom-region-BR-session-1234:secret",
        default_scheme="socks5",
    )[0]
    assert endpoint.url == (
        "socks5h://USER-zone-custom-region-BR-session-1234:secret@us.rrp.example:10000"
    )


def test_socks5_and_socks5h_deduplicate_to_remote_dns_route():
    endpoints = parse_proxy_lines(
        "socks5://user:pass@proxy.example:1080\n"
        "socks5h://user:pass@proxy.example:1080"
    )
    assert len(endpoints) == 1
    assert endpoints[0].scheme == "socks5h"


def test_password_colon_is_encoded_and_duplicates_are_removed():
    endpoints = parse_proxy_lines(
        "# pool\r\nproxy.example:1080:user:pa:ss\r\n\r\nproxy.example:1080:user:pa:ss\r\n"
    )
    assert len(endpoints) == 1
    assert "pa%3Ass" in endpoints[0].url


def test_pool_round_robins_without_cross_pool_state():
    first = ProxyPool(parse_proxy_lines("a.example:1\nb.example:2"))
    second = ProxyPool(parse_proxy_lines("c.example:3\nd.example:4"))
    assert [first.pick(i).host for i in range(1, 6)] == ["a.example", "b.example", "a.example", "b.example", "a.example"]
    assert [second.pick(i).host for i in range(1, 4)] == ["c.example", "d.example", "c.example"]


@pytest.mark.parametrize(
    "value",
    ["missing", "host:notaport", "ftp://host:21", "host:70000", "host:80:user:"],
)
def test_rejects_invalid_proxy_without_echoing_input(value):
    with pytest.raises(ProxyInputError) as captured:
        parse_proxy_lines(value)
    assert "第 1 行" in str(captured.value)
    assert value not in str(captured.value)
