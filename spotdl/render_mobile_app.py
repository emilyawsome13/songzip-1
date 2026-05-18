"""
ASGI entrypoint for the mobile-focused web server.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from spotdl._version import __version__
from spotdl.types.options import DownloaderOptions, WebOptions
from spotdl.utils.config import DOWNLOADER_OPTIONS, WEB_OPTIONS, get_spotdl_path
from spotdl.utils.ffmpeg import get_ffmpeg_path
from spotdl.utils.logging import NAME_TO_LEVEL
from spotdl.utils.web import (
    ALLOWED_ORIGINS,
    SPAStaticFiles,
    _normalize_web_audio_providers,
    _normalize_web_output_template,
    app_state,
    ensure_spotify_client_initialized,
    fix_mime_types,
    get_current_state,
    router,
)

__all__ = ["app", "create_app"]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def _env_list(name: str) -> Optional[List[str]]:
    value = os.environ.get(name)
    if value is None:
        return None

    parsed = [item.strip() for item in value.split(",") if item.strip()]
    return parsed or None


def _build_web_settings() -> WebOptions:
    web_settings = dict(WEB_OPTIONS)
    web_settings.update(
        {
            "host": os.environ.get("SPOTDL_HOST", "0.0.0.0"),
            "port": _env_int("PORT", _env_int("SPOTDL_PORT", 8801)),
            "keep_alive": True,
            "keep_sessions": True,
            "bundle_flatten": _env_bool("SPOTDL_BUNDLE_FLATTEN", True),
            "bundle_compression": os.environ.get("SPOTDL_BUNDLE_COMPRESSION", "store"),
            "forced_format": os.environ.get("SPOTDL_FORCED_FORMAT", "mp3"),
            "forced_output": os.environ.get(
                "SPOTDL_FORCED_OUTPUT", "{artist} - {title}.{output-ext}"
            ),
            "web_use_output_dir": _env_bool("SPOTDL_WEB_USE_OUTPUT_DIR", False),
            "allowed_origins": _env_list("SPOTDL_ALLOWED_ORIGINS"),
            "web_gui_location": str(
                (Path(__file__).resolve().parents[1] / "local-web-ui").resolve()
            ),
        }
    )
    return WebOptions(**web_settings)


def _build_output_template(web_settings: WebOptions) -> str:
    template = _normalize_web_output_template(
        os.environ.get(
            "SPOTDL_OUTPUT_TEMPLATE",
            "{artist} - {title}.{output-ext}",
        )
    )

    if not web_settings["web_use_output_dir"]:
        return template

    output_root = Path(
        os.environ.get(
            "SPOTDL_OUTPUT_ROOT",
            str((get_spotdl_path() / "mobile-downloads").absolute()),
        )
    )
    output_root.mkdir(parents=True, exist_ok=True)

    if Path(template).is_absolute():
        return template

    return str((output_root / template).absolute())


def _build_downloader_settings(web_settings: WebOptions) -> DownloaderOptions:
    downloader_settings = dict(DOWNLOADER_OPTIONS)
    ffmpeg_path = get_ffmpeg_path()
    log_level = os.environ.get("SPOTDL_LOG_LEVEL", "INFO").upper()
    if log_level not in NAME_TO_LEVEL:
        log_level = "INFO"

    downloader_settings.update(
        {
            "audio_providers": _normalize_web_audio_providers(
                _env_list("SPOTDL_AUDIO_PROVIDERS") or ["youtube-music"]
            ),
            "output": web_settings["forced_output"] or _build_output_template(web_settings),
            "bitrate": os.environ.get("SPOTDL_BITRATE", "192k"),
            "format": web_settings["forced_format"] or os.environ.get("SPOTDL_FORMAT", "mp3"),
            "overwrite": os.environ.get("SPOTDL_OVERWRITE", "skip"),
            "ffmpeg": (
                os.environ.get("SPOTDL_FFMPEG_PATH")
                or (str(ffmpeg_path) if ffmpeg_path else "ffmpeg")
            ),
            "threads": _env_int("SPOTDL_THREADS", 3),
            "yt_dlp_args": os.environ.get(
                "SPOTDL_YT_DLP_ARGS",
                "--js-runtimes node --remote-components ejs:github --concurrent-fragments 2",
            ),
            "simple_tui": True,
            "log_level": log_level,
        }
    )

    return DownloaderOptions(**downloader_settings)


def create_app() -> FastAPI:
    fix_mime_types()

    web_settings = _build_web_settings()
    downloader_settings = _build_downloader_settings(web_settings)

    app = FastAPI(
        title="SongZip Mobile Server",
        description="Mobile-focused SongZip web app with flat ZIP bundles",
        version=__version__,
        dependencies=[Depends(get_current_state)],
    )

    app_state.api = app
    app_state.web_settings = web_settings
    app_state.downloader_settings = downloader_settings
    app_state.clients = {}
    app_state.logger = logging.getLogger("uvicorn.error")

    @app.on_event("startup")
    async def _startup() -> None:
        app_state.loop = asyncio.get_running_loop()
        app_state.logger = logging.getLogger("uvicorn.error")
        ensure_spotify_client_initialized(app_state.logger)

    app.include_router(router)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=(
            ALLOWED_ORIGINS + web_settings["allowed_origins"]
            if web_settings["allowed_origins"]
            else ALLOWED_ORIGINS
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    web_app_dir = Path(web_settings["web_gui_location"]).resolve()
    app.mount(
        "/",
        SPAStaticFiles(directory=web_app_dir, html=True),
        name="static",
    )

    return app


app = create_app()
