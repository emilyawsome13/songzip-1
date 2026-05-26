import socket
import threading
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
import uvicorn

from spotdl.utils.songzip_store import SongZipStore
from spotdl.utils.songzip_store_remote import (
    RemoteSongZipStore,
    create_songzip_store_bridge_app,
)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _serve_app(app):
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    last_error = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/health", timeout=1)
            if response.status_code == 200:
                break
        except requests.RequestException as error:
            last_error = error
            time.sleep(0.1)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(f"SongZip store bridge did not start: {last_error}")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


class SongZipStoreRemoteTest(unittest.TestCase):
    def test_bridge_rpc_round_trip(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            app = create_songzip_store_bridge_app(store, shared_secret="secret-123")

            with _serve_app(app) as base_url:
                register = requests.post(
                    f"{base_url}/rpc",
                    headers={"X-SongZip-Store-Secret": "secret-123"},
                    json={
                        "method": "register_account",
                        "args": ["bridge@example.com", "password123"],
                        "kwargs": {},
                    },
                    timeout=5,
                )
                self.assertEqual(register.status_code, 200)
                account = register.json()["result"]

                membership = requests.post(
                    f"{base_url}/rpc",
                    headers={"X-SongZip-Store-Secret": "secret-123"},
                    json={
                        "method": "set_account_membership",
                        "args": [account["email"], "pro"],
                        "kwargs": {},
                    },
                    timeout=5,
                )
                self.assertEqual(membership.status_code, 200)

                loaded = requests.post(
                    f"{base_url}/rpc",
                    headers={"X-SongZip-Store-Secret": "secret-123"},
                    json={
                        "method": "load_subscription",
                        "args": [account["account_key"]],
                        "kwargs": {},
                    },
                    timeout=5,
                )
                self.assertEqual(loaded.status_code, 200)
                subscription = loaded.json()["result"]

        self.assertEqual(subscription["tier"], "pro")
        self.assertEqual(subscription["membership_source"], "admin")

    def test_remote_proxy_round_trip(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            app = create_songzip_store_bridge_app(store, shared_secret="secret-456")

            with _serve_app(app) as base_url:
                remote_store = RemoteSongZipStore(
                    base_url,
                    shared_secret="secret-456",
                    timeout_seconds=5,
                )
                account = remote_store.register_account("remote@example.com", "password123")
                remote_store.set_account_membership(account["email"], "plus")
                loaded = remote_store.load_subscription(account["account_key"])

        self.assertEqual(loaded["tier"], "plus")
        self.assertEqual(loaded["membership_source"], "admin")


if __name__ == "__main__":
    unittest.main()
