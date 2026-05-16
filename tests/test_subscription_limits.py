import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from spotdl.utils.web import (
    Client,
    FREE_TIER_DOWNLOAD_LIMIT,
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
        }
        client.pending_upgrade_prompt = None
        client._save_subscription_state = lambda: None  # type: ignore[method-assign]

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
        }
        client.pending_upgrade_prompt = None
        client._save_subscription_state = lambda: None  # type: ignore[method-assign]

        songs = ["one", "two", "three"]
        allowed, overflow = client._reserve_download_capacity(songs)  # pylint: disable=protected-access

        self.assertEqual(allowed, songs)
        self.assertEqual(overflow, 0)
        self.assertIsNone(client.pending_upgrade_prompt)

    def test_subscription_state_persists_for_activation_flow(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            client = Client.__new__(Client)
            client.client_id = "persist-test"
            client.account_key = "shared-key"
            client.subscription = {
                "tier": "basic",
                "downloads_used": 12,
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

    def test_guest_subscription_state_migrates_into_authenticated_account_key(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            with patch("spotdl.utils.web.songzip_store", store):
                store.save_subscription(
                    "guest-key",
                    {
                        "tier": "plus",
                        "downloads_used": 18,
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
        self.assertEqual(migrated["subscription_id"], "sub_live")
        self.assertEqual(guest_after["tier"], "free")
        self.assertEqual(record["account_key"], "acct-user")


if __name__ == "__main__":
    unittest.main()
