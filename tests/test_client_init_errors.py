import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from spotdl.download.downloader import DownloaderError
from spotdl.utils.config import DOWNLOADER_OPTIONS
from spotdl.utils.web import get_client


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


if __name__ == "__main__":
    unittest.main()
