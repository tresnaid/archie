from bookmedia.parser import parse_message


def test_basic_url_defaults_to_unsorted():
    parsed = parse_message("https://x.com/user/status/123")
    assert parsed is not None
    assert parsed.url == "https://x.com/user/status/123"
    assert parsed.type == "unsorted"


def test_url_with_type():
    parsed = parse_message("https://www.instagram.com/p/ABC/ --type=research")
    assert parsed is not None
    assert parsed.url == "https://www.instagram.com/p/ABC/"
    assert parsed.type == "research"


def test_surrounding_whitespace_tolerated():
    parsed = parse_message("  https://x.com/a --type=ai  \n")
    assert parsed is not None
    assert parsed.type == "ai"


def test_non_url_starts_no_job():
    assert parse_message("hello world") is None
    assert parse_message("--type=research") is None
    assert parse_message("") is None
    assert parse_message(None) is None


def test_non_http_scheme_rejected():
    assert parse_message("ftp://x.com/a") is None
    assert parse_message("x.com/a") is None


def test_extra_tokens_rejected():
    assert parse_message("https://x.com/a extra words") is None
    assert parse_message("https://x.com/a --type=a --type=b") is None


def test_malformed_type_rejected():
    assert parse_message("https://x.com/a --type=") is None
    assert parse_message("https://x.com/a --type") is None
    assert parse_message("https://x.com/a type=research") is None


def test_type_kept_verbatim():
    parsed = parse_message("https://x.com/a --type=Side-Hustle")
    assert parsed is not None
    assert parsed.type == "Side-Hustle"
