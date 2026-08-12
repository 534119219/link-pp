from __future__ import annotations

import re

import pytest

from handoff.countries import COUNTRIES, get_country, install_protocol_profiles, list_countries


def test_registry_has_broad_country_coverage_and_complete_profiles():
    assert len(COUNTRIES) >= 40
    for code, country in COUNTRIES.items():
        assert re.fullmatch(r"[A-Z]{2}", code)
        assert country.code == code
        assert re.fullmatch(r"[A-Z]{3}", country.currency)
        assert "-" in country.locale
        assert "/" in country.timezone
        assert country.processor_entity in {"openai_llc", "openai_ie"}
        assert country.address.line1
        assert country.address.city
        assert country.address.postal_code


def test_country_list_is_public_and_preferred_countries_come_first():
    countries = list_countries()
    assert [item["code"] for item in countries[:7]] == ["US", "BR", "GB", "DE", "FR", "JP", "TH"]
    assert "address" not in countries[0]


def test_thailand_paypal_profile_is_complete():
    thailand = get_country("th")
    assert thailand.name == "泰国"
    assert thailand.currency == "THB"
    assert thailand.locale == "th-TH"
    assert thailand.timezone == "Asia/Bangkok"


def test_unknown_country_is_not_silently_mapped_to_us():
    with pytest.raises(ValueError, match="不支持"):
        get_country("XX")


def test_protocol_profiles_are_installed_for_every_country():
    class Module:
        LOCALE_PROFILES = {}

    install_protocol_profiles(Module)
    assert set(Module.LOCALE_PROFILES) == set(COUNTRIES)
    assert Module.LOCALE_PROFILES["JP"]["browser_timezone"] == "Asia/Tokyo"
