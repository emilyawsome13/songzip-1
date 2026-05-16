"""
Helpers for official user-linked OAuth provider connections in the web UI.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

import requests

from spotdl.utils.config import get_spotdl_path

__all__ = [
    "ProviderAuthError",
    "ProviderAuthManager",
    "provider_auth_manager",
]


class ProviderAuthError(Exception):
    """
    Raised when provider authentication could not be completed.
    """


@dataclass(frozen=True)
class ProviderSpec:
    """
    Static details for an OAuth provider integration.
    """

    key: str
    label: str
    authorize_url: str
    token_url: str
    client_id_env: str
    client_secret_env: str
    redirect_uri_env: str
    scopes: List[str]
    setup_help_url: str
    profile_url: str
    profile_label_key: str
    profile_id_key: str
    refresh_requires_basic_auth: bool = False
    auth_params: Dict[str, str] = field(default_factory=dict)
    token_params: Dict[str, str] = field(default_factory=dict)


PROVIDER_SPECS: Dict[str, ProviderSpec] = {
    "spotify": ProviderSpec(
        key="spotify",
        label="Spotify",
        authorize_url="https://accounts.spotify.com/authorize",
        token_url="https://accounts.spotify.com/api/token",
        client_id_env="SPOTDL_SPOTIFY_OAUTH_CLIENT_ID",
        client_secret_env="SPOTDL_SPOTIFY_OAUTH_CLIENT_SECRET",
        redirect_uri_env="SPOTDL_SPOTIFY_OAUTH_REDIRECT_URI",
        scopes=[
            "user-library-read",
            "user-follow-read",
            "playlist-read-private",
            "playlist-read-collaborative",
        ],
        setup_help_url="https://developer.spotify.com/documentation/web-api/concepts/authorization",
        profile_url="https://api.spotify.com/v1/me",
        profile_label_key="display_name",
        profile_id_key="id",
        refresh_requires_basic_auth=True,
        auth_params={
            "response_type": "code",
            "show_dialog": "true",
        },
    ),
    "youtube": ProviderSpec(
        key="youtube",
        label="Google / YouTube",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        client_id_env="SPOTDL_GOOGLE_OAUTH_CLIENT_ID",
        client_secret_env="SPOTDL_GOOGLE_OAUTH_CLIENT_SECRET",
        redirect_uri_env="SPOTDL_GOOGLE_OAUTH_REDIRECT_URI",
        scopes=[
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
        setup_help_url="https://developers.google.com/youtube/v3/guides/authentication",
        profile_url="https://openidconnect.googleapis.com/v1/userinfo",
        profile_label_key="email",
        profile_id_key="sub",
        auth_params={
            "response_type": "code",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        },
    ),
}

PENDING_STATE_TTL_SECONDS = 900
TOKEN_EXPIRY_SAFETY_SECONDS = 60


def _utc_now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None

    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


class ProviderAuthManager:
    """
    Handles OAuth connection setup, callbacks, and storage for dashboard clients.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or (
            get_spotdl_path() / "web" / "provider_connections.json"
        )
        self._connections = self._load_connections()
        self._pending_states: Dict[str, Dict[str, str]] = {}

    def get_provider_statuses(self, client_id: str) -> List[Dict[str, Any]]:
        """
        Return public provider connection details for a dashboard client.
        """

        client_connections = self._connections.get("clients", {}).get(client_id, {})
        statuses: List[Dict[str, Any]] = []
        for spec in PROVIDER_SPECS.values():
            configured, missing = self._provider_configuration(spec)
            connection = client_connections.get(spec.key)
            statuses.append(
                {
                    "provider": spec.key,
                    "label": spec.label,
                    "configured": configured,
                    "connected": connection is not None,
                    "setup_missing": missing,
                    "setup_help_url": spec.setup_help_url,
                    "account_label": connection.get("account_label") if connection else None,
                    "account_id": connection.get("account_id") if connection else None,
                    "connected_at": connection.get("connected_at") if connection else None,
                    "token_expires_at": connection.get("expires_at") if connection else None,
                    "scopes": connection.get("scope", []) if connection else [],
                }
            )

        return statuses

    def build_authorization_url(self, provider_key: str, client_id: str) -> str:
        """
        Build the provider authorization URL for a client.
        """

        spec = self._get_spec(provider_key)
        configured, missing = self._provider_configuration(spec)
        if not configured:
            missing_str = ", ".join(missing)
            raise ProviderAuthError(
                f"{spec.label} OAuth is not configured. Missing: {missing_str}."
            )

        self._prune_pending_states()
        oauth_state = secrets.token_urlsafe(24)
        self._pending_states[oauth_state] = {
            "provider": spec.key,
            "client_id": client_id,
            "created_at": _timestamp(),
        }

        query = {
            **spec.auth_params,
            "client_id": os.environ[spec.client_id_env],
            "redirect_uri": os.environ[spec.redirect_uri_env],
            "scope": " ".join(spec.scopes),
            "state": oauth_state,
        }

        return f"{spec.authorize_url}?{urlencode(query)}"

    def complete_callback(
        self,
        provider_key: str,
        state_token: Optional[str],
        code: Optional[str],
        error: Optional[str],
    ) -> Dict[str, str]:
        """
        Complete the provider callback and store the connection for the client.
        """

        spec = self._get_spec(provider_key)

        if error:
            raise ProviderAuthError(f"{spec.label} sign-in was cancelled or denied.")

        if not state_token:
            raise ProviderAuthError("Missing OAuth state.")

        pending = self._pending_states.pop(state_token, None)
        if pending is None or pending.get("provider") != spec.key:
            raise ProviderAuthError("The sign-in session expired. Start again.")

        if not code:
            raise ProviderAuthError("Missing authorization code.")

        token_data = self._exchange_code_for_token(spec, code)
        profile = self._fetch_profile(spec, token_data["access_token"])
        connection = self._build_connection_record(spec, token_data, profile)

        client_id = pending["client_id"]
        clients = self._connections.setdefault("clients", {})
        clients.setdefault(client_id, {})[spec.key] = connection
        self._save_connections()

        return {
            "client_id": client_id,
            "provider": spec.key,
            "label": spec.label,
            "account_label": connection["account_label"],
        }

    def disconnect(self, provider_key: str, client_id: str) -> bool:
        """
        Remove a stored provider connection for a client.
        """

        spec = self._get_spec(provider_key)
        clients = self._connections.get("clients", {})
        client_connections = clients.get(client_id, {})
        if spec.key not in client_connections:
            return False

        client_connections.pop(spec.key, None)
        if len(client_connections) == 0:
            clients.pop(client_id, None)

        self._save_connections()
        return True

    def build_callback_redirect(
        self,
        provider_key: str,
        status: str,
        message: str,
    ) -> str:
        """
        Build the frontend redirect URL used after a callback finishes.
        """

        spec = self._get_spec(provider_key)
        configured, _ = self._provider_configuration(spec)
        redirect_uri = os.environ.get(spec.redirect_uri_env) if configured else None
        if not redirect_uri:
            base_url = "/"
        else:
            parsed = urlparse(redirect_uri)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"

        query = urlencode(
            {
                "auth_provider": provider_key,
                "auth_status": status,
                "auth_message": message,
            }
        )
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}{query}"

    def _provider_configuration(self, spec: ProviderSpec) -> tuple[bool, List[str]]:
        missing = [
            env_name
            for env_name in (
                spec.client_id_env,
                spec.client_secret_env,
                spec.redirect_uri_env,
            )
            if not os.environ.get(env_name)
        ]
        return len(missing) == 0, missing

    def _exchange_code_for_token(
        self,
        spec: ProviderSpec,
        code: str,
    ) -> Dict[str, Any]:
        payload = {
            **spec.token_params,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": os.environ[spec.redirect_uri_env],
        }

        headers = {"Accept": "application/json"}
        if spec.refresh_requires_basic_auth:
            basic = base64.b64encode(
                (
                    f"{os.environ[spec.client_id_env]}:"
                    f"{os.environ[spec.client_secret_env]}"
                ).encode("utf-8")
            ).decode("utf-8")
            headers["Authorization"] = f"Basic {basic}"
        else:
            payload["client_id"] = os.environ[spec.client_id_env]
            payload["client_secret"] = os.environ[spec.client_secret_env]

        response = requests.post(
            spec.token_url,
            data=payload,
            headers=headers,
            timeout=20,
        )
        if response.status_code >= 400:
            raise ProviderAuthError(self._provider_error_message(spec, response))

        token_data = response.json()
        if "access_token" not in token_data:
            raise ProviderAuthError(f"{spec.label} did not return an access token.")

        return token_data

    def _fetch_profile(self, spec: ProviderSpec, access_token: str) -> Dict[str, Any]:
        response = requests.get(
            spec.profile_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if response.status_code >= 400:
            raise ProviderAuthError(f"{spec.label} account details could not be loaded.")

        return response.json()

    def _build_connection_record(
        self,
        spec: ProviderSpec,
        token_data: Dict[str, Any],
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        expires_in = int(token_data.get("expires_in") or 3600)
        expires_at = (_utc_now() + dt.timedelta(seconds=expires_in)).isoformat()
        raw_scope = token_data.get("scope", "")
        if isinstance(raw_scope, str):
            scope = [value for value in raw_scope.split(" ") if value]
        else:
            scope = list(spec.scopes)

        account_label = profile.get(spec.profile_label_key) or profile.get(
            spec.profile_id_key
        )
        if not account_label:
            account_label = spec.label

        return {
            "provider": spec.key,
            "account_id": str(profile.get(spec.profile_id_key) or ""),
            "account_label": str(account_label),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "token_type": token_data.get("token_type", "Bearer"),
            "scope": scope,
            "expires_at": expires_at,
            "connected_at": _timestamp(),
            "profile": profile,
        }

    def get_access_token(self, provider_key: str, client_id: str) -> Optional[str]:
        """
        Return a fresh access token for a stored provider connection.
        """

        spec = self._get_spec(provider_key)
        connection = (
            self._connections.get("clients", {})
            .get(client_id, {})
            .get(spec.key)
        )
        if connection is None:
            return None

        if self._token_is_fresh(connection):
            return connection.get("access_token")

        refresh_token = connection.get("refresh_token")
        if not refresh_token:
            return connection.get("access_token")

        refreshed = self._refresh_access_token(spec, refresh_token)
        connection["access_token"] = refreshed["access_token"]
        connection["expires_at"] = refreshed["expires_at"]
        if refreshed.get("refresh_token"):
            connection["refresh_token"] = refreshed["refresh_token"]
        if refreshed.get("scope"):
            connection["scope"] = refreshed["scope"]
        self._save_connections()
        return connection.get("access_token")

    def _refresh_access_token(
        self,
        spec: ProviderSpec,
        refresh_token: str,
    ) -> Dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        headers = {"Accept": "application/json"}
        if spec.refresh_requires_basic_auth:
            basic = base64.b64encode(
                (
                    f"{os.environ[spec.client_id_env]}:"
                    f"{os.environ[spec.client_secret_env]}"
                ).encode("utf-8")
            ).decode("utf-8")
            headers["Authorization"] = f"Basic {basic}"
        else:
            payload["client_id"] = os.environ[spec.client_id_env]
            payload["client_secret"] = os.environ[spec.client_secret_env]

        response = requests.post(
            spec.token_url,
            data=payload,
            headers=headers,
            timeout=20,
        )
        if response.status_code >= 400:
            raise ProviderAuthError(self._provider_error_message(spec, response))

        token_data = response.json()
        expires_in = int(token_data.get("expires_in") or 3600)
        raw_scope = token_data.get("scope", "")
        if isinstance(raw_scope, str):
            scope = [value for value in raw_scope.split(" ") if value]
        else:
            scope = None

        return {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": (_utc_now() + dt.timedelta(seconds=expires_in)).isoformat(),
            "scope": scope,
        }

    def _token_is_fresh(self, connection: Dict[str, Any]) -> bool:
        expires_at = _parse_timestamp(connection.get("expires_at"))
        if expires_at is None:
            return True

        return expires_at > (
            _utc_now() + dt.timedelta(seconds=TOKEN_EXPIRY_SAFETY_SECONDS)
        )

    def _provider_error_message(
        self,
        spec: ProviderSpec,
        response: requests.Response,
    ) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        message = (
            payload.get("error_description")
            or payload.get("error")
            or response.text
            or "Unknown provider error"
        )
        return f"{spec.label} authorization failed: {message}"

    def _get_spec(self, provider_key: str) -> ProviderSpec:
        spec = PROVIDER_SPECS.get(provider_key)
        if spec is None:
            raise ProviderAuthError("Unsupported provider.")
        return spec

    def _prune_pending_states(self) -> None:
        cutoff = _utc_now() - dt.timedelta(seconds=PENDING_STATE_TTL_SECONDS)
        removable = [
            token
            for token, pending in self._pending_states.items()
            if (_parse_timestamp(pending.get("created_at")) or cutoff) < cutoff
        ]
        for token in removable:
            self._pending_states.pop(token, None)

    def _load_connections(self) -> Dict[str, Any]:
        if not self.storage_path.is_file():
            return {"clients": {}}

        try:
            return json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"clients": {}}

    def _save_connections(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.storage_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._connections, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self.storage_path)


provider_auth_manager = ProviderAuthManager()
