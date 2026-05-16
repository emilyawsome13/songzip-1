import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from spotdl.utils.web import Client


class WebBundleCompressionTest(unittest.TestCase):
    def test_mobile_bundle_uses_store_compression(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_root = root / "output"
            output_root.mkdir(parents=True, exist_ok=True)
            media_file = output_root / "Artist - Song.mp3"
            media_file.write_bytes(b"test-audio" * 128)

            client = Client.__new__(Client)
            client.client_id = "bundle-client"
            client.download_bundle = None
            client.get_output_root = lambda: str(output_root)  # type: ignore[method-assign]

            with patch("spotdl.utils.web.get_spotdl_path", return_value=root), patch(
                "spotdl.utils.web.app_state"
            ) as mock_app_state:
                mock_app_state.web_settings = {
                    "bundle_flatten": True,
                    "bundle_compression": "store",
                }
                mock_app_state.logger = Mock()

                bundle = client._create_download_bundle(  # pylint: disable=protected-access
                    [{"path": str(media_file)}]
                )

            self.assertIsNotNone(bundle)
            assert bundle is not None
            with zipfile.ZipFile(bundle["path"], "r") as archive:
                info = archive.infolist()[0]
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(info.filename, "Artist - Song.mp3")


if __name__ == "__main__":
    unittest.main()
