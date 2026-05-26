"""
Remote SongZip store bridge for using a PC-hosted database from Render.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from spotdl.utils.songzip_store import SongZipStore, SongZipStoreError

REMOTE_STORE_METHODS = {
    "authenticate_account",
    "claim_admin_account",
    "create_session",
    "delete_client_snapshot",
    "delete_session",
    "get_account_by_identifier",
    "get_account_by_session",
    "get_admin_account_key",
    "get_meta",
    "get_or_create_google_account",
    "grant_bonus_credits",
    "list_subscription_usage_events",
    "load_account_settings",
    "load_client_snapshot",
    "load_latest_paypal_subscription_for_account",
    "load_paypal_subscription",
    "load_subscription",
    "migrate_account_settings",
    "record_subscription_usage_event",
    "register_account",
    "save_account_settings",
    "save_client_snapshot",
    "save_paypal_subscription",
    "save_subscription",
    "set_account_membership",
    "set_meta",
}


class StoreRpcRequest(BaseModel):
    method: str
    args: list[Any] = Field(default_factory=list)
    kwargs: Dict[str, Any] = Field(default_factory=dict)


class RemoteSongZipStore:
    """
    Proxy SongZip store calls to a PC-hosted bridge service.
    """

    def __init__(
        self,
        base_url: str,
        shared_secret: str,
        timeout_seconds: float = 10.0,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.shared_secret = str(shared_secret or "").strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds or 10.0))
        if not self.base_url:
            raise SongZipStoreError("SongZip remote store URL is required.")
        if not self.shared_secret:
            raise SongZipStoreError("SongZip remote store shared secret is required.")

    def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in REMOTE_STORE_METHODS:
            raise AttributeError(f"Unsupported SongZip store method: {method}")

        response = requests.post(
            f"{self.base_url}/rpc",
            headers={"X-SongZip-Store-Secret": self.shared_secret},
            json={
                "method": method,
                "args": list(args),
                "kwargs": kwargs,
            },
            timeout=self.timeout_seconds,
        )

        try:
            payload = response.json()
        except ValueError as error:
            raise SongZipStoreError(
                "SongZip remote store returned an invalid response."
            ) from error

        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise SongZipStoreError(
                str(detail or "SongZip remote store request failed.")
            )

        return payload.get("result") if isinstance(payload, dict) else None

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in REMOTE_STORE_METHODS:
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            return self._rpc(name, *args, **kwargs)

        return _call


def create_songzip_store_bridge_app(
    store: Optional[SongZipStore] = None,
    shared_secret: Optional[str] = None,
) -> FastAPI:
    """
    Build the FastAPI app that exposes the local SongZip SQLite store over HTTP.
    """

    resolved_store = store or SongZipStore()
    resolved_secret = str(
        shared_secret
        or os.environ.get("SONGZIP_REMOTE_STORE_SHARED_SECRET", "")
    ).strip()
    if not resolved_secret:
        raise RuntimeError(
            "SONGZIP_REMOTE_STORE_SHARED_SECRET must be set to start the SongZip store bridge."
        )

    app = FastAPI(title="SongZip Store Bridge")

    def _assert_secret(header_value: Optional[str]) -> None:
        if str(header_value or "").strip() != resolved_secret:
            raise HTTPException(status_code=401, detail="SongZip store secret is invalid.")

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "database_path": str(resolved_store.database_path),
        }

    @app.post("/rpc")
    def rpc(
        payload: StoreRpcRequest,
        x_songzip_store_secret: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        _assert_secret(x_songzip_store_secret)

        method_name = str(payload.method or "").strip()
        if method_name not in REMOTE_STORE_METHODS:
            raise HTTPException(status_code=404, detail="SongZip store method is not allowed.")

        method = getattr(resolved_store, method_name, None)
        if method is None:
            raise HTTPException(status_code=404, detail="SongZip store method is unavailable.")

        try:
            result = method(*payload.args, **payload.kwargs)
        except SongZipStoreError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        return {"ok": True, "result": result}

    return app
