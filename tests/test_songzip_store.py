import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spotdl.utils.songzip_store import (
    GOOGLE_PASSWORD_PLACEHOLDER,
    SongZipStore,
    SongZipStoreError,
)


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
                    "downloads_lifetime": 44,
                    "subscription_id": "sub_123",
                    "activated_at": "2026-05-16T07:00:00-05:00",
                    "paypal_status": "ACTIVE",
                },
            )
            subscription = store.load_subscription("acct-demo")
            self.assertEqual(subscription["tier"], "plus")
            self.assertEqual(subscription["downloads_used"], 12)
            self.assertEqual(subscription["downloads_lifetime"], 44)

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

    def test_google_account_reuses_email_and_disables_password_login(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            local_account = store.register_account("test@example.com", "password123")
            google_account = store.get_or_create_google_account(
                "test@example.com",
                "google-subject-123",
                display_name="Test Person",
            )
            with self.assertRaises(SongZipStoreError):
                store.authenticate_account("test@example.com", "password123")

        self.assertEqual(google_account["account_key"], local_account["account_key"])
        self.assertEqual(google_account["auth_provider"], "google")
        self.assertEqual(google_account["provider_subject"], "google-subject-123")
        self.assertEqual(google_account["display_name"], "Test Person")

    def test_google_account_creation_uses_oauth_placeholder_password(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.get_or_create_google_account(
                "google@example.com",
                "google-subject-xyz",
                display_name="Google User",
            )

            with store._managed_connection() as connection:  # pylint: disable=protected-access
                row = connection.execute(
                    "SELECT password_hash FROM accounts WHERE id = ?",
                    (account["id"],),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["password_hash"], GOOGLE_PASSWORD_PLACEHOLDER)

    def test_grant_bonus_credits_updates_subscription(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account("credits@example.com", "password123")
            result = store.grant_bonus_credits(account["email"], 25)
            stored_bonus_credits = store.load_subscription(account["account_key"])[
                "bonus_credits"
            ]
            events = store.list_subscription_usage_events(account["account_key"])

        self.assertEqual(result["account"]["account_key"], account["account_key"])
        self.assertEqual(result["subscription"]["bonus_credits"], 25)
        self.assertEqual(stored_bonus_credits, 25)
        self.assertEqual(events[0]["event_type"], "credits_granted")
        self.assertEqual(events[0]["song_count"], 25)


if __name__ == "__main__":
    unittest.main()
