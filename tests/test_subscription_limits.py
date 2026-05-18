import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from starlette.requests import Request

from spotdl.utils.web import (
    Client,
    FREE_TIER_DOWNLOAD_LIMIT,
    _build_subscription_snapshot,
    get_client,
    _load_subscription_state_for_key,
    _migrate_subscription_state,
    _sync_subscription_state_from_record,
)
from spotdl.utils.songzip_store import SongZipStore


class SubscriptionLimitTest(unittest.TestCase):
    def test_free_tier_truncates_request_at_remaining_capacity(self):
        client = Client.__new__(Client)
        client.subscription = {
            "tier": "free",
            "downloads_used": FREE_TIER_DOWNLOAD_LIMIT - 2,
            "downloads_lifetime": FREE_TIER_DOWNLOAD_LIMIT - 2,
        }
        client.pending_upgrade_prompt = None
        client._save_subscription_state = lambda: None  # type: ignore[method-assign]
        client._record_subscription_event = lambda *args, **kwargs: None  # type: ignore[method-assign]

        songs = ["one", "two", "three", "four"]
        allowed, overflow = client._reserve_download_capacity(songs)  # pylint: disable=protected-access

        self.assertEqual(allowed, ["one", "two"])
        self.assertEqual(overflow, 2)
        self.assertEqual(client.subscription["downloads_used"], FREE_TIER_DOWNLOAD_LIMIT)
        self.assertIsNotNone(client.pending_upgrade_prompt)

    def test_paid_tier_is_not_capped_by_free_limit(self):
        client = Client.__new__(Client)
        client.subscription = {
            "tier": "basic",
            "downloads_used": 0,
            "downloads_lifetime": 0,
            "subscription_id": "sub_paid",
        }
        client.pending_upgrade_prompt = None
        client._save_subscription_state = lambda: None  # type: ignore[method-assign]
        client._record_subscription_event = lambda *args, **kwargs: None  # type: ignore[method-assign]

        songs = ["one", "two", "three"]
        allowed, overflow = client._reserve_download_capacity(songs)  # pylint: disable=protected-access

        self.assertEqual(allowed, songs)
        self.assertEqual(overflow, 0)
        self.assertIsNone(client.pending_upgrade_prompt)
        self.assertEqual(client.subscription["downloads_lifetime"], 3)

    def test_free_tier_bonus_credits_extend_limit(self):
        client = Client.__new__(Client)
        client.subscription = {
            "tier": "free",
            "downloads_used": FREE_TIER_DOWNLOAD_LIMIT,
            "downloads_lifetime": FREE_TIER_DOWNLOAD_LIMIT,
            "bonus_credits": 3,
        }
        client.pending_upgrade_prompt = None
        client._save_subscription_state = lambda: None  # type: ignore[method-assign]
        client._record_subscription_event = lambda *args, **kwargs: None  # type: ignore[method-assign]

        allowed, overflow = client._reserve_download_capacity(["one", "two", "three", "four"])  # pylint: disable=protected-access

        self.assertEqual(allowed, ["one", "two", "three"])
        self.assertEqual(overflow, 1)
        self.assertEqual(
            client.subscription["downloads_used"],
            FREE_TIER_DOWNLOAD_LIMIT + 3,
        )
        self.assertEqual(
            client.subscription["downloads_lifetime"],
            FREE_TIER_DOWNLOAD_LIMIT + 3,
        )

    def test_bonus_credits_clear_stale_upgrade_prompt_in_snapshot(self):
        snapshot = _build_subscription_snapshot(
            "acct-demo",
            {
                "tier": "free",
                "downloads_used": FREE_TIER_DOWNLOAD_LIMIT,
                "downloads_lifetime": FREE_TIER_DOWNLOAD_LIMIT,
                "bonus_credits": 100,
            },
            pending_upgrade_prompt={"message": "Upgrade now."},
        )

        self.assertEqual(snapshot["limit"], FREE_TIER_DOWNLOAD_LIMIT + 100)
        self.assertEqual(snapshot["remaining"], 100)
        self.assertFalse(snapshot["upgrade_required"])
        self.assertIsNone(snapshot["upgrade_prompt"])

    def test_subscription_state_persists_for_activation_flow(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            client = Client.__new__(Client)
            client.client_id = "persist-test"
            client.account_key = "shared-key"
            client.subscription = {
                "tier": "basic",
                "downloads_used": 12,
                "downloads_lifetime": 34,
                "subscription_id": "sub_123",
                "activated_at": "2026-05-16T00:00:00-05:00",
                "paypal_status": "LOCAL_APPROVED",
                "updated_at": "2026-05-16T00:00:00-05:00",
            }

            with patch("spotdl.utils.web.songzip_store", store):
                client._save_subscription_state()  # pylint: disable=protected-access
                loaded = client._load_subscription_state()  # pylint: disable=protected-access

        self.assertEqual(loaded["tier"], "basic")
        self.assertEqual(loaded["downloads_used"], 12)
        self.assertEqual(loaded["downloads_lifetime"], 34)
        self.assertEqual(loaded["subscription_id"], "sub_123")

    def test_paypal_record_sync_downgrades_cancelled_account_to_free(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            with patch("spotdl.utils.web.songzip_store", store):
                _sync_subscription_state_from_record(
                    {
                        "subscription_id": "sub_cancelled",
                        "account_key": "shared-key",
                        "tier": "pro",
                        "status": "CANCELLED",
                    }
                )
                loaded = _load_subscription_state_for_key("shared-key")

        self.assertEqual(loaded["tier"], "free")
        self.assertEqual(loaded["subscription_id"], None)

    def test_paypal_sync_does_not_override_admin_membership(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_subscription(
                "acct-admin-member",
                {
                    "tier": "pro",
                    "downloads_used": 0,
                    "downloads_lifetime": 0,
                    "membership_source": "admin",
                    "bonus_credits": 0,
                    "subscription_id": None,
                    "activated_at": "2026-05-17T00:00:00-05:00",
                    "paypal_status": "ADMIN_GRANTED",
                },
            )
            with patch("spotdl.utils.web.songzip_store", store):
                _sync_subscription_state_from_record(
                    {
                        "subscription_id": "sub_old",
                        "account_key": "acct-admin-member",
                        "tier": "free",
                        "status": "CANCELLED",
                    }
                )
                loaded = _load_subscription_state_for_key("acct-admin-member")

        self.assertEqual(loaded["tier"], "pro")
        self.assertEqual(loaded["membership_source"], "admin")
        self.assertEqual(loaded["paypal_status"], "ADMIN_GRANTED")

    def test_guest_subscription_state_migrates_into_authenticated_account_key(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            with patch("spotdl.utils.web.songzip_store", store):
                store.save_subscription(
                    "guest-key",
                    {
                        "tier": "plus",
                        "downloads_used": 18,
                        "downloads_lifetime": 26,
                        "subscription_id": "sub_live",
                        "activated_at": "2026-05-16T08:00:00-05:00",
                        "paypal_status": "ACTIVE",
                    },
                )
                store.save_subscription(
                    "acct-user",
                    {
                        "tier": "free",
                        "downloads_used": 0,
                        "downloads_lifetime": 7,
                        "subscription_id": None,
                        "activated_at": None,
                        "paypal_status": None,
                    },
                )
                store.save_paypal_subscription(
                    "sub_live",
                    {
                        "subscription_id": "sub_live",
                        "account_key": "guest-key",
                        "tier": "plus",
                        "status": "ACTIVE",
                        "plan_id": "plan_plus",
                        "activated_at": "2026-05-16T08:00:00-05:00",
                    },
                )

                migrated = _migrate_subscription_state("guest-key", "acct-user")
                guest_after = _load_subscription_state_for_key("guest-key")
                record = store.load_paypal_subscription("sub_live")

        self.assertEqual(migrated["tier"], "plus")
        self.assertEqual(migrated["downloads_used"], 18)
        self.assertEqual(migrated["downloads_lifetime"], 33)
        self.assertEqual(migrated["membership_source"], "paypal")
        self.assertEqual(migrated["subscription_id"], "sub_live")
        self.assertEqual(guest_after["tier"], "free")
        self.assertEqual(record["account_key"], "acct-user")

    def test_get_client_refresh_uses_authenticated_account_membership(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account("member@example.com", "supersecret123")
            store.set_account_membership("member@example.com", "pro")
            session_token = store.create_session(account["id"], user_agent="pytest")
            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/api/session/state",
                "raw_path": b"/api/session/state",
                "query_string": b"",
                "headers": [
                    (b"host", b"songzip.onrender.com"),
                    (b"cookie", f"songzip_session={session_token}".encode("utf-8")),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("songzip.onrender.com", 443),
            }
            request = Request(scope)

            mock_downloader = Mock()
            mock_downloader.progress_handler = Mock()

            with patch("spotdl.utils.web.songzip_store", store), patch(
                "spotdl.utils.web.Downloader",
                return_value=mock_downloader,
            ), patch(
                "spotdl.utils.web.Client._refresh_completed_downloads_from_output",
                return_value=None,
            ), patch("spotdl.utils.web.app_state") as mock_app_state:
                mock_app_state.downloader_settings = {}
                mock_app_state.loop = None
                mock_app_state.clients = {}
                mock_app_state.logger = Mock()
                mock_app_state.web_settings = {
                    "web_use_output_dir": False,
                    "host": "0.0.0.0",
                    "port": 10000,
                    "keep_alive": True,
                }
                client = get_client(
                    client_id="guest-browser-key",
                    account_key="guest-browser-key",
                    request=request,
                )

        self.assertEqual(client.account_key, account["account_key"])
        self.assertEqual(client.subscription["tier"], "pro")
        self.assertEqual(client.subscription["membership_source"], "admin")

    def test_session_snapshot_active_count_excludes_queued_songs(self):
        client = Client.__new__(Client)
        client.client_id = "snapshot-client"
        client.account_key = "acct-member"
        client.authenticated_account = None
        client.current_job = {
            "status": "running",
            "resolved_count": 4,
            "output_root": "downloads",
        }
        client.subscription = {
            "tier": "pro",
            "downloads_used": 0,
            "downloads_lifetime": 0,
            "membership_source": "admin",
            "bonus_credits": 0,
            "subscription_id": None,
            "activated_at": None,
            "paypal_status": "ADMIN_GRANTED",
        }
        client.completed_downloads = []
        client.download_bundle = None
        client.events = []
        client.latest_update = None
        client.get_output_root = lambda: "downloads"  # type: ignore[method-assign]
        client.get_subscription_snapshot = lambda: {  # type: ignore[method-assign]
            "tier": "pro",
            "account_key": "acct-member",
        }
        client.song_states = {
            "a": {"status": "queued", "progress": 0, "queue_position": 1},
            "b": {"status": "queued", "progress": 0, "queue_position": 2},
            "c": {"status": "downloading", "progress": 12, "queue_position": 3},
            "d": {"status": "done", "progress": 100, "queue_position": 4},
        }

        with patch("spotdl.utils.web.app_state") as mock_app_state:
            mock_app_state.web_settings = {
                "host": "0.0.0.0",
                "port": 10000,
                "keep_alive": True,
                "web_use_output_dir": False,
            }
            snapshot = client.get_state_snapshot()

        self.assertEqual(snapshot["stats"]["queued"], 2)
        self.assertEqual(snapshot["stats"]["active"], 1)


if __name__ == "__main__":
    unittest.main()
