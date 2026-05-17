"""
SQLite-backed account and subscription storage for SongZip.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from spotdl.utils.config import get_spotdl_path

__all__ = [
    "SongZipStore",
    "SongZipStoreError",
    "songzip_store",
]

ACCOUNT_KEY_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_TTL_DAYS = 30
PASSWORD_ITERATIONS = 240_000
GOOGLE_PASSWORD_PLACEHOLDER = "oauth_google"
ADMIN_ACCOUNT_META_KEY = "songzip_admin_account_key"


class SongZipStoreError(Exception):
    """
    Raised when SongZip account storage operations fail.
    """


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat()


def _normalize_account_key(value: Optional[str]) -> str:
    cleaned = ACCOUNT_KEY_PATTERN.sub("-", str(value or "").strip().lower()).strip("-_")
    return cleaned[:64]


def _normalize_email(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _make_password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt_b64}${digest_b64}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, raw_iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    try:
        iterations = int(raw_iterations)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.b64decode(digest_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False

    computed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(computed, expected_digest)


class SongZipStore:
    """
    SQLite-backed persistence for SongZip accounts and subscriptions.
    """

    def __init__(self, database_path: Optional[Path] = None):
        self.database_path = database_path or (
            get_spotdl_path() / "web" / "songzip.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _managed_connection(self):
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    @staticmethod
    def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        if column_name in self._column_names(connection, table_name):
            return

        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )

    def _ensure_schema(self):
        with self._managed_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    account_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_sessions (
                    session_token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    user_agent TEXT,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    account_key TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    downloads_used INTEGER NOT NULL,
                    downloads_lifetime INTEGER NOT NULL DEFAULT 0,
                    subscription_id TEXT,
                    activated_at TEXT,
                    paypal_status TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paypal_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    account_key TEXT,
                    tier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_id TEXT,
                    activated_at TEXT,
                    updated_at TEXT NOT NULL,
                    last_event_json TEXT
                );

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscription_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    song_count INTEGER NOT NULL DEFAULT 0,
                    tier TEXT,
                    subscription_id TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(
                connection,
                "accounts",
                "auth_provider",
                "TEXT NOT NULL DEFAULT 'local'",
            )
            self._ensure_column(connection, "accounts", "provider_subject", "TEXT")
            self._ensure_column(connection, "accounts", "display_name", "TEXT")
            self._ensure_column(
                connection,
                "subscriptions",
                "bonus_credits",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "subscriptions",
                "downloads_lifetime",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider_subject
                ON accounts(provider_subject)
                WHERE provider_subject IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_subscription_usage_events_account_created
                ON subscription_usage_events(account_key, created_at DESC)
                """
            )

    @staticmethod
    def _public_account(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "email": row["email"],
            "account_key": row["account_key"],
            "auth_provider": row["auth_provider"] or "local",
            "provider_subject": row["provider_subject"],
            "display_name": row["display_name"] or row["email"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _session_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _account_key_exists(self, account_key: str) -> bool:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM accounts WHERE account_key = ?",
                (account_key,),
            ).fetchone()

        return row is not None

    def _build_account_key(self, preferred_account_key: Optional[str] = None) -> str:
        preferred = _normalize_account_key(preferred_account_key)
        if preferred and not self._account_key_exists(preferred):
            return preferred

        while True:
            generated = _normalize_account_key(f"acct-{secrets.token_urlsafe(10)}")
            if generated and not self._account_key_exists(generated):
                return generated

    def get_meta(self, key: str) -> Optional[str]:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?",
                (str(key),),
            ).fetchone()

        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO app_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (str(key), str(value), _timestamp()),
            )

    def get_admin_account_key(self) -> Optional[str]:
        value = self.get_meta(ADMIN_ACCOUNT_META_KEY)
        return _normalize_account_key(value) or None

    def claim_admin_account(self, account_key: str) -> str:
        normalized_key = _normalize_account_key(account_key)
        if not normalized_key:
            raise SongZipStoreError("Admin account key is not valid.")

        existing = self.get_admin_account_key()
        if existing:
            return existing

        self.set_meta(ADMIN_ACCOUNT_META_KEY, normalized_key)
        return normalized_key

    def get_account_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        raw_value = str(identifier or "").strip()
        if not raw_value:
            return None

        normalized_email = _normalize_email(raw_value)
        normalized_key = _normalize_account_key(raw_value)

        with self._managed_connection() as connection:
            row = None
            if EMAIL_PATTERN.match(normalized_email):
                row = connection.execute(
                    "SELECT * FROM accounts WHERE email = ?",
                    (normalized_email,),
                ).fetchone()

            if row is None and normalized_key:
                row = connection.execute(
                    "SELECT * FROM accounts WHERE account_key = ?",
                    (normalized_key,),
                ).fetchone()

        if row is None:
            return None

        return self._public_account(row)

    def register_account(
        self,
        email: str,
        password: str,
        preferred_account_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_email = _normalize_email(email)
        if not EMAIL_PATTERN.match(normalized_email):
            raise SongZipStoreError("Enter a valid email address.")

        if len(password or "") < 8:
            raise SongZipStoreError("Use a password with at least 8 characters.")

        with self._managed_connection() as connection:
            existing = connection.execute(
                "SELECT 1 FROM accounts WHERE email = ?",
                (normalized_email,),
            ).fetchone()
        if existing is not None:
            raise SongZipStoreError("An account with that email already exists.")

        account_key = self._build_account_key(preferred_account_key)
        now = _timestamp()
        password_hash = _make_password_hash(password)

        try:
            with self._managed_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO accounts (
                        email,
                        account_key,
                        password_hash,
                        auth_provider,
                        provider_subject,
                        display_name,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_email,
                        account_key,
                        password_hash,
                        "local",
                        None,
                        normalized_email,
                        now,
                        now,
                    ),
                )
                account_row = connection.execute(
                    "SELECT * FROM accounts WHERE email = ?",
                    (normalized_email,),
                ).fetchone()
        except sqlite3.IntegrityError as error:
            raise SongZipStoreError(
                "Account registration could not reserve that SongZip account key."
            ) from error

        if account_row is None:
            raise SongZipStoreError("Account registration did not complete.")

        return self._public_account(account_row)

    def authenticate_account(self, email: str, password: str) -> Dict[str, Any]:
        normalized_email = _normalize_email(email)
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE email = ?",
                (normalized_email,),
            ).fetchone()

        if row is None:
            raise SongZipStoreError("Email or password is incorrect.")

        if str(row["auth_provider"] or "local") == "google":
            raise SongZipStoreError("Use Google sign-in for this account.")

        if not _verify_password(password, row["password_hash"]):
            raise SongZipStoreError("Email or password is incorrect.")

        return self._public_account(row)

    def get_or_create_google_account(
        self,
        email: str,
        google_subject: str,
        display_name: Optional[str] = None,
        preferred_account_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_email = _normalize_email(email)
        subject = str(google_subject or "").strip()
        resolved_display_name = str(display_name or normalized_email).strip() or normalized_email
        if not EMAIL_PATTERN.match(normalized_email):
            raise SongZipStoreError("Google sign-in did not return a valid email address.")

        if not subject:
            raise SongZipStoreError("Google sign-in did not return an account identifier.")

        now = _timestamp()
        with self._managed_connection() as connection:
            provider_row = connection.execute(
                "SELECT * FROM accounts WHERE provider_subject = ?",
                (subject,),
            ).fetchone()
            if provider_row is not None:
                connection.execute(
                    """
                    UPDATE accounts
                    SET email = ?, display_name = ?, auth_provider = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_email,
                        resolved_display_name,
                        "google",
                        now,
                        provider_row["id"],
                    ),
                )
                provider_row = connection.execute(
                    "SELECT * FROM accounts WHERE id = ?",
                    (provider_row["id"],),
                ).fetchone()
                return self._public_account(provider_row)

            email_row = connection.execute(
                "SELECT * FROM accounts WHERE email = ?",
                (normalized_email,),
            ).fetchone()
            if email_row is not None:
                existing_subject = str(email_row["provider_subject"] or "").strip()
                if existing_subject and existing_subject != subject:
                    raise SongZipStoreError(
                        "That email address is already linked to a different Google account."
                    )

                connection.execute(
                    """
                    UPDATE accounts
                    SET auth_provider = ?, provider_subject = ?, display_name = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "google",
                        subject,
                        resolved_display_name,
                        now,
                        email_row["id"],
                    ),
                )
                email_row = connection.execute(
                    "SELECT * FROM accounts WHERE id = ?",
                    (email_row["id"],),
                ).fetchone()
                return self._public_account(email_row)

            account_key = self._build_account_key(preferred_account_key)
            connection.execute(
                """
                INSERT INTO accounts (
                    email,
                    account_key,
                    password_hash,
                    auth_provider,
                    provider_subject,
                    display_name,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_email,
                    account_key,
                    GOOGLE_PASSWORD_PLACEHOLDER,
                    "google",
                    subject,
                    resolved_display_name,
                    now,
                    now,
                ),
            )
            created_row = connection.execute(
                "SELECT * FROM accounts WHERE provider_subject = ?",
                (subject,),
            ).fetchone()

        if created_row is None:
            raise SongZipStoreError("Google sign-in could not finish creating the account.")

        return self._public_account(created_row)

    def create_session(
        self,
        account_id: int,
        user_agent: Optional[str] = None,
    ) -> str:
        session_token = secrets.token_urlsafe(32)
        now = _utc_now()
        expires_at = now + dt.timedelta(days=SESSION_TTL_DAYS)

        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO account_sessions (
                    session_token_hash,
                    account_id,
                    created_at,
                    expires_at,
                    last_seen_at,
                    user_agent
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self._session_hash(session_token),
                    account_id,
                    now.isoformat(),
                    expires_at.isoformat(),
                    now.isoformat(),
                    user_agent,
                ),
            )

        return session_token

    def get_account_by_session(self, session_token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not session_token:
            return None

        session_hash = self._session_hash(session_token)
        with self._managed_connection() as connection:
            row = connection.execute(
                """
                SELECT accounts.*
                FROM account_sessions
                INNER JOIN accounts ON accounts.id = account_sessions.account_id
                WHERE account_sessions.session_token_hash = ?
                """,
                (session_hash,),
            ).fetchone()

            session_row = connection.execute(
                "SELECT expires_at FROM account_sessions WHERE session_token_hash = ?",
                (session_hash,),
            ).fetchone()

            if session_row is None:
                return None

            expires_at = dt.datetime.fromisoformat(session_row["expires_at"])
            if expires_at <= _utc_now():
                connection.execute(
                    "DELETE FROM account_sessions WHERE session_token_hash = ?",
                    (session_hash,),
                )
                return None

            connection.execute(
                "UPDATE account_sessions SET last_seen_at = ? WHERE session_token_hash = ?",
                (_timestamp(), session_hash),
            )

        if row is None:
            return None

        return self._public_account(row)

    def delete_session(self, session_token: Optional[str]) -> None:
        if not session_token:
            return

        with self._managed_connection() as connection:
            connection.execute(
                "DELETE FROM account_sessions WHERE session_token_hash = ?",
                (self._session_hash(session_token),),
            )

    def load_subscription(self, account_key: str) -> Dict[str, Any]:
        normalized_key = _normalize_account_key(account_key)
        default_state = {
            "tier": "free",
            "downloads_used": 0,
            "downloads_lifetime": 0,
            "bonus_credits": 0,
            "subscription_id": None,
            "activated_at": None,
            "paypal_status": None,
            "updated_at": _timestamp(),
        }

        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE account_key = ?",
                (normalized_key,),
            ).fetchone()

        if row is None:
            return default_state

        default_state.update(
            {
                "tier": row["tier"],
                "downloads_used": int(row["downloads_used"] or 0),
                "downloads_lifetime": int(row["downloads_lifetime"] or 0),
                "bonus_credits": int(row["bonus_credits"] or 0),
                "subscription_id": row["subscription_id"],
                "activated_at": row["activated_at"],
                "paypal_status": row["paypal_status"],
                "updated_at": row["updated_at"],
            }
        )
        return default_state

    def save_subscription(self, account_key: str, state: Dict[str, Any]) -> None:
        normalized_key = _normalize_account_key(account_key)
        updated_at = _timestamp()
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO subscriptions (
                    account_key,
                    tier,
                    downloads_used,
                    downloads_lifetime,
                    bonus_credits,
                    subscription_id,
                    activated_at,
                    paypal_status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    tier = excluded.tier,
                    downloads_used = excluded.downloads_used,
                    downloads_lifetime = excluded.downloads_lifetime,
                    bonus_credits = excluded.bonus_credits,
                    subscription_id = excluded.subscription_id,
                    activated_at = excluded.activated_at,
                    paypal_status = excluded.paypal_status,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_key,
                    state.get("tier", "free"),
                    int(state.get("downloads_used", 0) or 0),
                    int(state.get("downloads_lifetime", 0) or 0),
                    int(state.get("bonus_credits", 0) or 0),
                    state.get("subscription_id"),
                    state.get("activated_at"),
                    state.get("paypal_status"),
                    updated_at,
                ),
            )

    def record_subscription_usage_event(
        self,
        account_key: str,
        event_type: str,
        song_count: int = 0,
        tier: Optional[str] = None,
        subscription_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_key = _normalize_account_key(account_key)
        if not normalized_key:
            raise SongZipStoreError("Cannot store usage for an invalid SongZip account key.")

        resolved_event_type = str(event_type or "").strip().lower()
        if not resolved_event_type:
            raise SongZipStoreError("Usage event type is required.")

        payload = json.dumps(details) if isinstance(details, dict) else None
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO subscription_usage_events (
                    account_key,
                    event_type,
                    song_count,
                    tier,
                    subscription_id,
                    details_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_key,
                    resolved_event_type,
                    int(song_count or 0),
                    tier,
                    subscription_id,
                    payload,
                    _timestamp(),
                ),
            )

    def list_subscription_usage_events(
        self,
        account_key: str,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        normalized_key = _normalize_account_key(account_key)
        resolved_limit = max(1, min(int(limit or 50), 250))
        with self._managed_connection() as connection:
            rows = connection.execute(
                """
                SELECT account_key, event_type, song_count, tier, subscription_id, details_json, created_at
                FROM subscription_usage_events
                WHERE account_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (normalized_key, resolved_limit),
            ).fetchall()

        events: list[Dict[str, Any]] = []
        for row in rows:
            details = None
            if row["details_json"]:
                try:
                    details = json.loads(row["details_json"])
                except ValueError:
                    details = None

            events.append(
                {
                    "account_key": row["account_key"],
                    "event_type": row["event_type"],
                    "song_count": int(row["song_count"] or 0),
                    "tier": row["tier"],
                    "subscription_id": row["subscription_id"],
                    "details": details,
                    "created_at": row["created_at"],
                }
            )

        return events

    def grant_bonus_credits(
        self,
        account_identifier: str,
        credits: int,
    ) -> Dict[str, Any]:
        resolved_credits = int(credits or 0)
        if resolved_credits <= 0:
            raise SongZipStoreError("Grant at least 1 song credit.")

        account = self.get_account_by_identifier(account_identifier)
        if account is None:
            raise SongZipStoreError("No SongZip account matched that email or account key.")

        subscription = self.load_subscription(account["account_key"])
        subscription["bonus_credits"] = int(subscription.get("bonus_credits", 0) or 0) + resolved_credits
        self.save_subscription(account["account_key"], subscription)
        self.record_subscription_usage_event(
            account["account_key"],
            "credits_granted",
            song_count=resolved_credits,
            tier=subscription.get("tier"),
            subscription_id=subscription.get("subscription_id"),
            details={"account_identifier": account_identifier},
        )

        return {
            "account": account,
            "subscription": self.load_subscription(account["account_key"]),
        }

    def load_paypal_subscription(self, subscription_id: str) -> Optional[Dict[str, Any]]:
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT * FROM paypal_subscriptions WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchone()

        if row is None:
            return None

        event_payload = None
        if row["last_event_json"]:
            try:
                event_payload = json.loads(row["last_event_json"])
            except ValueError:
                event_payload = None

        return {
            "subscription_id": row["subscription_id"],
            "account_key": row["account_key"],
            "tier": row["tier"],
            "status": row["status"],
            "plan_id": row["plan_id"],
            "activated_at": row["activated_at"],
            "updated_at": row["updated_at"],
            "last_event": event_payload,
        }

    def save_paypal_subscription(self, subscription_id: str, record: Dict[str, Any]) -> None:
        with self._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO paypal_subscriptions (
                    subscription_id,
                    account_key,
                    tier,
                    status,
                    plan_id,
                    activated_at,
                    updated_at,
                    last_event_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    account_key = excluded.account_key,
                    tier = excluded.tier,
                    status = excluded.status,
                    plan_id = excluded.plan_id,
                    activated_at = excluded.activated_at,
                    updated_at = excluded.updated_at,
                    last_event_json = excluded.last_event_json
                """,
                (
                    subscription_id,
                    record.get("account_key"),
                    record.get("tier", "free"),
                    record.get("status", "UNKNOWN"),
                    record.get("plan_id"),
                    record.get("activated_at"),
                    _timestamp(),
                    json.dumps(record.get("last_event")) if record.get("last_event") else None,
                ),
            )


songzip_store = SongZipStore()
