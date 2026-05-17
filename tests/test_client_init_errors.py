import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from spotdl.download.downloader import DownloaderError
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.spotify import SpotifyError
from spotdl.utils.web import (
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


if __name__ == "__main__":
    unittest.main()
