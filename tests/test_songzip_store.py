import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spotdl.utils.songzip_store import SongZipStore, SongZipStoreError


class SongZipStoreTest(unittest.TestCase):
    def test_register_authenticate_and_session_roundtrip(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account("test@example.com", "password123")
            self.assertEqual(account["email"], "test@example.com")
            self.assertTrue(account["account_key"].startswith("acct-"))

            authenticated = store.authenticate_account(
                "test@example.com",
                "password123",
            )
            self.assertEqual(authenticated["account_key"], account["account_key"])

            token = store.create_session(account["id"], user_agent="unit-test")
            from_session = store.get_account_by_session(token)
            self.assertIsNotNone(from_session)
            self.assertEqual(from_session["email"], "test@example.com")

    def test_duplicate_email_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.register_account("test@example.com", "password123")
            with self.assertRaises(SongZipStoreError):
                store.register_account("test@example.com", "password123")

    def test_register_account_prefers_available_account_key(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account(
                "test@example.com",
                "password123",
                preferred_account_key="shared-demo-key",
            )

        self.assertEqual(account["account_key"], "shared-demo-key")

    def test_subscription_and_paypal_record_persist(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_subscription(
                "acct-demo",
                {
                    "tier": "plus",
                    "downloads_used": 12,
                    "subscription_id": "sub_123",
                    "activated_at": "2026-05-16T07:00:00-05:00",
                    "paypal_status": "ACTIVE",
                },
            )
            subscription = store.load_subscription("acct-demo")
            self.assertEqual(subscription["tier"], "plus")
            self.assertEqual(subscription["downloads_used"], 12)

            store.save_paypal_subscription(
                "sub_123",
                {
                    "subscription_id": "sub_123",
                    "account_key": "acct-demo",
                    "tier": "plus",
                    "status": "ACTIVE",
                    "plan_id": "plan_plus",
                    "activated_at": "2026-05-16T07:00:00-05:00",
                    "last_event": {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED"},
                },
            )
            record = store.load_paypal_subscription("sub_123")
            self.assertIsNotNone(record)
            self.assertEqual(record["tier"], "plus")
            self.assertEqual(record["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
