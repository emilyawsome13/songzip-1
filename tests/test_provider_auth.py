from urllib.parse import parse_qs, urlparse

from spotdl.utils.provider_auth import ProviderAuthManager


def _set_spotify_env(monkeypatch):
    monkeypatch.setenv("SPOTDL_SPOTIFY_OAUTH_CLIENT_ID", "spotify-client-id")
    monkeypatch.setenv("SPOTDL_SPOTIFY_OAUTH_CLIENT_SECRET", "spotify-client-secret")
    monkeypatch.setenv(
        "SPOTDL_SPOTIFY_OAUTH_REDIRECT_URI",
        "http://127.0.0.1:8801/api/auth/callback/spotify",
    )


def test_provider_status_reports_missing_setup(tmp_path):
    manager = ProviderAuthManager(tmp_path / "provider-connections.json")

    statuses = manager.get_provider_statuses("client-1")
    spotify = next(item for item in statuses if item["provider"] == "spotify")

    assert spotify["configured"] is False
    assert spotify["connected"] is False
    assert "SPOTDL_SPOTIFY_OAUTH_CLIENT_ID" in spotify["setup_missing"]


def test_build_authorization_url_tracks_state(monkeypatch, tmp_path):
    _set_spotify_env(monkeypatch)
    manager = ProviderAuthManager(tmp_path / "provider-connections.json")

    auth_url = manager.build_authorization_url("spotify", "client-42")

    parsed = urlparse(auth_url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.spotify.com"
    assert query["client_id"] == ["spotify-client-id"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8801/api/auth/callback/spotify"]
    assert "user-library-read" in query["scope"][0]
    assert query["state"][0] in manager._pending_states  # pylint: disable=protected-access


def test_complete_callback_persists_connection(monkeypatch, tmp_path):
    _set_spotify_env(monkeypatch)
    manager = ProviderAuthManager(tmp_path / "provider-connections.json")

    auth_url = manager.build_authorization_url("spotify", "client-99")
    state_token = parse_qs(urlparse(auth_url).query)["state"][0]

    monkeypatch.setattr(
        manager,
        "_exchange_code_for_token",
        lambda spec, code: {
            "access_token": "token-123",
            "refresh_token": "refresh-123",
            "expires_in": 3600,
            "scope": "user-library-read playlist-read-private",
            "token_type": "Bearer",
        },
    )
    monkeypatch.setattr(
        manager,
        "_fetch_profile",
        lambda spec, access_token: {
            "id": "spotify-user-1",
            "display_name": "SongZip Tester",
        },
    )

    result = manager.complete_callback("spotify", state_token, "oauth-code", None)
    statuses = manager.get_provider_statuses("client-99")
    spotify = next(item for item in statuses if item["provider"] == "spotify")

    assert result["client_id"] == "client-99"
    assert spotify["connected"] is True
    assert spotify["account_label"] == "SongZip Tester"
    assert spotify["account_id"] == "spotify-user-1"
    assert (tmp_path / "provider-connections.json").is_file()

    assert manager.disconnect("spotify", "client-99") is True
    statuses_after_disconnect = manager.get_provider_statuses("client-99")
    spotify_after_disconnect = next(
        item for item in statuses_after_disconnect if item["provider"] == "spotify"
    )
    assert spotify_after_disconnect["connected"] is False
