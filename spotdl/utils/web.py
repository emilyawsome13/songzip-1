"""
Module which contains the web server related function
FastAPI routes/classes etc.
"""

import argparse
import asyncio
import base64
import datetime
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import Scope
from uvicorn import Server
from websockets.exceptions import ConnectionClosed

from spotdl._version import __version__
from spotdl.download.downloader import Downloader, DownloaderError
from spotdl.download.progress_handler import ProgressHandler, SongTracker
from spotdl.types.album import Album
from spotdl.types.artist import Artist
from spotdl.types.options import (
    DownloaderOptionalOptions,
    DownloaderOptions,
    WebOptions,
)
from spotdl.types.playlist import Playlist
from spotdl.types.song import Song
from spotdl.utils.arguments import create_parser
from spotdl.utils.config import (
    DOWNLOADER_OPTIONS,
    SPOTIFY_OPTIONS,
    create_settings_type,
    get_spotdl_path,
)
from spotdl.utils.github import RateLimitError, get_latest_version, get_status
from spotdl.utils.provider_auth import ProviderAuthError, provider_auth_manager
from spotdl.utils.songzip_store import SongZipStoreError, songzip_store
from spotdl.utils.search import get_search_results, get_simple_songs
from spotdl.utils.spotify import SpotifyClient, SpotifyError

__all__ = [
    "ALLOWED_ORIGINS",
    "SPAStaticFiles",
    "Client",
    "ApplicationState",
    "router",
    "app_state",
    "get_current_state",
    "get_client",
    "websocket_endpoint",
    "song_from_url",
    "query_search",
    "session_state",
    "download_query",
    "download_url",
    "download_file",
    "download_bundle",
    "get_settings",
    "update_settings",
    "fix_mime_types",
    "ensure_spotify_client_initialized",
]

FREE_TIER_DOWNLOAD_LIMIT = 50
SUPPORTED_SUBSCRIPTION_TIERS = {"free", "basic", "plus", "pro"}
SUBSCRIPTION_TIER_PRIORITY = {
    "free": 0,
    "basic": 1,
    "plus": 2,
    "pro": 3,
}
ACCOUNT_KEY_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")
PAYPAL_SUBSCRIPTION_STATUS_MAP = {
    "BILLING.SUBSCRIPTION.ACTIVATED": "ACTIVE",
    "BILLING.SUBSCRIPTION.UPDATED": "ACTIVE",
    "BILLING.SUBSCRIPTION.SUSPENDED": "SUSPENDED",
    "BILLING.SUBSCRIPTION.CANCELLED": "CANCELLED",
    "BILLING.SUBSCRIPTION.EXPIRED": "EXPIRED",
    "PAYMENT.SALE.COMPLETED": "ACTIVE",
}
PAYPAL_ACTIVE_STATUSES = {"ACTIVE", "APPROVAL_PENDING", "APPROVED", "LOCAL_APPROVED"}
PAYPAL_TERMINAL_STATUSES = {"CANCELLED", "SUSPENDED", "EXPIRED", "FREE"}
PAYPAL_PLAN_TO_TIER = {
    os.environ.get("SONGZIP_PAYPAL_BASIC_PLAN_ID", "P-68Y262703G6930321NID6XTQ"): "basic",
    os.environ.get("SONGZIP_PAYPAL_PLUS_PLAN_ID", "P-95499278FS551045NNID6Y2Y"): "plus",
    os.environ.get("SONGZIP_PAYPAL_PRO_PLAN_ID", "P-3HV972983J415051HNID6Z2A"): "pro",
}
SONGZIP_SESSION_COOKIE = "songzip_session"
GOOGLE_ACCOUNT_OAUTH_SCOPES = ["openid", "email", "profile"]
GOOGLE_ACCOUNT_STATE_TTL_SECONDS = 900
COOKIE_META_PREFIX = "songzip_cookie_file"
COOKIE_FILE_HEADERS = {
    "# netscape http cookie file",
    "# http cookie file",
}
_pending_google_account_states: Dict[str, Dict[str, str]] = {}


class SubscriptionLimitError(Exception):
    """
    Raised when the active subscription tier cannot accept more songs.
    """

    def __init__(self, message: str, prompt: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.prompt = prompt or {}


def ensure_spotify_client_initialized(logger: Optional[logging.Logger] = None) -> bool:
    """
    Ensure the shared Spotify metadata client is ready for hosted web usage.

    ### Arguments
    - logger: optional logger for diagnostics

    ### Returns
    - True when the Spotify client is available
    """

    try:
        SpotifyClient()
        return True
    except SpotifyError:
        pass

    spotify_settings = dict(SPOTIFY_OPTIONS)
    spotify_settings["headless"] = True

    try:
        SpotifyClient.init(**spotify_settings)
        if logger is not None:
            logger.info("Spotify metadata client initialized for SongZip.")
        return True
    except SpotifyError as exception:
        if "already been initialized" in str(exception):
            return True

        if logger is not None:
            logger.warning("Spotify metadata client unavailable: %s", exception)

        return False


def _friendly_job_error_message(exception: Exception) -> str:
    """
    Convert backend exceptions into cleaner dashboard-facing messages.
    """

    message = str(exception).strip() or exception.__class__.__name__

    if "Spotify client not created" in message:
        return (
            "Spotify artist, album, playlist, and track links are unavailable on this "
            "server right now because Spotify metadata is not initialized."
        )

    if "Spotify rate limit reached" in message or "returned 429" in message:
        return (
            "Spotify links are rate-limited on this server right now. Add your own "
            "SPOTDL_CLIENT_ID and SPOTDL_CLIENT_SECRET in Render, or try again later."
        )

    lowered_message = message.casefold()
    if "confirm you" in lowered_message and "not a bot" in lowered_message:
        return (
            "YouTube rejected this hosted download session before media extraction "
            "completed. SongZip now retries alternate source URLs automatically, but "
            "cloud-hosted YouTube requests can still fail even with cookies because "
            "YouTube may bind requests to the browser session IP."
        )

    return message


def _timestamp_now() -> str:
    """
    Get a human-readable timestamp.

    ### Returns
    - ISO formatted timestamp
    """

    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_account_key(value: Optional[str]) -> str:
    """
    Normalize the shared SongZip account key used across devices.

    ### Arguments
    - value: raw account key

    ### Returns
    - normalized account key
    """

    cleaned = ACCOUNT_KEY_PATTERN.sub("-", str(value or "").strip().lower()).strip("-_")
    if not cleaned:
        return ""

    return cleaned[:64]


def _subscription_root_path() -> Path:
    """
    Get the directory where subscription state files live.

    ### Returns
    - subscriptions root path
    """

    root = get_spotdl_path() / "web/subscriptions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _subscription_record_root_path() -> Path:
    """
    Get the directory where PayPal subscription records live.

    ### Returns
    - subscription records root path
    """

    root = get_spotdl_path() / "web/paypal-subscriptions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_subscription_state() -> Dict[str, Any]:
    """
    Build the default subscription state for a new browser account.

    ### Returns
    - subscription state dictionary
    """

    return {
        "tier": "free",
        "downloads_used": 0,
        "downloads_lifetime": 0,
        "membership_source": "free",
        "bonus_credits": 0,
        "subscription_id": None,
        "activated_at": None,
        "paypal_status": None,
        "updated_at": _timestamp_now(),
    }


def _get_subscription_limit_for_state(subscription_state: Optional[Dict[str, Any]]) -> Optional[int]:
    """
    Resolve the active download cap for a subscription payload.

    ### Arguments
    - subscription_state: subscription state payload

    ### Returns
    - numeric cap or None when uncapped in this prototype
    """

    state = subscription_state or {}
    if str(state.get("tier", "free")).strip().lower() == "free":
        return FREE_TIER_DOWNLOAD_LIMIT + max(0, int(state.get("bonus_credits", 0) or 0))

    return None


def _subscription_has_remaining_capacity(
    subscription_state: Optional[Dict[str, Any]],
) -> bool:
    """
    Determine whether a subscription state can still accept more songs.

    ### Arguments
    - subscription_state: subscription state payload

    ### Returns
    - True when the account still has room to download more songs
    """

    state = subscription_state or {}
    if str(state.get("tier", "free")).strip().lower() != "free":
        return True

    limit = _get_subscription_limit_for_state(state)
    if limit is None:
        return True

    used = max(0, int(state.get("downloads_used", 0) or 0))
    return used < limit


def _build_subscription_snapshot(
    account_key: str,
    subscription_state: Optional[Dict[str, Any]],
    pending_upgrade_prompt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a serializable subscription snapshot from stored state.

    ### Arguments
    - account_key: active SongZip account key
    - subscription_state: raw stored subscription state
    - pending_upgrade_prompt: optional upgrade message for the current client

    ### Returns
    - normalized subscription payload for API consumers
    """

    state = dict(subscription_state or _default_subscription_state())
    limit = _get_subscription_limit_for_state(state)
    used = max(0, int(state.get("downloads_used", 0) or 0))
    lifetime_downloads = max(0, int(state.get("downloads_lifetime", 0) or 0))
    remaining = max(0, limit - used) if limit is not None else None
    tier = str(state.get("tier", "free")).strip().lower()
    effective_upgrade_prompt = (
        pending_upgrade_prompt
        if tier == "free" and not _subscription_has_remaining_capacity(state)
        else None
    )

    return {
        "account_key": _normalize_account_key(account_key),
        "tier": tier,
        "downloads_used": used,
        "downloads_lifetime": lifetime_downloads,
        "membership_source": str(state.get("membership_source", "free")).strip().lower(),
        "bonus_credits": max(0, int(state.get("bonus_credits", 0) or 0)),
        "limit": limit,
        "remaining": remaining,
        "subscription_id": state.get("subscription_id"),
        "activated_at": state.get("activated_at"),
        "paypal_status": state.get("paypal_status"),
        "upgrade_required": bool(
            effective_upgrade_prompt and tier == "free"
        )
        or (limit is not None and remaining == 0),
        "upgrade_prompt": effective_upgrade_prompt,
    }


def _subscription_state_path_for_key(account_key: str) -> Path:
    """
    Get the path for an account's subscription state file.

    ### Arguments
    - account_key: normalized SongZip account key

    ### Returns
    - account subscription state path
    """

    return _subscription_root_path() / f"{account_key}.json"


def _apply_paypal_record_to_state(
    account_key: str,
    state: Dict[str, Any],
    record: Dict[str, Any],
) -> tuple[Dict[str, Any], bool, bool]:
    """
    Merge a PayPal subscription record into the local SongZip state.

    ### Arguments
    - account_key: normalized SongZip account key
    - state: current local state
    - record: PayPal subscription record

    ### Returns
    - next state, whether the state changed, whether admin membership ignored the record
    """

    next_state = dict(state or _default_subscription_state())
    tier = str(record.get("tier", "free")).strip().lower()
    if tier not in SUPPORTED_SUBSCRIPTION_TIERS:
        tier = "free"

    status = str(record.get("status", "LOCAL_APPROVED")).strip().upper()
    membership_source = str(next_state.get("membership_source", "free")).strip().lower()
    current_subscription_id = str(next_state.get("subscription_id") or "").strip()
    record_subscription_id = str(record.get("subscription_id") or "").strip()

    if membership_source == "admin" and (
        not current_subscription_id or current_subscription_id != record_subscription_id
    ):
        return next_state, False, True

    next_state["paypal_status"] = status
    next_state["updated_at"] = max(
        str(next_state.get("updated_at") or ""),
        str(record.get("updated_at") or ""),
    ) or _timestamp_now()

    if status in PAYPAL_ACTIVE_STATUSES:
        next_state["tier"] = tier
        next_state["membership_source"] = "paypal" if tier != "free" else "free"
        next_state["subscription_id"] = record.get("subscription_id")
        next_state["activated_at"] = (
            record.get("activated_at")
            or next_state.get("activated_at")
            or _timestamp_now()
        )
    else:
        next_state["tier"] = "free"
        next_state["membership_source"] = "free"
        next_state["subscription_id"] = None
        next_state["activated_at"] = None

    changed = any(
        next_state.get(key) != (state or {}).get(key)
        for key in (
            "tier",
            "membership_source",
            "subscription_id",
            "activated_at",
            "paypal_status",
            "updated_at",
        )
    )
    return next_state, changed, False


def _maybe_refresh_subscription_from_paypal_record(
    account_key: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Refresh local subscription state from the latest stored PayPal record when needed.

    ### Arguments
    - account_key: normalized SongZip account key
    - state: current loaded state

    ### Returns
    - reconciled subscription state
    """

    record = songzip_store.load_latest_paypal_subscription_for_account(account_key)
    if record is None:
        return state

    current_subscription_id = str(state.get("subscription_id") or "").strip()
    record_subscription_id = str(record.get("subscription_id") or "").strip()
    record_updated_at = str(record.get("updated_at") or "")
    state_updated_at = str(state.get("updated_at") or "")
    state_tier = str(state.get("tier", "free")).strip().lower()
    record_status = str(record.get("status", "")).strip().upper()

    should_refresh = (
        bool(record_updated_at and record_updated_at > state_updated_at)
        or (
            record_status in PAYPAL_ACTIVE_STATUSES
            and state_tier == "free"
        )
        or (
            record_status in PAYPAL_TERMINAL_STATUSES
            and str(state.get("membership_source", "free")).strip().lower() == "paypal"
            and state_tier != "free"
        )
        or (
            bool(record_subscription_id)
            and record_subscription_id != current_subscription_id
            and record_status in PAYPAL_ACTIVE_STATUSES
        )
    )
    if not should_refresh:
        return state

    next_state, changed, ignored = _apply_paypal_record_to_state(account_key, state, record)
    if ignored:
        return state

    if changed:
        songzip_store.save_subscription(account_key, next_state)
        return songzip_store.load_subscription(account_key)

    return next_state


def _load_subscription_state_for_key(account_key: str) -> Dict[str, Any]:
    """
    Load persisted subscription state for a SongZip account key.

    ### Arguments
    - account_key: normalized SongZip account key

    ### Returns
    - subscription state dictionary
    """

    normalized_key = _normalize_account_key(account_key)
    state = songzip_store.load_subscription(normalized_key)
    if not normalized_key:
        return state

    return _maybe_refresh_subscription_from_paypal_record(normalized_key, state)


def _save_subscription_state_for_key(account_key: str, state: Dict[str, Any]):
    """
    Persist subscription state for a SongZip account key.

    ### Arguments
    - account_key: normalized SongZip account key
    - state: subscription state to persist
    """

    state["updated_at"] = _timestamp_now()
    songzip_store.save_subscription(account_key, state)


def _subscription_state_has_usage_or_payment(state: Optional[Dict[str, Any]]) -> bool:
    """
    Determine whether a subscription state contains meaningful usage or payment data.

    ### Arguments
    - state: subscription state payload

    ### Returns
    - whether the state should be preserved during account migration
    """

    if not isinstance(state, dict):
        return False

    if str(state.get("tier", "free")).strip().lower() != "free":
        return True

    if int(state.get("downloads_used", 0) or 0) > 0:
        return True

    if int(state.get("downloads_lifetime", 0) or 0) > 0:
        return True

    if int(state.get("bonus_credits", 0) or 0) > 0:
        return True

    if str(state.get("membership_source", "free")).strip().lower() != "free":
        return True

    return bool(state.get("subscription_id") or state.get("paypal_status"))


def _paypal_subscription_record_path(subscription_id: str) -> Path:
    """
    Get the path for a PayPal subscription record file.

    ### Arguments
    - subscription_id: PayPal subscription id

    ### Returns
    - record path
    """

    safe_id = ACCOUNT_KEY_PATTERN.sub("-", str(subscription_id or "").strip()).strip("-_")
    return _subscription_record_root_path() / f"{safe_id}.json"


def _load_paypal_subscription_record(subscription_id: str) -> Optional[Dict[str, Any]]:
    """
    Load a PayPal subscription record from disk.

    ### Arguments
    - subscription_id: PayPal subscription id

    ### Returns
    - record if present
    """

    if not subscription_id:
        return None

    return songzip_store.load_paypal_subscription(subscription_id)


def _save_paypal_subscription_record(subscription_id: str, record: Dict[str, Any]):
    """
    Persist a PayPal subscription record to disk.

    ### Arguments
    - subscription_id: PayPal subscription id
    - record: record payload
    """

    if not subscription_id:
        return

    record["updated_at"] = _timestamp_now()
    songzip_store.save_paypal_subscription(subscription_id, record)


def _migrate_subscription_state(source_key: str, target_key: str) -> Dict[str, Any]:
    """
    Move meaningful subscription state from one SongZip account key to another.

    ### Arguments
    - source_key: currently active guest/shared key
    - target_key: authenticated account key

    ### Returns
    - resulting target subscription state
    """

    normalized_source = _normalize_account_key(source_key)
    normalized_target = _normalize_account_key(target_key)

    if not normalized_source or not normalized_target or normalized_source == normalized_target:
        return _load_subscription_state_for_key(normalized_target or normalized_source)

    try:
        songzip_store.migrate_account_settings(normalized_source, normalized_target)
    except (OSError, SongZipStoreError):
        app_state.logger.debug(
            "Could not migrate account settings from %s to %s",
            normalized_source,
            normalized_target,
        )

    source_state = _load_subscription_state_for_key(normalized_source)
    target_state = _load_subscription_state_for_key(normalized_target)

    if not _subscription_state_has_usage_or_payment(source_state):
        return target_state

    merged_state = dict(target_state)
    merged_state["downloads_used"] = max(
        int(target_state.get("downloads_used", 0) or 0),
        int(source_state.get("downloads_used", 0) or 0),
    )
    merged_state["downloads_lifetime"] = int(
        target_state.get("downloads_lifetime", 0) or 0
    ) + int(source_state.get("downloads_lifetime", 0) or 0)
    merged_state["bonus_credits"] = int(target_state.get("bonus_credits", 0) or 0) + int(
        source_state.get("bonus_credits", 0) or 0
    )

    source_tier = str(source_state.get("tier", "free")).strip().lower()
    target_tier = str(target_state.get("tier", "free")).strip().lower()
    source_priority = SUBSCRIPTION_TIER_PRIORITY.get(source_tier, 0)
    target_priority = SUBSCRIPTION_TIER_PRIORITY.get(target_tier, 0)

    if source_priority >= target_priority:
        merged_state["tier"] = source_tier
        merged_state["subscription_id"] = source_state.get("subscription_id")
        merged_state["activated_at"] = (
            source_state.get("activated_at")
            or target_state.get("activated_at")
        )
        merged_state["paypal_status"] = (
            source_state.get("paypal_status")
            or target_state.get("paypal_status")
        )
        merged_state["membership_source"] = (
            str(source_state.get("membership_source", "free")).strip().lower()
        )
    else:
        merged_state["membership_source"] = (
            str(target_state.get("membership_source", "free")).strip().lower()
        )

    _save_subscription_state_for_key(normalized_target, merged_state)
    try:
        songzip_store.record_subscription_usage_event(
            normalized_target,
            "subscription_migrated",
            details={
                "source_account_key": normalized_source,
                "target_account_key": normalized_target,
            },
        )
    except (OSError, SongZipStoreError):
        app_state.logger.debug(
            "Could not save subscription migration event for %s",
            normalized_target,
        )

    subscription_id = source_state.get("subscription_id")
    if subscription_id:
        record = _load_paypal_subscription_record(subscription_id) or {
            "subscription_id": subscription_id,
        }
        record["account_key"] = normalized_target
        record["tier"] = merged_state.get("tier", "free")
        record["status"] = record.get("status") or merged_state.get("paypal_status") or "LOCAL_APPROVED"
        record["activated_at"] = (
            record.get("activated_at")
            or merged_state.get("activated_at")
            or _timestamp_now()
        )
        _save_paypal_subscription_record(subscription_id, record)
        _sync_subscription_state_from_record(record)

    if normalized_source != normalized_target:
        reset_state = _default_subscription_state()
        reset_state["force_membership_reset"] = True
        _save_subscription_state_for_key(normalized_source, reset_state)

    return _load_subscription_state_for_key(normalized_target)


def _sync_subscription_state_from_record(record: Dict[str, Any]):
    """
    Apply a PayPal subscription record to its linked SongZip account key.

    ### Arguments
    - record: PayPal subscription record
    """

    account_key = _normalize_account_key(record.get("account_key"))
    if not account_key:
        return

    state = _load_subscription_state_for_key(account_key)
    next_state, changed, ignored = _apply_paypal_record_to_state(account_key, state, record)
    status = str(record.get("status", "LOCAL_APPROVED")).strip().upper()

    if ignored:
        try:
            songzip_store.record_subscription_usage_event(
                account_key,
                "paypal_subscription_sync_ignored",
                tier=state.get("tier"),
                subscription_id=record.get("subscription_id"),
                details={
                    "status": status,
                    "plan_id": record.get("plan_id"),
                    "reason": "admin_membership_override",
                },
            )
        except (OSError, SongZipStoreError):
            app_state.logger.debug(
                "Could not save ignored PayPal sync event for %s",
                account_key,
            )
        return

    if changed:
        _save_subscription_state_for_key(account_key, next_state)

    try:
        songzip_store.record_subscription_usage_event(
            account_key,
            "paypal_subscription_sync",
            tier=next_state.get("tier"),
            subscription_id=record.get("subscription_id"),
            details={
                "status": status,
                "plan_id": record.get("plan_id"),
            },
        )
    except (OSError, SongZipStoreError):
        app_state.logger.debug(
            "Could not save PayPal sync event for %s",
            account_key,
        )


def _paypal_api_base() -> str:
    """
    Get the PayPal API base URL.

    ### Returns
    - PayPal API base URL
    """

    return os.environ.get("SONGZIP_PAYPAL_API_BASE", "https://api-m.paypal.com").rstrip("/")


def _paypal_credentials() -> tuple[str, str, str]:
    """
    Load PayPal credentials needed for webhook verification.

    ### Returns
    - client id, client secret, webhook id
    """

    client_id = os.environ.get("SONGZIP_PAYPAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SONGZIP_PAYPAL_CLIENT_SECRET", "").strip()
    webhook_id = os.environ.get("SONGZIP_PAYPAL_WEBHOOK_ID", "").strip()
    return client_id, client_secret, webhook_id


def _paypal_fetch_access_token() -> str:
    """
    Fetch an OAuth access token from PayPal.

    ### Returns
    - access token
    """

    client_id, client_secret, _ = _paypal_credentials()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail="PayPal webhook verification is not configured yet.",
        )

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{_paypal_api_base()}/v1/oauth2/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail="Could not fetch a PayPal access token for webhook verification.",
        ) from error

    token = str(payload.get("access_token", "")).strip()
    if not token:
        raise HTTPException(
            status_code=502,
            detail="PayPal did not return an access token for webhook verification.",
        )

    return token


def _verify_paypal_webhook_signature(
    headers: Dict[str, str],
    webhook_event: Dict[str, Any],
) -> bool:
    """
    Verify a PayPal webhook payload against PayPal's verification endpoint.

    ### Arguments
    - headers: incoming webhook headers
    - webhook_event: raw webhook event body

    ### Returns
    - whether PayPal marked the signature as valid
    """

    _, _, webhook_id = _paypal_credentials()
    if not webhook_id:
        raise HTTPException(
            status_code=503,
            detail="PayPal webhook verification is not configured yet.",
        )

    verification_payload = {
        "auth_algo": headers.get("PAYPAL-AUTH-ALGO"),
        "cert_url": headers.get("PAYPAL-CERT-URL"),
        "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
        "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
        "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
        "webhook_id": webhook_id,
        "webhook_event": webhook_event,
    }

    missing = [key for key, value in verification_payload.items() if value in (None, "")]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"PayPal webhook headers are incomplete: {', '.join(missing)}.",
        )

    request = urllib.request.Request(
        f"{_paypal_api_base()}/v1/notifications/verify-webhook-signature",
        data=json.dumps(verification_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_paypal_fetch_access_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail="Could not verify the PayPal webhook signature.",
        ) from error

    return str(payload.get("verification_status", "")).upper() == "SUCCESS"


def _tier_from_paypal_plan_id(plan_id: Optional[str]) -> Optional[str]:
    """
    Resolve a PayPal plan id to a SongZip tier.

    ### Arguments
    - plan_id: PayPal billing plan id

    ### Returns
    - matching SongZip tier if known
    """

    if not plan_id:
        return None

    return PAYPAL_PLAN_TO_TIER.get(str(plan_id).strip())


def _process_paypal_webhook_event(webhook_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Apply an incoming PayPal webhook event to local subscription state.

    ### Arguments
    - webhook_event: raw verified PayPal event

    ### Returns
    - updated PayPal subscription record when one could be resolved
    """

    event_type = str(webhook_event.get("event_type", "")).strip()
    if event_type not in PAYPAL_SUBSCRIPTION_STATUS_MAP:
        return None

    resource = webhook_event.get("resource") or {}
    subscription_id = str(resource.get("id") or resource.get("billing_agreement_id") or "").strip()
    if not subscription_id:
        return None

    record = _load_paypal_subscription_record(subscription_id) or {
        "subscription_id": subscription_id,
    }

    account_key = _normalize_account_key(
        resource.get("custom_id") or record.get("account_key")
    )
    tier = (
        str(record.get("tier", "")).strip().lower()
        or _tier_from_paypal_plan_id(resource.get("plan_id"))
        or "free"
    )

    record.update(
        {
            "subscription_id": subscription_id,
            "account_key": account_key,
            "tier": tier,
            "status": PAYPAL_SUBSCRIPTION_STATUS_MAP[event_type],
            "event_type": event_type,
            "plan_id": resource.get("plan_id") or record.get("plan_id"),
            "activated_at": record.get("activated_at") or _timestamp_now(),
            "last_event": webhook_event,
        }
    )
    _save_paypal_subscription_record(subscription_id, record)
    _sync_subscription_state_from_record(record)
    return record


def _set_songzip_session_cookie(response: Response, session_token: str):
    """
    Set the SongZip session cookie on a response.

    ### Arguments
    - response: outgoing response
    - session_token: raw session token
    """

    secure_cookie = bool(
        os.environ.get("SONGZIP_COOKIE_SECURE")
        or os.environ.get("RENDER")
        or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    )

    response.set_cookie(
        SONGZIP_SESSION_COOKIE,
        session_token,
        httponly=True,
        samesite="lax",
        secure=secure_cookie,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


def _clear_songzip_session_cookie(response: Response):
    """
    Clear the SongZip session cookie.

    ### Arguments
    - response: outgoing response
    """

    response.delete_cookie(SONGZIP_SESSION_COOKIE, path="/")


def _normalize_email_address(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _configured_admin_email() -> str:
    return _normalize_email_address(os.environ.get("SONGZIP_ADMIN_EMAIL"))


def _decorate_account(account: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if account is None:
        return None

    decorated = dict(account)
    admin_email = _configured_admin_email()
    decorated["is_admin"] = bool(
        admin_email
        and _normalize_email_address(account.get("email")) == admin_email
    )
    return decorated


def _resolve_authenticated_account(request: Optional[Request]) -> Optional[Dict[str, Any]]:
    """
    Resolve the authenticated SongZip account from the current request cookie.

    ### Arguments
    - request: active request if available

    ### Returns
    - public account payload if authenticated
    """

    if request is None:
        return None

    session_token = request.cookies.get(SONGZIP_SESSION_COOKIE)
    return _decorate_account(songzip_store.get_account_by_session(session_token))


def _claim_google_admin_if_needed(account: Dict[str, Any]) -> Dict[str, Any]:
    return _decorate_account(account) or account


def _assert_admin_account(request: Request) -> Dict[str, Any]:
    account = _resolve_authenticated_account(request)
    if account is None:
        raise HTTPException(status_code=401, detail="Sign in first.")

    if not account.get("is_admin"):
        raise HTTPException(
            status_code=403,
            detail="Only the SongZip admin account can manage SongZip credits and memberships.",
        )

    return account


def _prune_google_account_states():
    now = datetime.datetime.now(datetime.timezone.utc)
    expired: List[str] = []
    for state_token, payload in _pending_google_account_states.items():
        created_at = payload.get("created_at")
        try:
            created = datetime.datetime.fromisoformat(str(created_at))
        except (TypeError, ValueError):
            created = None

        if created is None or (now - created).total_seconds() > GOOGLE_ACCOUNT_STATE_TTL_SECONDS:
            expired.append(state_token)

    for state_token in expired:
        _pending_google_account_states.pop(state_token, None)


def _build_google_login_redirect_uri(request: Request) -> str:
    configured = str(os.environ.get("SONGZIP_GOOGLE_LOGIN_REDIRECT_URI", "")).strip()
    if configured:
        return configured

    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/api/account/google/callback"


def _google_login_client_config(request: Request) -> Dict[str, str]:
    client_id = str(
        os.environ.get("SONGZIP_GOOGLE_LOGIN_CLIENT_ID")
        or os.environ.get("SPOTDL_GOOGLE_OAUTH_CLIENT_ID")
        or ""
    ).strip()
    client_secret = str(
        os.environ.get("SONGZIP_GOOGLE_LOGIN_CLIENT_SECRET")
        or os.environ.get("SPOTDL_GOOGLE_OAUTH_CLIENT_SECRET")
        or ""
    ).strip()
    redirect_uri = _build_google_login_redirect_uri(request)

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google sign-in is not configured yet. Add "
                "SONGZIP_GOOGLE_LOGIN_CLIENT_ID and SONGZIP_GOOGLE_LOGIN_CLIENT_SECRET "
                "(or the matching SPOTDL_GOOGLE_OAUTH_* values) first."
            ),
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


def _build_google_login_redirect_back(
    request: Request,
    status: str,
    message: str,
) -> str:
    base_url = str(request.base_url).rstrip("/")
    return (
        f"{base_url}/?account_auth_status={urllib.parse.quote(status)}"
        f"&account_auth_message={urllib.parse.quote(message)}"
    )


def _exchange_google_login_code(
    client_config: Dict[str, str],
    code: str,
) -> Dict[str, Any]:
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode(
            {
                "code": code,
                "client_id": client_config["client_id"],
                "client_secret": client_config["client_secret"],
                "redirect_uri": client_config["redirect_uri"],
                "grant_type": "authorization_code",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail="Google sign-in could not exchange the authorization code.",
        ) from error

    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="Google sign-in did not return an access token.",
        )

    return payload


def _fetch_google_login_profile(access_token: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail="Google sign-in could not load your account profile.",
        ) from error

    if not payload.get("email") or not payload.get("sub"):
        raise HTTPException(
            status_code=502,
            detail="Google sign-in did not return the required account profile fields.",
        )

    if payload.get("email_verified") is False:
        raise HTTPException(
            status_code=400,
            detail="Google sign-in requires a verified email address.",
        )

    return payload

ALLOWED_ORIGINS = [
    "http://localhost:8800",
    "http://127.0.0.1:8800",
    "https://localhost:8800",
    "https://127.0.0.1:8800",
]

DEFAULT_WEB_OUTPUT_TEMPLATE = "{album-artist}/{album}/{title}.{output-ext}"
LEGACY_WEB_OUTPUT_TEMPLATE = "{artists} - {title}.{output-ext}"


def _normalize_web_audio_providers(providers: Optional[List[str]]) -> List[str]:
    """
    Expand the primary audio provider into a small fallback chain for the web UI.

    ### Arguments
    - providers: current provider list

    ### Returns
    - normalized provider list
    """

    normalized = [
        provider.strip()
        for provider in providers or []
        if isinstance(provider, str) and provider.strip()
    ]
    if len(normalized) == 0:
        normalized = ["youtube-music"]

    fallback_map = {
        "youtube-music": ["youtube-music", "youtube"],
        "youtube": ["youtube", "youtube-music"],
        "soundcloud": ["soundcloud", "youtube-music", "youtube"],
        "bandcamp": ["bandcamp", "youtube-music", "youtube"],
        "piped": ["piped", "youtube", "youtube-music"],
    }

    expanded: List[str] = []
    for provider in normalized:
        for candidate in fallback_map.get(provider, [provider]):
            if candidate not in expanded:
                expanded.append(candidate)

    return expanded


def _normalize_web_output_template(output: Optional[str]) -> str:
    """
    Normalize the output template used by the web dashboard.

    ### Arguments
    - output: requested output template

    ### Returns
    - normalized output template
    """

    if output is None:
        return DEFAULT_WEB_OUTPUT_TEMPLATE

    output = output.strip()
    if output == "" or output == LEGACY_WEB_OUTPUT_TEMPLATE:
        return DEFAULT_WEB_OUTPUT_TEMPLATE

    return output


def _normalize_web_downloader_settings(settings: Dict[str, Any]) -> DownloaderOptions:
    """
    Apply web-dashboard-specific defaults and migrations to downloader settings.

    ### Arguments
    - settings: raw downloader settings

    ### Returns
    - normalized downloader settings
    """

    settings_cpy = dict(settings)
    settings_cpy["audio_providers"] = _normalize_web_audio_providers(
        settings_cpy.get("audio_providers")
    )
    settings_cpy["output"] = _normalize_web_output_template(
        settings_cpy.get("output")
    )

    return DownloaderOptions(**settings_cpy)  # type: ignore[arg-type]


def _default_web_downloader_settings() -> DownloaderOptions:
    return _normalize_web_downloader_settings(
        create_settings_type(
            Namespace(config=False),
            dict(app_state.downloader_settings),
            DOWNLOADER_OPTIONS,
        )  # type: ignore[arg-type]
    )


def _load_account_downloader_settings(account_key: str) -> DownloaderOptions:
    normalized_key = _normalize_account_key(account_key)
    settings_cpy = dict(_default_web_downloader_settings())
    persisted_settings = songzip_store.load_account_settings(normalized_key)
    if persisted_settings:
        settings_cpy.update({k: v for k, v in persisted_settings.items() if v is not None})

    forced_format = app_state.web_settings.get("forced_format")
    if forced_format:
        settings_cpy["format"] = forced_format

    forced_output = app_state.web_settings.get("forced_output")
    if forced_output:
        settings_cpy["output"] = forced_output

    normalized_settings = _normalize_web_downloader_settings(settings_cpy)
    normalized_settings["cookie_file"] = (
        _stored_cookie_file_for_account(normalized_key)
        or normalized_settings.get("cookie_file")
        or ""
    )
    return normalized_settings


def _is_path_within_root(file_path: Path, root_path: Path) -> bool:
    """
    Check whether a file path is inside a root directory.

    ### Arguments
    - file_path: candidate file path
    - root_path: allowed root directory

    ### Returns
    - whether the file path is inside the root
    """

    try:
        file_path.resolve().relative_to(root_path.resolve())
        return True
    except ValueError:
        return False


def _cookie_meta_key(account_key: str) -> str:
    return f"{COOKIE_META_PREFIX}:{_normalize_account_key(account_key)}"


def _youtube_cookie_root_path() -> Path:
    root = get_spotdl_path() / "web" / "youtube-cookies"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _looks_like_cookie_file_contents(value: str) -> bool:
    text = str(value or "")
    stripped = text.strip()
    if not stripped:
        return False

    lowered = stripped.splitlines()[0].strip().lower()
    if lowered in COOKIE_FILE_HEADERS:
        return True

    return "\n" in text and "\t" in text


def _store_cookie_contents_for_account(account_key: str, cookie_text: str) -> str:
    normalized_key = _normalize_account_key(account_key)
    if not normalized_key:
        raise HTTPException(status_code=400, detail="SongZip account key is required.")

    stripped = cookie_text.strip()
    first_line = stripped.splitlines()[0].strip().lower() if stripped else ""
    if first_line not in COOKIE_FILE_HEADERS:
        raise HTTPException(
            status_code=400,
            detail=(
                "YouTube cookies must be pasted in Netscape cookies.txt format. "
                "The first line should be # Netscape HTTP Cookie File."
            ),
        )

    cookie_path = _youtube_cookie_root_path() / f"{normalized_key}.txt"
    cookie_path.write_text(stripped + "\n", encoding="utf-8")
    songzip_store.set_meta(_cookie_meta_key(normalized_key), str(cookie_path))
    return str(cookie_path)


def _clear_stored_cookie_file(account_key: str) -> None:
    normalized_key = _normalize_account_key(account_key)
    if not normalized_key:
        return

    existing = songzip_store.get_meta(_cookie_meta_key(normalized_key))
    if existing:
        try:
            Path(existing).unlink(missing_ok=True)
        except OSError:
            logger = getattr(app_state, "logger", None)
            if logger is not None:
                logger.debug(
                    "Could not remove stored YouTube cookies for %s",
                    normalized_key,
                )
    songzip_store.set_meta(_cookie_meta_key(normalized_key), "")


def _stored_cookie_file_for_account(account_key: str) -> str:
    normalized_key = _normalize_account_key(account_key)
    if not normalized_key:
        return ""

    stored = str(songzip_store.get_meta(_cookie_meta_key(normalized_key)) or "").strip()
    if not stored:
        return ""

    stored_path = Path(stored)
    if stored_path.is_file():
        return str(stored_path)

    return ""


def _resolve_cookie_file_setting(account_key: str, raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if not value:
        _clear_stored_cookie_file(account_key)
        return ""

    if _looks_like_cookie_file_contents(value):
        return _store_cookie_contents_for_account(account_key, value)

    candidate = Path(value).expanduser()
    if candidate.is_file():
        resolved = str(candidate.resolve())
        songzip_store.set_meta(_cookie_meta_key(account_key), resolved)
        return resolved

    raise HTTPException(
        status_code=400,
        detail=(
            "Hosted SongZip cannot read cookie paths from your own device. "
            "Paste the exported Netscape cookies.txt contents into the cookie field instead."
        ),
    )


class SPAStaticFiles(StaticFiles):
    """
    Override the static files to serve the index.html and other assets.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """
        Serve static files from the SPA.

        ### Arguments
        - path: The path to the file.
        - scope: The scope of the request.

        ### Returns
        - returns the response.
        """

        response = await super().get_response(path, scope)
        if response.status_code == 404:
            response = await super().get_response(".", scope)

        response.headers.setdefault(
            "Cache-Control", "no-store, no-cache, max-age=0, must-revalidate"
        )
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("Expires", "0")

        return response


class Client:
    """
    Holds the client's state.
    """

    def __init__(
        self,
        websocket: Optional[WebSocket],
        client_id: str,
        account_key: Optional[str] = None,
        authenticated_account: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the WebSocket handler.
        ### Arguments
        - websocket: The WebSocket instance.
        - client_id: The client's ID.
        - downloader_settings: The downloader settings.
        """

        self.websocket: Optional[WebSocket] = websocket
        self.client_id = client_id
        self.authenticated_account = authenticated_account
        self.account_key = _normalize_account_key(account_key) or _normalize_account_key(
            client_id
        )
        self.downloader_settings = _load_account_downloader_settings(self.account_key)
        self.downloader = self._create_downloader(self.downloader_settings)
        self.download_task: Optional[asyncio.Task] = None
        self.events: List[Dict[str, Any]] = []
        self.song_states: Dict[str, Dict[str, Any]] = {}
        self.completed_downloads: List[Dict[str, Any]] = []
        self.download_bundle: Optional[Dict[str, Any]] = None
        self.latest_update: Optional[Dict[str, Any]] = None
        self.pending_upgrade_prompt: Optional[Dict[str, Any]] = None
        self.subscription = self._load_subscription_state()
        self.current_job: Dict[str, Any] = {
            "status": "idle",
            "query": None,
            "message": "Ready",
            "started_at": None,
            "finished_at": None,
            "resolved_count": 0,
            "error": None,
            "output_root": self.get_output_root(),
        }
        self._restore_persisted_snapshot()

    def _create_downloader(self, settings: DownloaderOptions) -> Downloader:
        downloader = Downloader(settings=settings, loop=app_state.loop)
        downloader.progress_handler.web_ui = True
        return downloader

    def _reload_account_downloader_settings(self) -> None:
        self.downloader_settings = _load_account_downloader_settings(self.account_key)
        self.downloader = self._create_downloader(self.downloader_settings)
        if hasattr(self, "current_job") and isinstance(self.current_job, dict):
            self.current_job["output_root"] = self.get_output_root()

    def set_account_key(self, account_key: Optional[str]):
        """
        Switch the shared SongZip account key for this browser client.

        ### Arguments
        - account_key: requested account key
        """

        normalized = _normalize_account_key(account_key) or _normalize_account_key(
            self.client_id
        )
        if normalized == self.account_key:
            return

        self.account_key = normalized
        self.pending_upgrade_prompt = None
        self._reload_account_downloader_settings()
        self.subscription = self._load_subscription_state()
        self._reconcile_upgrade_prompt()
        self._persist_client_snapshot()

    def set_authenticated_account(self, account: Optional[Dict[str, Any]]):
        """
        Update the authenticated SongZip account bound to this client.

        ### Arguments
        - account: authenticated account payload, if any
        """

        self.authenticated_account = account
        if account and account.get("account_key"):
            self.set_account_key(str(account["account_key"]))
        else:
            self._persist_client_snapshot()

    def attach_websocket(self, websocket: WebSocket):
        """
        Attach a websocket connection to an existing client session.

        ### Arguments
        - websocket: the websocket to attach
        """

        self.websocket = websocket

    def detach_websocket(self, websocket: WebSocket):
        """
        Detach the websocket if it is still the active one.

        ### Arguments
        - websocket: the websocket to detach
        """

        if self.websocket is websocket:
            self.websocket = None

    @staticmethod
    def _timestamp() -> str:
        """
        Get a human-readable timestamp.

        ### Returns
        - ISO formatted timestamp
        """

        return _timestamp_now()

    @staticmethod
    def _song_key(song: Song) -> str:
        """
        Get a stable key for a song.

        ### Arguments
        - song: the song to identify

        ### Returns
        - stable key
        """

        if song.url:
            return song.url

        if song.song_id:
            return song.song_id

        return song.display_name

    @staticmethod
    def _normalize_status(message: str) -> str:
        """
        Normalize a progress message to a compact status string.

        ### Arguments
        - message: the human-readable message

        ### Returns
        - normalized status
        """

        mapping = {
            "processing": "queued",
            "downloading": "downloading",
            "converting": "converting",
            "embedding metadata": "embedding",
            "done": "done",
            "error": "error",
            "skipped": "skipped",
        }
        return mapping.get(message.strip().lower(), message.strip().lower())

    def get_output_root(self) -> str:
        """
        Get the output root for the current client.

        ### Returns
        - output directory root
        """

        if app_state.web_settings.get("web_use_output_dir", False):
            return str(
                Path(self.downloader_settings["output"].split("{", 1)[0]).absolute()
            )

        return str((get_spotdl_path() / f"web/sessions/{self.client_id}").absolute())

    def get_download_output(self) -> str:
        """
        Get the downloader output value for the current client.

        ### Returns
        - output template/path passed to the downloader
        """

        if app_state.web_settings.get("web_use_output_dir", False):
            return self.downloader_settings["output"]

        return str(
            (Path(self.get_output_root()) / self.downloader_settings["output"]).absolute()
        )

    def _get_subscription_state_path(self) -> Path:
        """
        Get the on-disk path for this session's subscription state.

        ### Returns
        - subscription state path
        """

        return _subscription_state_path_for_key(self.account_key)

    def _default_subscription_state(self) -> Dict[str, Any]:
        """
        Build the default subscription state for a new browser session.

        ### Returns
        - subscription state dictionary
        """

        return _default_subscription_state()

    def _load_subscription_state(self) -> Dict[str, Any]:
        """
        Load persisted subscription state for this browser session.

        ### Returns
        - subscription state dictionary
        """

        return _load_subscription_state_for_key(self.account_key)

    def _reconcile_upgrade_prompt(self):
        """
        Clear any stale upgrade prompt after account access changes.
        """

        if _subscription_has_remaining_capacity(self.subscription):
            self.pending_upgrade_prompt = None

    def _save_subscription_state(self):
        """
        Persist the browser session's subscription state.
        """

        try:
            _save_subscription_state_for_key(self.account_key, self.subscription)
        except OSError:
            app_state.logger.debug(
                "Could not save subscription state for %s",
                self.account_key,
            )

    def _serialize_client_snapshot(self) -> Dict[str, Any]:
        return {
            "job": dict(self.current_job),
            "song_states": list(self.song_states.values()),
            "downloads": list(self.completed_downloads[-50:]),
            "bundle": dict(self.download_bundle) if isinstance(self.download_bundle, dict) else None,
            "events": list(self.events[-120:]),
            "latest_update": dict(self.latest_update) if isinstance(self.latest_update, dict) else self.latest_update,
            "pending_upgrade_prompt": (
                dict(self.pending_upgrade_prompt)
                if isinstance(self.pending_upgrade_prompt, dict)
                else None
            ),
        }

    def _persist_client_snapshot(self) -> None:
        try:
            songzip_store.save_client_snapshot(
                self.client_id,
                self.account_key,
                self._serialize_client_snapshot(),
            )
        except (OSError, SongZipStoreError):
            app_state.logger.debug(
                "Could not persist dashboard snapshot for %s",
                self.client_id,
            )

    def _restore_persisted_snapshot(self) -> None:
        try:
            snapshot = songzip_store.load_client_snapshot(self.client_id)
        except (OSError, SongZipStoreError):
            snapshot = None

        if not isinstance(snapshot, dict):
            return

        snapshot_key = _normalize_account_key(snapshot.get("account_key"))
        if snapshot_key and snapshot_key != self.account_key:
            return

        raw_job = snapshot.get("job")
        if isinstance(raw_job, dict):
            self.current_job.update(raw_job)

        self.song_states = {}
        for item in snapshot.get("song_states") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            self.song_states[key] = dict(item)

        self.completed_downloads = [
            dict(item)
            for item in (snapshot.get("downloads") or [])
            if isinstance(item, dict)
        ]
        raw_bundle = snapshot.get("bundle")
        self.download_bundle = dict(raw_bundle) if isinstance(raw_bundle, dict) else None
        self.events = [
            dict(item)
            for item in (snapshot.get("events") or [])
            if isinstance(item, dict)
        ]
        raw_latest = snapshot.get("latest_update")
        self.latest_update = dict(raw_latest) if isinstance(raw_latest, dict) else None
        raw_prompt = snapshot.get("pending_upgrade_prompt")
        self.pending_upgrade_prompt = dict(raw_prompt) if isinstance(raw_prompt, dict) else None

        if str(self.current_job.get("status", "idle")).strip().lower() in {"starting", "running"}:
            self.current_job.update(
                {
                    "status": "interrupted",
                    "message": "Needs retry",
                    "finished_at": self._timestamp(),
                    "error": (
                        "SongZip restarted before the previous job could finish. "
                        "Retry the last query to continue."
                    ),
                    "output_root": self.get_output_root(),
                }
            )
            self.events.append(
                {
                    "timestamp": self._timestamp(),
                    "level": "warning",
                    "kind": "job",
                    "message": "Previous job was interrupted before completion.",
                    "details": {
                        "query": self.current_job.get("query"),
                    },
                }
            )

        self._reconcile_upgrade_prompt()
        self._persist_client_snapshot()

    def _record_subscription_event(
        self,
        event_type: str,
        song_count: int = 0,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Persist a subscription usage event for auditing and recovery.

        ### Arguments
        - event_type: event kind
        - song_count: song delta for the event
        - details: optional event metadata
        """

        try:
            songzip_store.record_subscription_usage_event(
                self.account_key,
                event_type,
                song_count=song_count,
                tier=str(self.subscription.get("tier", "free")).strip().lower(),
                subscription_id=self.subscription.get("subscription_id"),
                details=details,
            )
        except (OSError, SongZipStoreError):
            app_state.logger.debug(
                "Could not save subscription usage event %s for %s",
                event_type,
                self.account_key,
            )

    def _get_subscription_limit(self) -> Optional[int]:
        """
        Get the download cap for the active tier.

        ### Returns
        - numeric cap or None when uncapped in this prototype
        """

        return _get_subscription_limit_for_state(self.subscription)

    def get_subscription_snapshot(self) -> Dict[str, Any]:
        """
        Build a serializable snapshot of the active subscription state.

        ### Returns
        - subscription state for API consumers
        """

        self._reconcile_upgrade_prompt()
        return _build_subscription_snapshot(
            self.account_key,
            self.subscription,
            pending_upgrade_prompt=self.pending_upgrade_prompt,
        )

    def activate_subscription_tier(
        self,
        tier: str,
        subscription_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Activate a subscription tier for this browser session.

        ### Arguments
        - tier: target tier
        - subscription_id: optional payment provider subscription id

        ### Returns
        - updated subscription snapshot
        """

        normalized_tier = str(tier or "").strip().lower()
        if normalized_tier not in SUPPORTED_SUBSCRIPTION_TIERS:
            raise HTTPException(status_code=400, detail="Unsupported subscription tier.")

        self.subscription["tier"] = normalized_tier
        self.subscription["membership_source"] = (
            "paypal" if normalized_tier != "free" else "free"
        )
        self.subscription["subscription_id"] = subscription_id
        self.subscription["activated_at"] = self._timestamp()
        self.subscription["paypal_status"] = (
            "LOCAL_APPROVED" if normalized_tier != "free" else None
        )
        self.pending_upgrade_prompt = None
        self._save_subscription_state()
        self._record_subscription_event(
            "subscription_activated",
            details={
                "tier": normalized_tier,
                "paypal_status": self.subscription["paypal_status"],
            },
        )
        if subscription_id:
            record = _load_paypal_subscription_record(subscription_id) or {}
            record.update(
                {
                    "subscription_id": subscription_id,
                    "account_key": self.account_key,
                    "tier": normalized_tier,
                    "status": "LOCAL_APPROVED" if normalized_tier != "free" else "FREE",
                    "plan_id": record.get("plan_id"),
                    "activated_at": self.subscription["activated_at"],
                }
            )
            _save_paypal_subscription_record(subscription_id, record)

        self._reconcile_upgrade_prompt()
        return self.get_subscription_snapshot()

    def _reserve_download_capacity(
        self,
        songs: List[Song],
    ) -> tuple[List[Song], int]:
        """
        Reserve any available download capacity for the current tier.

        ### Arguments
        - songs: resolved songs for the current request

        ### Returns
        - allowed songs and overflow count
        """

        self.pending_upgrade_prompt = None
        if self.subscription.get("tier") != "free":
            allowed_count = len(songs)
            if allowed_count > 0:
                self.subscription["downloads_lifetime"] = int(
                    self.subscription.get("downloads_lifetime", 0) or 0
                ) + allowed_count
                self._save_subscription_state()
                self._record_subscription_event(
                    "songs_downloaded",
                    song_count=allowed_count,
                    details={"mode": "paid"},
                )
            return songs, 0

        used = int(self.subscription.get("downloads_used", 0))
        limit = self._get_subscription_limit() or FREE_TIER_DOWNLOAD_LIMIT
        remaining = max(0, limit - used)
        requested = len(songs)

        if remaining <= 0:
            self.pending_upgrade_prompt = {
                "tier": "free",
                "requested": requested,
                "allowed": 0,
                "held": requested,
                "used": used,
                "limit": limit,
                "message": "Free tier limit reached. Upgrade to keep downloading songs.",
            }
            return [], requested

        allowed = songs[:remaining]
        overflow_count = max(0, requested - len(allowed))
        self.subscription["downloads_used"] = used + len(allowed)
        self.subscription["downloads_lifetime"] = int(
            self.subscription.get("downloads_lifetime", 0) or 0
        ) + len(allowed)
        self._save_subscription_state()
        if allowed:
            self._record_subscription_event(
                "songs_downloaded",
                song_count=len(allowed),
                details={"mode": "free", "overflow_count": overflow_count},
            )

        if overflow_count > 0 or self.subscription["downloads_used"] >= limit:
            self.pending_upgrade_prompt = {
                "tier": "free",
                "requested": requested,
                "allowed": len(allowed),
                "held": overflow_count,
                "used": int(self.subscription["downloads_used"]),
                "limit": limit,
                "message": (
                    "Free tier limit reached. Upgrade to keep downloading more songs."
                    if overflow_count == 0
                    else f"Free tier allows {len(allowed)} more song(s) right now. Upgrade to unlock the remaining {overflow_count}."
                ),
            }

        return allowed, overflow_count

    def get_output_root_path(self) -> Path:
        """
        Get the current output root as a Path object.

        ### Returns
        - output root path
        """

        return Path(self.get_output_root()).absolute()

    def _create_download_record(self, file_path: Path) -> Dict[str, Any]:
        """
        Create a download record from a file on disk.

        ### Arguments
        - file_path: downloaded file path

        ### Returns
        - serializable download record
        """

        absolute_path = str(file_path.absolute())
        display_name = file_path.stem
        source_url = None

        for song_state in self.song_states.values():
            if song_state.get("path") == absolute_path:
                display_name = song_state.get("display_name") or display_name
                source_url = (song_state.get("song") or {}).get("url")
                break

        return {
            "display_name": display_name,
            "path": absolute_path,
            "url": source_url,
        }

    def _refresh_completed_downloads_from_output(self):
        """
        Refresh completed download records and zip bundle from files on disk.
        """

        completed_downloads, download_bundle = self._build_download_snapshot()
        self.completed_downloads = completed_downloads
        self.download_bundle = download_bundle

    def _build_download_snapshot(
        self,
    ) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Build the completed download list and bundle metadata from output files.

        ### Returns
        - completed downloads and optional bundle metadata
        """

        output_root = self.get_output_root_path()
        output_format = self.downloader_settings["format"]

        completed_downloads = []
        seen_paths = set()
        if output_root.exists():
            for file_path in sorted(output_root.rglob(f"*.{output_format}")):
                absolute_path = str(file_path.absolute())
                if absolute_path in seen_paths:
                    continue

                seen_paths.add(absolute_path)
                completed_downloads.append(self._create_download_record(file_path))

        return completed_downloads, self._create_download_bundle(completed_downloads)

    def _clear_session_output_root(self):
        """
        Clear this browser session's output directory when using isolated web sessions.
        """

        if app_state.web_settings.get("web_use_output_dir", False):
            return

        output_root = Path(self.get_output_root())
        if not output_root.exists():
            return

        try:
            shutil.rmtree(output_root)
        except OSError:
            app_state.logger.debug(
                "Could not remove old session output: %s", output_root
            )

    def _delete_existing_bundle(self):
        """
        Delete the active session bundle if it exists.
        """

        if self.download_bundle is None:
            return

        bundle_path = self.download_bundle.get("path")
        if bundle_path and Path(bundle_path).is_file():
            try:
                Path(bundle_path).unlink()
            except OSError:
                app_state.logger.debug("Could not remove old bundle: %s", bundle_path)

        self.download_bundle = None

    def _create_download_bundle(
        self, completed_downloads: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Create a zip bundle for the finished downloads.

        ### Arguments
        - completed_downloads: current job downloads

        ### Returns
        - bundle metadata, or None if no files are available
        """

        self._delete_existing_bundle()

        if len(completed_downloads) == 0:
            return None

        bundle_root = get_spotdl_path() / "web/bundles" / self.client_id
        bundle_root.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        bundle_name = f"spotdl-downloads-{timestamp}.zip"
        bundle_path = bundle_root / bundle_name
        output_root = Path(self.get_output_root())
        bundled_files = 0
        seen_file_paths = set()
        used_archive_names: Dict[str, int] = {}
        bundle_compression = str(
            app_state.web_settings.get("bundle_compression", "deflate")
        ).strip().lower()
        zip_kwargs: Dict[str, Any] = {"mode": "w"}
        if bundle_compression in {"store", "stored", "none"}:
            zip_kwargs["compression"] = zipfile.ZIP_STORED
        elif bundle_compression in {"deflate-fast", "fast"}:
            zip_kwargs["compression"] = zipfile.ZIP_DEFLATED
            zip_kwargs["compresslevel"] = 1
        else:
            zip_kwargs["compression"] = zipfile.ZIP_DEFLATED

        with zipfile.ZipFile(bundle_path, **zip_kwargs) as archive:
            for download in completed_downloads:
                file_path_str = download.get("path")
                if not file_path_str:
                    continue

                file_path = Path(file_path_str)
                if not file_path.is_file():
                    continue

                normalized_path = str(file_path.resolve())
                if normalized_path in seen_file_paths:
                    continue

                seen_file_paths.add(normalized_path)

                if app_state.web_settings.get("bundle_flatten", False):
                    archive_name = file_path.name
                else:
                    try:
                        archive_name = str(file_path.relative_to(output_root))
                    except ValueError:
                        archive_name = file_path.name

                archive_stem = Path(archive_name).stem
                archive_suffix = Path(archive_name).suffix
                archive_key = archive_name.lower()
                duplicate_index = used_archive_names.get(archive_key, 0)
                if duplicate_index > 0:
                    archive_name = f"{archive_stem} ({duplicate_index + 1}){archive_suffix}"

                used_archive_names[archive_key] = duplicate_index + 1

                archive.write(file_path, arcname=archive_name)
                bundled_files += 1

        if bundled_files == 0:
            try:
                bundle_path.unlink()
            except OSError:
                pass
            return None

        return {
            "name": bundle_name,
            "path": str(bundle_path.absolute()),
            "count": bundled_files,
            "created_at": self._timestamp(),
        }

    def _append_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Append an event to diagnostics history.

        ### Arguments
        - event: event payload

        ### Returns
        - the event
        """

        self.events.append(event)
        self.events = self.events[-200:]
        self.latest_update = event
        return event

    def _ensure_song_state(
        self,
        song: Song,
        queue_position: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Ensure a song has a state entry.

        ### Arguments
        - song: the song to track
        - queue_position: optional queue position

        ### Returns
        - mutable song state dictionary
        """

        key = self._song_key(song)
        song_state = self.song_states.get(key)
        if song_state is None:
            song_state = {
                "key": key,
                "display_name": song.display_name,
                "progress": 0,
                "message": "Queued",
                "status": "queued",
                "queue_position": queue_position or (len(self.song_states) + 1),
                "path": None,
                "updated_at": self._timestamp(),
                "song": song.json,
            }
            self.song_states[key] = song_state
        elif queue_position is not None:
            song_state["queue_position"] = queue_position

        return song_state

    def get_state_snapshot(self) -> Dict[str, Any]:
        """
        Get the current state snapshot for the client.

        ### Returns
        - serializable state snapshot
        """

        song_list = sorted(
            self.song_states.values(),
            key=lambda state: state.get("queue_position", 0),
        )

        completed = len(
            [song for song in song_list if song.get("status") == "done"]
        )
        failed = len([song for song in song_list if song.get("status") == "error"])
        skipped = len([song for song in song_list if song.get("status") == "skipped"])
        queued = len([song for song in song_list if song.get("status") == "queued"])
        active = len(
            [
                song
                for song in song_list
                if song.get("status") in {"downloading", "converting", "embedding"}
            ]
        )
        progress = (
            round(
                sum(song.get("progress", 0) for song in song_list) / len(song_list), 1
            )
            if song_list
            else 0.0
        )

        return {
            "client_id": self.client_id,
            "account": {
                "key": self.account_key,
                "shared": self.account_key != _normalize_account_key(self.client_id),
                "authenticated": self.authenticated_account is not None,
                "email": (
                    self.authenticated_account.get("email")
                    if self.authenticated_account
                    else None
                ),
                "is_admin": bool(
                    self.authenticated_account.get("is_admin")
                    if self.authenticated_account
                    else False
                ),
            },
            "job": self.current_job,
            "subscription": self.get_subscription_snapshot(),
            "stats": {
                "total": len(song_list),
                "resolved": self.current_job.get("resolved_count", 0),
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
                "queued": queued,
                "active": active,
                "progress": progress,
            },
            "songs": song_list,
            "downloads": self.completed_downloads[-20:],
            "bundle": self.download_bundle,
            "events": self.events[-80:],
            "latest_update": self.latest_update,
            "server": {
                "version": __version__,
                "host": app_state.web_settings["host"],
                "port": app_state.web_settings["port"],
                "keep_alive": app_state.web_settings["keep_alive"],
                "web_use_output_dir": app_state.web_settings["web_use_output_dir"],
                "output_root": self.get_output_root(),
            },
        }

    async def push_state(self, event: Optional[Dict[str, Any]] = None):
        """
        Push the current state snapshot to the websocket client.

        ### Arguments
        - event: optional event that triggered the update
        """

        await self.send_update(
            {
                "type": "state",
                "state": self.get_state_snapshot(),
                "event": event,
            }
        )

    async def add_event(
        self,
        level: str,
        message: str,
        kind: str = "system",
        details: Optional[Any] = None,
        broadcast: bool = True,
    ) -> Dict[str, Any]:
        """
        Add an event to the diagnostics stream.

        ### Arguments
        - level: event severity level
        - message: event message
        - kind: event category
        - details: optional structured details
        - broadcast: whether to immediately push state

        ### Returns
        - event payload
        """

        event = self._append_event(
            {
                "timestamp": self._timestamp(),
                "level": level,
                "kind": kind,
                "message": message,
                "details": details,
            }
        )

        if broadcast:
            await self.push_state(event=event)

        self._persist_client_snapshot()
        return event

    async def handle_song_update(self, update: Dict[str, Any]):
        """
        Apply a song progress update and broadcast it.

        ### Arguments
        - update: serialized progress update
        """

        song_data = update["song"]
        song = Song.from_dict(song_data)
        song_state = self._ensure_song_state(song)

        previous_message = song_state.get("message")
        previous_progress = song_state.get("progress", 0)
        current_progress = int(update["progress"])
        current_message = update["message"]

        song_state.update(
            {
                "display_name": song.display_name,
                "progress": current_progress,
                "message": current_message,
                "status": self._normalize_status(current_message),
                "updated_at": self._timestamp(),
                "song": song_data,
            }
        )

        event = None
        if (
            previous_message != current_message
            or current_message in {"Done", "Error", "Skipped"}
        ):
            event = self._append_event(
                {
                    "timestamp": self._timestamp(),
                    "level": "error" if current_message == "Error" else "info",
                    "kind": "song",
                    "message": f"{song.display_name}: {current_message}",
                    "details": {
                        "song": song_data,
                        "progress": current_progress,
                        "overall_progress": update["overall_progress"],
                        "overall_completed": update["overall_completed"],
                        "overall_total": update["overall_total"],
                    },
                }
            )

        if current_message in {"Done", "Skipped"}:
            self._refresh_completed_downloads_from_output()

        if (
            previous_message != current_message
            or abs(current_progress - previous_progress) >= 2
            or current_progress in {0, 100}
        ):
            await self.push_state(event=event)

        self._persist_client_snapshot()

    def log_event(
        self,
        level: str,
        message: str,
        kind: str = "system",
        details: Optional[Any] = None,
        broadcast: bool = True,
    ):
        """
        Thread-safe wrapper for adding an event.

        ### Arguments
        - level: event severity level
        - message: event message
        - kind: event category
        - details: optional structured details
        - broadcast: whether to immediately push state
        """

        asyncio.run_coroutine_threadsafe(
            self.add_event(level, message, kind=kind, details=details, broadcast=broadcast),
            app_state.loop,
        )

    async def mark_query_resolved(
        self,
        query: str,
        songs: List[Song],
        output_root: str,
    ):
        """
        Mark a query as resolved and initialize queue state.

        ### Arguments
        - query: original query
        - songs: resolved songs
        - output_root: output directory root
        """

        self.current_job.update(
            {
                "status": "running",
                "query": query,
                "message": "Downloading",
                "resolved_count": len(songs),
                "error": None,
                "output_root": output_root,
            }
        )

        for index, song in enumerate(songs, start=1):
            song_state = self._ensure_song_state(song, queue_position=index)
            song_state.update(
                {
                    "message": "Queued",
                    "status": "queued",
                    "progress": 0,
                    "updated_at": self._timestamp(),
                }
            )

        await self.add_event(
            "info",
            f"Resolved {len(songs)} songs for query.",
            kind="job",
            details={"query": query, "output_root": output_root},
        )
        self._persist_client_snapshot()

    async def finish_query_download(
        self,
        results: List[Any],
        errors: List[str],
    ):
        """
        Finalize a query download and store result metadata.

        ### Arguments
        - results: downloader results
        - errors: downloader error strings
        """

        for song, path in results:
            song_state = self._ensure_song_state(song)
            absolute_path = str(path.absolute()) if path is not None else None
            if absolute_path:
                song_state["path"] = absolute_path
                song_state["status"] = "done"
                song_state["message"] = "Done"
                song_state["progress"] = 100
        (
            self.completed_downloads,
            self.download_bundle,
        ) = await asyncio.to_thread(self._build_download_snapshot)
        final_status = "complete-with-errors" if errors else "complete"
        final_message = "Finished"
        final_error = errors[0] if errors else None
        if self.pending_upgrade_prompt is not None:
            final_status = "limit-reached"
            final_message = "Upgrade required"
            final_error = self.pending_upgrade_prompt["message"]

        self.current_job.update(
            {
                "status": final_status,
                "message": final_message,
                "finished_at": self._timestamp(),
                "error": final_error,
            }
        )

        for error in errors:
            self._append_event(
                {
                    "timestamp": self._timestamp(),
                    "level": "error",
                    "kind": "diagnostic",
                    "message": error,
                    "details": None,
                }
            )

        await self.add_event(
            "info",
            f"Download finished with {len(self.completed_downloads)} file(s) and {len(errors)} error(s).",
            kind="job",
            details={
                "downloads": self.completed_downloads[-10:],
                "errors": errors,
                "bundle": self.download_bundle,
            },
        )
        self._persist_client_snapshot()

    async def fail_query_download(self, query: str, exception: Exception):
        """
        Mark the current query as failed.

        ### Arguments
        - query: original query
        - exception: the exception raised
        """

        if isinstance(exception, SubscriptionLimitError):
            self.current_job.update(
                {
                    "status": "limit-reached",
                    "query": query,
                    "message": "Upgrade required",
                    "finished_at": self._timestamp(),
                    "error": str(exception),
                }
            )

            await self.add_event(
                "error",
                str(exception),
                kind="billing",
                details=exception.prompt,
            )
            self._persist_client_snapshot()
            return

        friendly_error = _friendly_job_error_message(exception)

        self.current_job.update(
            {
                "status": "error",
                "query": query,
                "message": "Failed",
                "finished_at": self._timestamp(),
                "error": friendly_error,
            }
        )

        await self.add_event(
            "error",
            f"Download failed: {friendly_error}",
            kind="job",
            details=traceback.format_exc(),
        )
        self._persist_client_snapshot()

    async def start_download_query(self, query: str) -> Dict[str, Any]:
        """
        Start downloading a full query in the background.

        ### Arguments
        - query: the query to resolve and download

        ### Returns
        - state snapshot
        """

        if self.download_task and not self.download_task.done():
            raise HTTPException(
                status_code=409,
                detail="A download is already running for this browser session.",
            )

        self._delete_existing_bundle()
        self._clear_session_output_root()
        self.events = []
        self.song_states = {}
        self.completed_downloads = []
        self.latest_update = None
        self.pending_upgrade_prompt = None
        self.current_job = {
            "status": "starting",
            "query": query,
            "message": "Resolving query",
            "started_at": self._timestamp(),
            "finished_at": None,
            "resolved_count": 0,
            "error": None,
            "output_root": self.get_output_root(),
        }

        await self.add_event(
            "info",
            "Download queued.",
            kind="job",
            details={"query": query, "output_root": self.get_output_root()},
        )

        self._persist_client_snapshot()
        self.download_task = asyncio.create_task(self._run_download_query_task(query))
        return self.get_state_snapshot()

    async def retry_last_query(self) -> Dict[str, Any]:
        """
        Retry the most recent query for this SongZip client.

        ### Returns
        - current state snapshot
        """

        query = str(self.current_job.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="No previous query is available to retry.")

        return await self.start_download_query(query)

    def reset_session_state(self):
        """
        Reset the current dashboard session state and clear persisted snapshot data.
        """

        if self.download_task and not self.download_task.done():
            raise HTTPException(
                status_code=409,
                detail="A download is still running. Wait for it to finish before resetting the session.",
            )

        self._delete_existing_bundle()
        self.events = []
        self.song_states = {}
        self.completed_downloads = []
        self.latest_update = None
        self.pending_upgrade_prompt = None
        self.current_job = {
            "status": "idle",
            "query": None,
            "message": "Ready",
            "started_at": None,
            "finished_at": None,
            "resolved_count": 0,
            "error": None,
            "output_root": self.get_output_root(),
        }
        self.download_task = None
        self._persist_client_snapshot()

    async def _run_download_query_task(self, query: str):
        """
        Run a query download in a worker thread.

        ### Arguments
        - query: the query to process
        """

        try:
            await asyncio.to_thread(self._run_download_query_sync, query)
        except Exception as exception:
            await self.fail_query_download(query, exception)

    def _run_download_query_sync(self, query: str):
        """
        Synchronous worker that resolves and downloads a query.

        ### Arguments
        - query: the query to process
        """

        settings_dict = dict(self.downloader_settings)
        if not app_state.web_settings.get("web_use_output_dir", False):
            settings_dict["output"] = self.get_download_output()

        settings_dict["simple_tui"] = True
        downloader = Downloader(settings=settings_dict)
        downloader.progress_handler = ProgressHandler(
            simple_tui=True,
            update_callback=self.song_update,
        )
        downloader.progress_handler.web_ui = True

        self.log_event(
            "info",
            "Resolving query against supported providers.",
            kind="diagnostic",
            details={"query": query},
            broadcast=False,
        )

        queries = [line.strip() for line in query.splitlines() if line.strip()]
        if len(queries) == 0:
            queries = [query]

        songs = get_simple_songs(
            queries,
            use_ytm_data=downloader.settings["ytm_data"],
            playlist_numbering=downloader.settings["playlist_numbering"],
            album_type=downloader.settings["album_type"],
            playlist_retain_track_cover=downloader.settings[
                "playlist_retain_track_cover"
            ],
        )

        if len(songs) == 0:
            raise ValueError("No songs were found for this query.")

        songs, overflow_count = self._reserve_download_capacity(songs)
        if len(songs) == 0:
            raise SubscriptionLimitError(
                "Free tier limit reached. Upgrade to keep downloading songs.",
                self.pending_upgrade_prompt,
            )

        if overflow_count > 0 and self.pending_upgrade_prompt is not None:
            self.log_event(
                "warning",
                self.pending_upgrade_prompt["message"],
                kind="billing",
                details=self.pending_upgrade_prompt,
                broadcast=False,
            )

        asyncio.run_coroutine_threadsafe(
            self.mark_query_resolved(query, songs, self.get_output_root()),
            app_state.loop,
        ).result()

        results = downloader.download_multiple_songs(songs)

        asyncio.run_coroutine_threadsafe(
            self.finish_query_download(results, list(downloader.errors)),
            app_state.loop,
        ).result()

    async def connect(self):
        """
        Called when a new client connects to the websocket.
        """

        if self.websocket is None:
            raise RuntimeError("Cannot connect a dashboard client without a websocket.")

        await self.websocket.accept()

        # Add the connection to the list of connections
        app_state.clients[self.client_id] = self
        app_state.logger.info("Client %s connected", self.client_id)
        await self.add_event(
            "info",
            "Dashboard connected.",
            kind="system",
        )

    async def send_update(self, update: Dict[str, Any]):
        """
        Send an update to the client.

        ### Arguments
        - update: The update to send.
        """

        if self.websocket is None:
            return

        try:
            await self.websocket.send_json(update)
        except (RuntimeError, WebSocketDisconnect, ConnectionClosed):
            app_state.logger.debug(
                "Client %s websocket closed while sending update",
                self.client_id,
            )
            self.websocket = None

    def song_update(self, progress_handler: SongTracker, message: str):
        """
        Called when a song updates.

        ### Arguments
        - progress_handler: The progress handler.
        - message: The message to send.
        """

        update_message = {
            "song": progress_handler.song.json,
            "progress": progress_handler.progress,
            "message": message,
            "overall_progress": round(
                (
                    progress_handler.parent.overall_progress
                    / progress_handler.parent.overall_total
                    * 100
                )
                if progress_handler.parent.overall_total
                else 0,
                1,
            ),
            "overall_completed": progress_handler.parent.overall_completed_tasks,
            "overall_total": progress_handler.parent.song_count,
        }

        asyncio.run_coroutine_threadsafe(
            self.handle_song_update(update_message), app_state.loop
        )

    @classmethod
    def get_instance(cls, client_id: str) -> Optional["Client"]:
        """
        Get the WebSocket instance for a client.

        ### Arguments
        - client_id: The client's ID.

        ### Returns
        - returns the WebSocket instance.
        """

        instance = app_state.clients.get(client_id)
        if instance:
            return instance

        app_state.logger.debug("Client %s not found in active dashboard sessions", client_id)

        return None


class ApplicationState:
    """
    Class that holds the application state.
    """

    api: FastAPI
    server: Server
    loop: asyncio.AbstractEventLoop
    web_settings: WebOptions
    downloader_settings: DownloaderOptions
    clients: Dict[str, Client] = {}
    logger: logging.Logger


class ProviderActionRequest(BaseModel):
    """
    Provider action body used by the web auth endpoints.
    """

    provider: str


class AccountCredentialsRequest(BaseModel):
    """
    Account registration / login body.
    """

    email: str
    password: str


class AccountCreditGrantRequest(BaseModel):
    """
    Admin credit-grant body.
    """

    account_identifier: str
    credits: int


class AccountMembershipGrantRequest(BaseModel):
    """
    Admin membership body.
    """

    account_identifier: str
    tier: str


class SubscriptionActivationRequest(BaseModel):
    """
    Subscription activation body used by the pricing flow.
    """

    tier: str
    subscription_id: Optional[str] = None


router = APIRouter()
app_state: ApplicationState = ApplicationState()


def get_current_state() -> ApplicationState:
    """
    Get the current state of the application.

    ### Returns
    - returns the application state.
    """

    return app_state


def get_client(
    client_id: Union[str, None] = Query(default=None),
    account_key: Union[str, None] = Query(default=None),
    request: Request = None,
) -> Client:
    """
    Get the client's state.

    ### Arguments
    - client_id: The client's ID.

    ### Returns
    - returns the client's state.
    """

    if client_id is None:
        raise HTTPException(status_code=400, detail="client_id is required")

    authenticated_account = _resolve_authenticated_account(request)
    resolved_account_key = (
        authenticated_account.get("account_key")
        if authenticated_account is not None
        else account_key
    )

    try:
        instance = Client.get_instance(client_id)
        if instance is None:
            instance = Client(
                None,
                client_id,
                account_key=resolved_account_key,
                authenticated_account=authenticated_account,
            )
            instance._refresh_completed_downloads_from_output()
            app_state.clients[client_id] = instance
        else:
            instance.set_authenticated_account(authenticated_account)
            instance.set_account_key(resolved_account_key)
            instance.subscription = instance._load_subscription_state()
    except DownloaderError as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "SongZip could not initialize the downloader runtime. "
                f"{error}"
            ),
        ) from error

    return instance


@router.websocket("/api/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    client_id: str,
    account_key: Optional[str] = None,
):
    """
    Websocket endpoint.

    ### Arguments
    - websocket: The WebSocket instance.
    """

    authenticated_account = _decorate_account(
        songzip_store.get_account_by_session(
        websocket.cookies.get(SONGZIP_SESSION_COOKIE)
        )
    )
    resolved_account_key = (
        authenticated_account.get("account_key")
        if authenticated_account is not None
        else account_key
    )

    client = Client.get_instance(client_id)
    if client is None:
        client = Client(
            websocket,
            client_id,
            account_key=resolved_account_key,
            authenticated_account=authenticated_account,
        )
    else:
        client.set_authenticated_account(authenticated_account)
        client.set_account_key(resolved_account_key)
        client.subscription = client._load_subscription_state()
        client.attach_websocket(websocket)

    await client.connect()

    try:
        while True:
            await websocket.receive_json()
    except WebSocketDisconnect:
        client.detach_websocket(websocket)

        if (
            len([session for session in app_state.clients.values() if session.websocket])
            == 0
            and app_state.web_settings["keep_alive"] is False
        ):
            app_state.logger.debug(
                "No active connections, waiting 1s before shutting down"
            )

            await asyncio.sleep(1)

            # Wait 1 second before shutting down
            # This is to prevent the server from shutting down when a client
            # disconnects and reconnects quickly (e.g. when refreshing the page)
            if len(
                [session for session in app_state.clients.values() if session.websocket]
            ) == 0:
                # Perform a clean exit
                app_state.logger.info("Shutting down server, no active connections")
                app_state.server.force_exit = True
                app_state.server.should_exit = True
                await app_state.server.shutdown()


# Deprecated
@router.get("/api/song/url", response_model=None)
def song_from_url(url: str) -> Song:
    """
    Search for a song on spotify using url.

    ### Arguments
    - url: The url to search.

    ### Returns
    - returns the first result as a Song object.
    """

    return Song.from_url(url)


@router.get("/api/url", response_model=None)
def songs_from_url(url: str) -> List[Song]:
    """
    Search for a song, playlist, artist or album on spotify using url.

    ### Arguments
    - url: The url to search.

    ### Returns
    - returns a list with Song objects to be downloaded.
    """

    if "playlist" in url:
        playlist = Playlist.from_url(url)
        return list(map(Song.from_url, playlist.urls))
    if "album" in url:
        album = Album.from_url(url)
        return list(map(Song.from_url, album.urls))
    if "artist" in url:
        artist = Artist.from_url(url)
        return list(map(Song.from_url, artist.urls))

    return [Song.from_url(url)]


@router.get("/api/version", response_model=None)
def version() -> str:
    """
    Get the current version
    This method is created to ensure backward compatibility of the web app,
    as the web app is updated with the latest regardless of the backend version

    ### Returns
    -  returns the version of the app
    """

    return __version__


@router.on_event("shutdown")
async def shutdown_event():
    """
    Called when the server is shutting down.
    """

    if (
        not app_state.web_settings["keep_sessions"]
        and not app_state.web_settings["web_use_output_dir"]
    ):
        app_state.logger.info("Removing sessions directories")
        sessions_dir = Path(get_spotdl_path(), "web/sessions")
        if sessions_dir.exists():
            shutil.rmtree(sessions_dir)


@router.get("/api/songs/search", response_model=None)
def query_search(query: str) -> List[Song]:
    """
    Parse search term and return list of Song objects.

    ### Arguments
    - query: The query to parse.

    ### Returns
    - returns a list of Song objects.
    """

    return get_search_results(query)


@router.get("/api/session/state", response_model=None)
def session_state(
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Get the current dashboard state for a client.

    ### Arguments
    - client: the client's state

    ### Returns
    - state snapshot
    """

    return client.get_state_snapshot()


@router.get("/api/account/me", response_model=None)
def account_me(
    request: Request,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Return the current authenticated SongZip account, if any.
    """

    account = _resolve_authenticated_account(request)
    if account is None:
        client.set_authenticated_account(None)
        return {
            "authenticated": False,
            "account": None,
            "account_key": client.account_key,
        }

    client.set_authenticated_account(account)
    client.subscription = client._load_subscription_state()
    return {
        "authenticated": True,
        "account": account,
        "account_key": client.account_key,
    }


@router.get("/api/account/google/start", response_model=None)
def account_google_start(
    request: Request,
    client_id: Union[str, None] = Query(default=None),
    account_key: Union[str, None] = Query(default=None),
) -> RedirectResponse:
    """
    Start Google OAuth login for a SongZip account.
    """

    resolved_client_id = str(client_id or "").strip()
    resolved_account_key = _normalize_account_key(account_key) or _normalize_account_key(
        resolved_client_id
    )
    if not resolved_client_id:
        raise HTTPException(status_code=400, detail="SongZip client_id is required.")

    try:
        client_config = _google_login_client_config(request)
    except HTTPException as error:
        return RedirectResponse(
            url=_build_google_login_redirect_back(request, "error", str(error.detail)),
            status_code=303,
        )

    _prune_google_account_states()
    state_token = secrets.token_urlsafe(24)
    _pending_google_account_states[state_token] = {
        "client_id": resolved_client_id,
        "account_key": resolved_account_key,
        "created_at": _timestamp_now(),
        "redirect_uri": client_config["redirect_uri"],
    }

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode(
            {
                "client_id": client_config["client_id"],
                "redirect_uri": client_config["redirect_uri"],
                "response_type": "code",
                "scope": " ".join(GOOGLE_ACCOUNT_OAUTH_SCOPES),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "select_account",
                "state": state_token,
            }
        )
    )
    return RedirectResponse(url=auth_url, status_code=303)


@router.get("/api/account/google/callback", response_model=None)
def account_google_callback(
    request: Request,
    response: Response,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    """
    Complete Google OAuth login for a SongZip account.
    """

    if error:
        return RedirectResponse(
            url=_build_google_login_redirect_back(
                request,
                "error",
                "Google sign-in was cancelled or denied.",
            ),
            status_code=303,
        )

    _prune_google_account_states()
    pending = _pending_google_account_states.pop(str(state or ""), None)
    if pending is None:
        return RedirectResponse(
            url=_build_google_login_redirect_back(
                request,
                "error",
                "The Google sign-in session expired. Start again.",
            ),
            status_code=303,
        )

    if not code:
        return RedirectResponse(
            url=_build_google_login_redirect_back(
                request,
                "error",
                "Google sign-in did not return an authorization code.",
            ),
            status_code=303,
        )

    try:
        client_config = _google_login_client_config(request)
        client_config["redirect_uri"] = pending.get("redirect_uri") or client_config["redirect_uri"]
        token_payload = _exchange_google_login_code(client_config, code)
        profile = _fetch_google_login_profile(str(token_payload["access_token"]))
        account = songzip_store.get_or_create_google_account(
            email=str(profile["email"]),
            google_subject=str(profile["sub"]),
            display_name=str(profile.get("name") or profile["email"]),
            preferred_account_key=pending.get("account_key"),
        )
        account = _claim_google_admin_if_needed(account)
        session_token = songzip_store.create_session(
            account["id"],
            user_agent=request.headers.get("user-agent"),
        )
        redirect = RedirectResponse(
            url=_build_google_login_redirect_back(
                request,
                "connected",
                f"Signed in with Google as {account['email']}.",
            ),
            status_code=303,
        )
        _set_songzip_session_cookie(redirect, session_token)
        _migrate_subscription_state(
            pending.get("account_key") or pending.get("client_id") or "",
            account["account_key"],
        )
        return redirect
    except (HTTPException, SongZipStoreError) as error:
        detail = error.detail if isinstance(error, HTTPException) else str(error)
        return RedirectResponse(
            url=_build_google_login_redirect_back(request, "error", str(detail)),
            status_code=303,
        )


@router.post("/api/account/register", response_model=None)
def account_register(
    payload: AccountCredentialsRequest,
    request: Request,
    response: Response,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Create a SongZip account and start an authenticated session.
    """

    try:
        account = songzip_store.register_account(
            payload.email,
            payload.password,
            preferred_account_key=client.account_key,
        )
        session_token = songzip_store.create_session(
            account["id"],
            user_agent=request.headers.get("user-agent"),
        )
    except SongZipStoreError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    _set_songzip_session_cookie(response, session_token)
    account = _decorate_account(account) or account
    client.set_authenticated_account(account)
    client.subscription = client._load_subscription_state()
    return {
        "authenticated": True,
        "account": account,
        "subscription": client.get_subscription_snapshot(),
    }


@router.post("/api/account/login", response_model=None)
def account_login(
    payload: AccountCredentialsRequest,
    request: Request,
    response: Response,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Sign in to an existing SongZip account.
    """

    guest_account_key = client.account_key
    try:
        account = songzip_store.authenticate_account(payload.email, payload.password)
        session_token = songzip_store.create_session(
            account["id"],
            user_agent=request.headers.get("user-agent"),
        )
    except SongZipStoreError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    _set_songzip_session_cookie(response, session_token)
    account = _decorate_account(account) or account
    migrated_subscription = _migrate_subscription_state(
        guest_account_key,
        account["account_key"],
    )
    client.set_authenticated_account(account)
    client.subscription = migrated_subscription
    return {
        "authenticated": True,
        "account": account,
        "subscription": client.get_subscription_snapshot(),
    }


@router.post("/api/account/logout", response_model=None)
def account_logout(
    request: Request,
    response: Response,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    End the current SongZip account session.
    """

    session_token = request.cookies.get(SONGZIP_SESSION_COOKIE)
    songzip_store.delete_session(session_token)
    _clear_songzip_session_cookie(response)
    client.set_authenticated_account(None)
    client.set_account_key(client.client_id)
    client.subscription = client._load_subscription_state()
    return {
        "authenticated": False,
        "account": None,
        "account_key": client.account_key,
    }


@router.post("/api/account/credits/grant", response_model=None)
def grant_account_credits(
    payload: AccountCreditGrantRequest,
    request: Request,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Grant extra free-tier song credits to a SongZip account.
    """

    _assert_admin_account(request)
    try:
        result = songzip_store.grant_bonus_credits(
            payload.account_identifier,
            payload.credits,
        )
    except SongZipStoreError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    granted_account = _decorate_account(result["account"]) or result["account"]
    if granted_account["account_key"] == client.account_key:
        client.subscription = client._load_subscription_state()
        client.pending_upgrade_prompt = None
        subscription = client.get_subscription_snapshot()
    else:
        subscription = _build_subscription_snapshot(
            granted_account["account_key"],
            result["subscription"],
        )

    return {
        "account": granted_account,
        "subscription": subscription,
    }


@router.post("/api/account/membership", response_model=None)
def update_account_membership(
    payload: AccountMembershipGrantRequest,
    request: Request,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Grant or cancel a SongZip membership tier for an account.
    """

    _assert_admin_account(request)
    try:
        result = songzip_store.set_account_membership(
            payload.account_identifier,
            payload.tier,
        )
    except SongZipStoreError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    granted_account = _decorate_account(result["account"]) or result["account"]
    if granted_account["account_key"] == client.account_key:
        client.subscription = client._load_subscription_state()
        client.pending_upgrade_prompt = None
        subscription = client.get_subscription_snapshot()
    else:
        subscription = _build_subscription_snapshot(
            granted_account["account_key"],
            result["subscription"],
        )

    return {
        "account": granted_account,
        "subscription": subscription,
    }


@router.get("/api/auth/providers", response_model=None)
def auth_provider_status(
    client: Client = Depends(get_client),
) -> List[Dict[str, Any]]:
    """
    Return OAuth provider connection details for the current dashboard client.

    ### Arguments
    - client: the client's state

    ### Returns
    - public provider connection details
    """

    return provider_auth_manager.get_provider_statuses(client.client_id)


@router.post("/api/auth/start", response_model=None)
def start_auth_provider(
    action: ProviderActionRequest,
    client: Client = Depends(get_client),
) -> Dict[str, str]:
    """
    Build the provider OAuth URL for the current dashboard client.

    ### Arguments
    - action: requested provider
    - client: the client's state

    ### Returns
    - authorization url payload
    """

    try:
        auth_url = provider_auth_manager.build_authorization_url(
            action.provider,
            client.client_id,
        )
    except ProviderAuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {
        "provider": action.provider,
        "auth_url": auth_url,
    }


@router.get("/api/auth/callback/{provider}", response_model=None)
def auth_provider_callback(
    provider: str,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    """
    Complete the provider OAuth callback and redirect back to the frontend.

    ### Arguments
    - provider: oauth provider key
    - state: oauth state token
    - code: provider authorization code
    - error: provider oauth error

    ### Returns
    - redirect back to the web app
    """

    try:
        result = provider_auth_manager.complete_callback(provider, state, code, error)
        redirect_url = provider_auth_manager.build_callback_redirect(
            provider,
            "connected",
            f"{result['label']} connected for {result['account_label']}.",
        )
    except ProviderAuthError as auth_error:
        redirect_url = provider_auth_manager.build_callback_redirect(
            provider,
            "error",
            str(auth_error),
        )

    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/api/auth/disconnect", response_model=None)
def disconnect_auth_provider(
    action: ProviderActionRequest,
    client: Client = Depends(get_client),
) -> Dict[str, bool]:
    """
    Disconnect a stored provider link for the current dashboard client.

    ### Arguments
    - action: requested provider
    - client: the client's state

    ### Returns
    - whether the provider was disconnected
    """

    try:
        disconnected = provider_auth_manager.disconnect(
            action.provider,
            client.client_id,
        )
    except ProviderAuthError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {"ok": disconnected}


@router.post("/api/subscription/activate", response_model=None)
def activate_subscription(
    payload: SubscriptionActivationRequest,
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Activate a subscription tier for the current browser session.

    ### Arguments
    - payload: target tier and optional provider subscription id
    - client: the client's state

    ### Returns
    - current state snapshot
    """

    client.activate_subscription_tier(
        payload.tier,
        subscription_id=payload.subscription_id,
    )
    return client.get_state_snapshot()


@router.post("/api/paypal/webhook", response_model=None)
async def paypal_webhook(request: Request) -> Dict[str, Any]:
    """
    Receive and verify a PayPal webhook notification.

    ### Arguments
    - request: raw webhook request

    ### Returns
    - acknowledgment payload
    """

    webhook_event = await request.json()
    if not isinstance(webhook_event, dict):
        raise HTTPException(status_code=400, detail="Invalid PayPal webhook payload.")

    if not _verify_paypal_webhook_signature(dict(request.headers), webhook_event):
        raise HTTPException(status_code=400, detail="PayPal webhook signature verification failed.")

    record = _process_paypal_webhook_event(webhook_event)
    return {
        "ok": True,
        "event_type": webhook_event.get("event_type"),
        "subscription_id": record.get("subscription_id") if record else None,
    }


@router.post("/api/download/query", response_model=None)
async def download_query(
    query: str = Body(..., embed=True),
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Resolve a query and download all matching songs in the background.

    ### Arguments
    - query: the query string to download
    - client: the client's state

    ### Returns
    - current state snapshot
    """

    stripped_query = query.strip()
    if stripped_query == "":
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    return await client.start_download_query(stripped_query)


@router.post("/api/download/retry-last", response_model=None)
async def retry_last_download_query(
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Retry the latest query for the current SongZip dashboard client.

    ### Arguments
    - client: the client's state

    ### Returns
    - current state snapshot
    """

    return await client.retry_last_query()


@router.post("/api/session/reset", response_model=None)
def reset_session_state(
    client: Client = Depends(get_client),
) -> Dict[str, Any]:
    """
    Clear the current dashboard job/session view without affecting account access.

    ### Arguments
    - client: the client's state

    ### Returns
    - reset state snapshot
    """

    client.reset_session_state()
    return client.get_state_snapshot()


@router.post("/api/download/url")
async def download_url(
    url: str,
    client: Client = Depends(get_client),
    state: ApplicationState = Depends(get_current_state),
) -> Optional[str]:
    """
    Download songs using Song url.

    ### Arguments
    - url: The url to download.

    ### Returns
    - returns the file path if the song was downloaded.
    """

    if state.web_settings.get("web_use_output_dir", False):
        client.downloader.settings["output"] = client.downloader_settings["output"]
    else:
        client.downloader.settings["output"] = client.get_download_output()

    client.downloader.progress_handler = ProgressHandler(
        simple_tui=True,
        update_callback=client.song_update,
    )

    try:
        # Fetch song metadata
        song = Song.from_url(url)

        # Download Song
        _, path = await client.downloader.pool_download(song)

        if path is None:
            state.logger.error(f"Failure downloading {song.name}")

            raise HTTPException(
                status_code=500, detail=f"Error downloading: {song.name}"
            )

        return str(path.absolute())

    except Exception as exception:
        state.logger.error(f"Error downloading! {exception}")

        raise HTTPException(
            status_code=500, detail=f"Error downloading: {exception}"
        ) from exception


@router.get("/api/download/file")
async def download_file(
    file: str,
    client: Client = Depends(get_client),
    state: ApplicationState = Depends(get_current_state),
):
    """
    Download file using path.

    ### Arguments
    - file: The file path.
    - client: The client's state.

    ### Returns
    - returns the file response, filename specified to return as attachment.
    """

    file_path = Path(file).absolute()
    expected_root = Path(get_spotdl_path() / "web/sessions").absolute()
    if state.web_settings.get("web_use_output_dir", False):
        expected_root = Path(
            client.downloader_settings["output"].split("{", 1)[0]
        ).absolute()

    if file_path.suffix.lower() != f".{client.downloader_settings['format']}".lower():
        raise HTTPException(status_code=400, detail="Invalid download path.")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="The requested file is missing.")

    if not _is_path_within_root(file_path, expected_root):
        raise HTTPException(status_code=400, detail="Invalid download path.")

    return FileResponse(
        file_path,
        filename=os.path.basename(file_path),
    )


@router.get("/api/download/bundle")
def download_bundle(
    client: Client = Depends(get_client),
):
    """
    Download the current session bundle as a zip archive.

    ### Arguments
    - client: The client's state.

    ### Returns
    - bundle file response
    """

    if client.download_bundle is None:
        client._refresh_completed_downloads_from_output()

    bundle = client.download_bundle
    if bundle is None:
        raise HTTPException(status_code=404, detail="No download bundle is ready yet.")

    bundle_path = bundle.get("path")
    if not bundle_path or not Path(bundle_path).is_file():
        raise HTTPException(status_code=404, detail="The download bundle is missing.")

    return FileResponse(
        bundle_path,
        filename=bundle.get("name", "spotdl-downloads.zip"),
        media_type="application/zip",
    )


@router.get("/api/settings")
def get_settings(
    client: Client = Depends(get_client),
) -> DownloaderOptions:
    """
    Get client settings.

    ### Arguments
    - client: The client's state.

    ### Returns
    - returns the settings.
    """

    return client.downloader_settings


@router.post("/api/settings/update")
def update_settings(
    settings: DownloaderOptionalOptions,
    client: Client = Depends(get_client),
    state: ApplicationState = Depends(get_current_state),
) -> DownloaderOptions:
    """
    Update client settings, and re-initialize downloader.

    ### Arguments
    - settings: The settings to change.
    - client: The client's state.
    - state: The application state.

    ### Returns
    - returns True if the settings were changed.
    """

    def is_blank(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip() == ""

        if isinstance(value, list):
            return len(value) == 0 or all(
                isinstance(item, str) and item.strip() == "" for item in value
            )

        return False

    # Create shallow copy of settings
    settings_cpy = client.downloader_settings.copy()

    # Update settings with new settings that are not None
    settings_cpy.update({k: v for k, v in settings.items() if v is not None})  # type: ignore

    for key, default_value in DOWNLOADER_OPTIONS.items():
        if is_blank(settings_cpy.get(key)):
            settings_cpy[key] = default_value

    if "cookie_file" in settings_cpy:
        settings_cpy["cookie_file"] = _resolve_cookie_file_setting(
            client.account_key,
            settings_cpy.get("cookie_file"),
        )

    forced_format = state.web_settings.get("forced_format")
    if forced_format:
        settings_cpy["format"] = forced_format

    forced_output = state.web_settings.get("forced_output")
    if forced_output:
        settings_cpy["output"] = forced_output

    new_settings = _normalize_web_downloader_settings(settings_cpy)
    state.logger.info("Applying settings: %s", dict(new_settings))

    # Re-initialize downloader
    client.downloader_settings = new_settings
    client.downloader = Downloader(
        new_settings,
        loop=state.loop,
    )
    client.downloader.progress_handler.web_ui = True
    songzip_store.save_account_settings(client.account_key, dict(new_settings))

    return new_settings


@router.get("/api/check_update")
def check_update() -> bool:
    """
    Check for update.

    ### Returns
    - returns True if there is an update.
    """

    try:
        _, ahead, _ = get_status(__version__, "master")
        if ahead > 0:
            return True
    except RuntimeError:
        latest_version = get_latest_version()
        latest_tuple = tuple(latest_version.replace("v", "").split("."))
        current_tuple = tuple(__version__.split("."))
        if latest_tuple > current_tuple:
            return True
    except RateLimitError:
        return False

    return False


@router.get("/api/options_model")
def get_options() -> Dict[str, Any]:
    """
    Get options model (possible settings).

    ### Returns
    - returns the options.
    """

    parser = create_parser()

    # Forbidden actions
    forbidden_actions = [
        "help",
        "operation",
        "version",
        "config",
        "user_auth",
        "client_id",
        "client_secret",
        "auth_token",
        "cache_path",
        "no_cache",
        "cookie_file",
        "ffmpeg",
        "archive",
        "host",
        "port",
        "keep_alive",
        "enable_tls",
        "key_file",
        "cert_file",
        "ca_file",
        "allowed_origins",
        "web_use_output_dir",
        "keep_sessions",
        "log_level",
        "simple_tui",
        "headless",
        "download_ffmpeg",
        "generate_config",
        "check_for_updates",
        "profile",
        "version",
    ]

    options = {}
    for action in parser._actions:  # pylint: disable=protected-access
        if action.dest in forbidden_actions:
            continue

        default = app_state.downloader_settings.get(action.dest, None)
        choices = list(action.choices) if action.choices else None

        type_name = ""
        if action.type is not None:
            if hasattr(action.type, "__objclass__"):
                type_name: str = action.type.__objclass__.__name__  # type: ignore
            else:
                type_name: str = action.type.__name__  # type: ignore

        if isinstance(
            action, argparse._StoreConstAction  # pylint: disable=protected-access
        ):
            type_name = "bool"

        if choices is not None and action.nargs == "*":
            type_name = "list"

        options[action.dest] = {
            "type": type_name,
            "choices": choices,
            "default": default,
            "help": action.help,
        }

    return options


def fix_mime_types():
    """Fix incorrect entries in the `mimetypes` registry.
    On Windows, the Python standard library's `mimetypes` reads in
    mappings from file extension to MIME type from the Windows
    registry. Other applications can and do write incorrect values
    to this registry, which causes `mimetypes.guess_type` to return
    incorrect values, which causes spotDL to fail to render on
    the frontend.
    This method hard-codes the correct mappings for certain MIME
    types that are known to be either used by TensorBoard or
    problematic in general.
    """

    # Known to be problematic when Visual Studio is installed:
    # <https://github.com/tensorflow/tensorboard/issues/3120>
    # https://github.com/spotDL/spotify-downloader/issues/1540
    mimetypes.add_type("application/javascript", ".js")

    # Not known to be problematic, but used by spotDL:
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("image/svg+xml", ".svg")
    mimetypes.add_type("text/html", ".html")
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    mimetypes.add_type(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    )
