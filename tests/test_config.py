"""Settings from the environment."""

import pytest

from shelflife.config import load_settings


def test_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("STEAM_API_KEY", " abc ")
    monkeypatch.setenv("STEAM_ID", "123")
    settings = load_settings()
    assert (settings.steam_api_key, settings.steam_id) == ("abc", "123")  # surrounding spaces are trimmed


@pytest.mark.parametrize("missing", [["STEAM_API_KEY"], ["STEAM_ID"], ["STEAM_API_KEY", "STEAM_ID"]])
def test_missing_settings_exit_with_a_plain_message_not_a_traceback(monkeypatch, missing):
    monkeypatch.setenv("STEAM_API_KEY", "abc")
    monkeypatch.setenv("STEAM_ID", "123")
    for name in missing:
        monkeypatch.delenv(name)
    with pytest.raises(SystemExit) as caught:
        load_settings()
    message = str(caught.value)
    assert all(name in message for name in missing)
    assert ".env.example" in message


def test_a_blank_value_counts_as_missing(monkeypatch):
    monkeypatch.setenv("STEAM_API_KEY", "   ")
    monkeypatch.setenv("STEAM_ID", "123")
    with pytest.raises(SystemExit):
        load_settings()
