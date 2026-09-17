"""
The IP recorded on a reveal.

It is only a hint for whoever reviews a flag, but a forged hint is worse than
none, and the column is an inet -- junk in the header must not become a 500 on
the request that hands over a gate code.
"""

import pytest
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory

from audit.services import client_ip


def _request(**meta):
    return APIRequestFactory().get("/", **meta)


@pytest.fixture
def proxies(monkeypatch):
    def _set(count):
        monkeypatch.setattr(api_settings, "NUM_PROXIES", count)

    return _set


class TestClientIp:
    def test_with_no_proxy_the_forwarded_header_is_ignored(self, proxies):
        proxies(0)
        request = _request(REMOTE_ADDR="203.0.113.9", HTTP_X_FORWARDED_FOR="10.1.1.1")

        assert client_ip(request) == "203.0.113.9"

    def test_behind_one_proxy_the_last_entry_is_the_one_it_wrote(self, proxies):
        proxies(1)
        request = _request(REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7")

        assert client_ip(request) == "198.51.100.7"

    def test_behind_two_proxies_it_is_the_entry_before_that(self, proxies):
        proxies(2)
        request = _request(
            REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7, 10.0.0.1"
        )

        assert client_ip(request) == "198.51.100.7"

    def test_a_proxy_that_sent_no_header_falls_back_to_the_socket(self, proxies):
        proxies(1)

        assert client_ip(_request(REMOTE_ADDR="203.0.113.9")) == "203.0.113.9"

    def test_junk_is_dropped_rather_than_stored(self, proxies):
        proxies(1)
        request = _request(REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="'; drop table--")

        assert client_ip(request) is None

    def test_ipv6_is_kept(self, proxies):
        proxies(1)
        request = _request(REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="2001:db8::1")

        assert client_ip(request) == "2001:db8::1"
