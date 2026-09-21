import pytest

from bookmedia.urls import normalize_url


def test_lowercases_scheme_and_host_keeps_path_case():
    assert normalize_url("HTTPS://X.COM/User/Status/1") == "https://x.com/User/Status/1"


def test_strips_trailing_slash_and_fragment():
    assert normalize_url("https://x.com/u/status/1/#frag") == "https://x.com/u/status/1"


def test_drops_default_ports_keeps_others():
    assert normalize_url("https://x.com:443/a") == "https://x.com/a"
    assert normalize_url("http://x.com:80/a") == "http://x.com/a"
    assert normalize_url("https://x.com:8443/a") == "https://x.com:8443/a"


def test_strips_utm_params_and_sorts_rest():
    assert (
        normalize_url("https://x.com/a?b=2&utm_source=t&a=1&UTM_MEDIUM=x")
        == "https://x.com/a?a=1&b=2"
    )


def test_rejects_non_url():
    with pytest.raises(ValueError):
        normalize_url("not a url")
