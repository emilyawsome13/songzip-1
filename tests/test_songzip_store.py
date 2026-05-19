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

    def test_set_account_membership_updates_subscription(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account("member@example.com", "password123")

            upgraded = store.set_account_membership(account["email"], "plus")
            upgraded_events = store.list_subscription_usage_events(account["account_key"])

            downgraded = store.set_account_membership(account["account_key"], "free")
            downgraded_events = store.list_subscription_usage_events(account["account_key"])

        self.assertEqual(upgraded["subscription"]["tier"], "plus")
        self.assertEqual(upgraded["subscription"]["membership_source"], "admin")
        self.assertEqual(upgraded["subscription"]["paypal_status"], "ADMIN_GRANTED")
        self.assertEqual(upgraded_events[0]["event_type"], "membership_changed")
        self.assertEqual(upgraded_events[0]["details"]["new_tier"], "plus")
        self.assertEqual(downgraded["subscription"]["tier"], "free")
        self.assertEqual(downgraded["subscription"]["membership_source"], "free")
        self.assertIsNone(downgraded["subscription"]["subscription_id"])
        self.assertEqual(downgraded["subscription"]["paypal_status"], "ADMIN_CANCELLED")
        self.assertEqual(downgraded_events[0]["details"]["new_tier"], "free")

    def test_subscription_backup_restores_membership_when_row_is_missing(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_subscription(
                "acct-backup",
                {
                    "tier": "pro",
                    "downloads_used": 5,
                    "downloads_lifetime": 105,
                    "membership_source": "admin",
                    "bonus_credits": 10000,
                    "subscription_id": None,
                    "activated_at": "2026-05-18T00:00:00-05:00",
                    "paypal_status": "ADMIN_GRANTED",
                },
            )

            with store._managed_connection() as connection:  # pylint: disable=protected-access
                connection.execute(
                    "DELETE FROM subscriptions WHERE account_key = ?",
                    ("acct-backup",),
                )

            restored = store.load_subscription("acct-backup")

        self.assertEqual(restored["tier"], "pro")
        self.assertEqual(restored["membership_source"], "admin")
        self.assertEqual(restored["bonus_credits"], 10000)
        self.assertEqual(restored["downloads_lifetime"], 105)

    def test_membership_persists_in_dedicated_database_row(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account("persist@example.com", "password123")
            store.set_account_membership(account["email"], "pro")
            store.grant_bonus_credits(account["email"], 500)

            with store._managed_connection() as connection:  # pylint: disable=protected-access
                row = connection.execute(
                    """
                    SELECT tier, membership_source, bonus_credits, paypal_status
                    FROM account_memberships
                    WHERE account_key = ?
                    """,
                    (account["account_key"],),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["tier"], "pro")
        self.assertEqual(row["membership_source"], "admin")
        self.assertEqual(int(row["bonus_credits"] or 0), 500)
        self.assertEqual(row["paypal_status"], "ADMIN_GRANTED")

    def test_stale_free_save_does_not_overwrite_admin_membership(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            account = store.register_account("sticky@example.com", "password123")
            store.set_account_membership(account["email"], "pro")
            store.grant_bonus_credits(account["email"], 10000)

            store.save_subscription(
                account["account_key"],
                {
                    "tier": "free",
                    "downloads_used": 12,
                    "downloads_lifetime": 88,
                    "membership_source": "free",
                    "bonus_credits": 0,
                    "subscription_id": None,
                    "activated_at": None,
                    "paypal_status": None,
                },
            )
            reloaded = store.load_subscription(account["account_key"])

        self.assertEqual(reloaded["tier"], "pro")
        self.assertEqual(reloaded["membership_source"], "admin")
        self.assertEqual(reloaded["bonus_credits"], 10000)
        self.assertEqual(reloaded["downloads_used"], 12)
        self.assertEqual(reloaded["downloads_lifetime"], 88)

    def test_stale_save_does_not_reduce_usage_counters(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_subscription(
                "acct-usage",
                {
                    "tier": "free",
                    "downloads_used": 42,
                    "downloads_lifetime": 142,
                    "membership_source": "free",
                    "bonus_credits": 0,
                    "subscription_id": None,
                    "activated_at": None,
                    "paypal_status": None,
                },
            )
            store.save_subscription(
                "acct-usage",
                {
                    "tier": "free",
                    "downloads_used": 4,
                    "downloads_lifetime": 14,
                    "membership_source": "free",
                    "bonus_credits": 0,
                    "subscription_id": None,
                    "activated_at": None,
                    "paypal_status": None,
                },
            )
            reloaded = store.load_subscription("acct-usage")

        self.assertEqual(reloaded["downloads_used"], 42)
        self.assertEqual(reloaded["downloads_lifetime"], 142)

    def test_account_settings_round_trip_and_migration(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_account_settings(
                "guest-key",
                {
                    "threads": 3,
                    "preload": True,
                    "format": "mp3",
                },
            )
            store.save_account_settings(
                "acct-user",
                {
                    "threads": 1,
                    "format": "m4a",
                },
            )

            merged = store.migrate_account_settings("guest-key", "acct-user")
            reloaded = store.load_account_settings("acct-user")
            source_after = store.load_account_settings("guest-key")

        self.assertEqual(merged["threads"], 3)
        self.assertEqual(merged["preload"], True)
        self.assertEqual(reloaded["format"], "mp3")
        self.assertEqual(source_after, {})

    def test_client_snapshot_round_trip(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_client_snapshot(
                "browser-1",
                "acct-demo",
                {
                    "job": {"status": "interrupted", "query": "artist url"},
                    "song_states": [{"key": "song-1", "status": "queued"}],
                    "events": [{"kind": "job", "message": "Queued"}],
                },
            )

            restored = store.load_client_snapshot("browser-1")

        self.assertIsNotNone(restored)
        self.assertEqual(restored["account_key"], "acct-demo")
        self.assertEqual(restored["job"]["status"], "interrupted")
        self.assertEqual(restored["song_states"][0]["key"], "song-1")

    def test_latest_paypal_subscription_for_account_prefers_newest_record(self):
        with TemporaryDirectory() as temp_dir:
            store = SongZipStore(Path(temp_dir) / "songzip.sqlite3")
            store.save_paypal_subscription(
                "sub_old",
                {
                    "subscription_id": "sub_old",
                    "account_key": "acct-demo",
                    "tier": "basic",
                    "status": "ACTIVE",
                    "plan_id": "plan_basic",
                    "activated_at": "2026-05-10T00:00:00+00:00",
                    "last_event": {"event_type": "BILLING.SUBSCRIPTION.ACTIVATED"},
                },
            )
            store.save_paypal_subscription(
                "sub_new",
                {
                    "subscription_id": "sub_new",
                    "account_key": "acct-demo",
                    "tier": "pro",
                    "status": "ACTIVE",
                    "plan_id": "plan_pro",
                    "activated_at": "2026-05-11T00:00:00+00:00",
                    "last_event": {"event_type": "BILLING.SUBSCRIPTION.UPDATED"},
                },
            )

            latest = store.load_latest_paypal_subscription_for_account("acct-demo")

        self.assertIsNotNone(latest)
        self.assertEqual(latest["subscription_id"], "sub_new")
        self.assertEqual(latest["tier"], "pro")


if __name__ == "__main__":
    unittest.main()
