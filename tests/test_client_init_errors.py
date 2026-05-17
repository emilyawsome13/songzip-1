import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from spotdl.download.downloader import DownloaderError
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.spotify import SpotifyError
from spotdl.utils.web import (
    account_google_start,
    _friendly_job_error_message,
    ensure_spotify_client_initialized,
    get_client,
)


class ClientInitErrorTest(unittest.TestCase):
    def test_get_client_returns_service_unavailable_for_downloader_boot_errors(self):
        with patch("spotdl.utils.web._resolve_authenticated_account", return_value=None), patch(
            "spotdl.utils.web.Client.get_instance", return_value=None
        ), patch("spotdl.utils.web.Downloader", side_effect=DownloaderError("ffmpeg is not installed")), patch(
            "spotdl.utils.web.app_state"
        ) as mock_app_state:
            mock_app_state.downloader_settings = dict(DOWNLOADER_OPTIONS)
            mock_app_state.loop = None
            mock_app_state.clients = {}
            mock_app_state.logger = Mock()
            with self.assertRaises(HTTPException) as caught:
                get_client(client_id="render-test", account_key=None, request=None)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("ffmpeg is not installed", str(caught.exception.detail))

    def test_ensure_spotify_client_initialized_bootstraps_shared_client(self):
        spotify_client = Mock(
            side_effect=SpotifyError(
                "Spotify client not created. Call SpotifyClient.init(client_id, client_secret, user_auth, cache_path, no_cache, open_browser) first."
            )
        )
        spotify_client.init = Mock()
        logger = Mock()

        with patch("spotdl.utils.web.SpotifyClient", spotify_client):
            result = ensure_spotify_client_initialized(logger)

        self.assertTrue(result)
        spotify_client.init.assert_called_once()
        logger.info.assert_called()

    def test_friendly_job_error_message_humanizes_spotify_boot_failure(self):
        message = _friendly_job_error_message(
            SpotifyError(
                "Spotify client not created. Call SpotifyClient.init(client_id, client_secret, user_auth, cache_path, no_cache, open_browser) first."
            )
        )

        self.assertIn("Spotify artist, album, playlist, and track links", message)
        self.assertIn("not initialized", message)

    def test_friendly_job_error_message_humanizes_spotify_rate_limit(self):
        message = _friendly_job_error_message(
            SpotifyError(
                "Spotify rate limit reached. Retry-After was 86400 seconds, so spotDL will not block waiting that long."
            )
        )

        self.assertIn("rate-limited", message)
        self.assertIn("SPOTDL_CLIENT_ID", message)

    def test_google_account_start_redirects_back_with_message_when_unconfigured(self):
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/account/google/start",
            "raw_path": b"/api/account/google/start",
            "query_string": b"",
            "headers": [(b"host", b"songzip.onrender.com")],
            "client": ("127.0.0.1", 12345),
            "server": ("songzip.onrender.com", 443),
        }
        request = Request(scope)

        with patch.dict(
            "os.environ",
            {
                "SONGZIP_GOOGLE_LOGIN_CLIENT_ID": "",
                "SONGZIP_GOOGLE_LOGIN_CLIENT_SECRET": "",
                "SPOTDL_GOOGLE_OAUTH_CLIENT_ID": "",
                "SPOTDL_GOOGLE_OAUTH_CLIENT_SECRET": "",
            },
            clear=False,
        ):
            response = account_google_start(
                request,
                client_id="smoke-client",
                account_key="smoke-key",
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("account_auth_status=error", response.headers["location"])
        self.assertIn("Google%20sign-in%20is%20not%20configured", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
