import pytest

from bookmedia.config import load_settings


def _env(**overrides):
    base = {
        "TELEGRAM_BOT_TOKEN": "token",
    }
    base.update(overrides)
    return base


def test_defaults():
    s = load_settings(_env())
    assert s.data_dir == "/app/data"
    assert s.temp_dir == "/app/tmp"
    assert s.cookies_path == "/app/cookies/cookies.txt"
    assert s.log_level == "INFO"
    assert s.allowed_user_ids == ()
    assert s.db_path == "/app/data/archive.db"
    assert s.local_bot_api_host == ""
    assert s.local_bot_api_port == 8081
    assert s.local_bot_api_url == ""
    assert s.dashboard_port == 0


def test_dashboard_port_parsing():
    s = load_settings(_env(DASHBOARD_PORT="8080"))
    assert s.dashboard_port == 8080


def test_dashboard_port_invalid_falls_back():
    s = load_settings(_env(DASHBOARD_PORT="notanumber"))
    assert s.dashboard_port == 0


def test_missing_token_names_var_without_value():
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        load_settings(_env(TELEGRAM_BOT_TOKEN=""))
    try:
        load_settings(_env(TELEGRAM_BOT_TOKEN=""))
    except ValueError as exc:
        assert "token" not in str(exc)


def test_allowed_user_ids_parsing():
    s = load_settings(_env(TELEGRAM_ALLOWED_USER_IDS="1, 2,,3 "))
    assert s.allowed_user_ids == ("1", "2", "3")


def test_log_level_normalized():
    assert load_settings(_env(LOG_LEVEL="debug")).log_level == "DEBUG"


def test_max_media_bytes_default_is_2gib():
    s = load_settings(_env())
    assert s.max_media_bytes == 2000 * 1024 * 1024


def test_max_media_bytes_override_and_disable():
    s = load_settings(_env(MAX_MEDIA_BYTES="1000000"))
    assert s.max_media_bytes == 1_000_000
    s = load_settings(_env(MAX_MEDIA_BYTES="0"))
    assert s.max_media_bytes == 0  # 0 disables the gate


def test_max_media_bytes_invalid_falls_back_and_negative_clamps_to_disabled():
    s = load_settings(_env(MAX_MEDIA_BYTES="junk"))
    assert s.max_media_bytes == 2000 * 1024 * 1024
    s = load_settings(_env(MAX_MEDIA_BYTES="-5"))
    assert s.max_media_bytes == 0


def test_values_are_stripped_of_surrounding_whitespace_and_cr():
    s = load_settings(_env(TELEGRAM_BOT_TOKEN="token\r\n"))
    assert s.bot_token == "token"


def test_local_bot_api_host_and_port():
    s = load_settings(_env(LOCAL_BOT_API_HOST="localhost", LOCAL_BOT_API_PORT="9090"))
    assert s.local_bot_api_host == "localhost"
    assert s.local_bot_api_port == 9090
    assert s.local_bot_api_url == "http://localhost:9090"


def test_local_bot_api_defaults_to_cloud():
    s = load_settings(_env())
    assert s.local_bot_api_host == ""
    assert s.local_bot_api_url == ""


def test_local_bot_api_port_invalid_falls_back():
    s = load_settings(_env(LOCAL_BOT_API_HOST="localhost", LOCAL_BOT_API_PORT="notanumber"))
    assert s.local_bot_api_port == 8081
