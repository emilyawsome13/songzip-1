"""
Web module for the console.
"""

import asyncio
import logging
import os
import socket
import sys
import webbrowser
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn import Config, Server

from spotdl._version import __version__
from spotdl.types.options import DownloaderOptions, WebOptions
from spotdl.utils.config import get_web_ui_path
from spotdl.utils.github import download_github_dir
from spotdl.utils.logging import NAME_TO_LEVEL
from spotdl.utils.web import (
    ALLOWED_ORIGINS,
    SPAStaticFiles,
    app_state,
    fix_mime_types,
    get_current_state,
    router,
)

__all__ = ["web"]

logger = logging.getLogger(__name__)


def _resolve_lan_url(protocol: str, host: str, port: int) -> str:
    """
    Resolve a friendly LAN url for local-network testing.

    ### Arguments
    - protocol: the url protocol
    - host: configured host
    - port: configured port

    ### Returns
    - best-effort LAN url
    """

    if host not in {"0.0.0.0", "::"}:
        return f"{protocol}://{host}:{port}/"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            lan_ip = sock.getsockname()[0]
    except OSError:
        lan_ip = "127.0.0.1"

    return f"{protocol}://{lan_ip}:{port}/"


def web(web_settings: WebOptions, downloader_settings: DownloaderOptions):
    """
    Run the web server.

    ### Arguments
    - web_settings: Web server settings.
    - downloader_settings: Downloader settings.
    """

    # Apply the fix for mime types
    fix_mime_types()

    # Set up the app loggers
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.propagate = False

    spotipy_logger = logging.getLogger("spotipy")
    spotipy_logger.setLevel(logging.NOTSET)

    # Initialize the web server settings
    app_state.web_settings = web_settings
    app_state.logger = uvicorn_logger

    # Create the event loop
    app_state.loop = (
        asyncio.new_event_loop()
        if sys.platform != "win32"
        else asyncio.ProactorEventLoop()  # type: ignore
    )

    downloader_settings["simple_tui"] = True

    bundled_web_app_dir = Path(__file__).resolve().parents[2] / "local-web-ui"

    # Download web app from GitHub if not already downloaded or force flag set
    web_app_dir = get_web_ui_path()
    dist_dir = web_app_dir / "dist"
    if web_settings["web_gui_location"]:
        web_app_dir = Path(web_settings["web_gui_location"]).resolve()
        logger.info("Using custom web app location: %s", web_app_dir)
    elif bundled_web_app_dir.exists():
        web_app_dir = bundled_web_app_dir.resolve()
        logger.info("Using bundled web app: %s", web_app_dir)
    elif (not dist_dir.exists() or web_settings["force_update_gui"]):
        if web_settings["web_gui_repo"] is None:
            gui_repo = "https://github.com/spotdl/web-ui/tree/master/dist"
        else:
            gui_repo = web_settings["web_gui_repo"]

        logger.info("Updating web app from %s", gui_repo)

        download_github_dir(
            gui_repo,
            output_dir=str(web_app_dir),
        )
        web_app_dir = Path(os.path.join(web_app_dir, "dist")).resolve()
    else:
        logger.info(
            "Using cached web app. To update use the `--force-update-gui` flag."
        )
        web_app_dir = Path(os.path.join(web_app_dir, "dist")).resolve()

    app_state.api = FastAPI(
        title="spotDL",
        description="Download music from Spotify",
        version=__version__,
        dependencies=[Depends(get_current_state)],
    )

    app_state.api.include_router(router)

    # Add the CORS middleware
    app_state.api.add_middleware(
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

    # Add the static files
    app_state.api.mount(
        "/",
        SPAStaticFiles(directory=web_app_dir, html=True),
        name="static",
    )
    protocol = "http"
    config = Config(
        app=app_state.api,
        host=web_settings["host"],
        port=web_settings["port"],
        workers=1,
        log_level=NAME_TO_LEVEL[downloader_settings["log_level"]],
        loop=app_state.loop,  # type: ignore
    )
    if web_settings["enable_tls"]:
        logger.info("Enabeling TLS")
        protocol = "https"
        config.ssl_certfile = web_settings["cert_file"]
        config.ssl_keyfile = web_settings["key_file"]
        config.ssl_ca_certs = web_settings["ca_file"]

    app_state.server = Server(config)

    app_state.downloader_settings = downloader_settings

    browser_url = (
        f"{protocol}://127.0.0.1:{web_settings['port']}/"
        if web_settings["host"] in {"0.0.0.0", "::"}
        else f"{protocol}://{web_settings['host']}:{web_settings['port']}/"
    )
    lan_url = _resolve_lan_url(protocol, web_settings["host"], web_settings["port"])

    # Open the web browser
    webbrowser.open(browser_url)

    logger.info("Dashboard URL (this PC): %s", browser_url)
    logger.info("Dashboard URL (phone/tablet): %s", lan_url)

    if not web_settings["web_use_output_dir"]:
        logger.info(
            "Files are stored in temporary directory "
            "and will be deleted after the program exits "
            "to save them to current directory permanently "
            "enable the `web_use_output_dir` option "
        )
    else:
        logger.info(
            "Files are stored in current directory "
            "to save them to temporary directory "
            "disable the `web_use_output_dir` option "
        )

    logger.info("Starting web server \n")

    # Start the web server
    app_state.loop.run_until_complete(app_state.server.serve())
