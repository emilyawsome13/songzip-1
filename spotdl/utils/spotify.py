"""
Module for interacting with Spotify API.
To use this module, you must have a Spotify API key and Spotify API secret.

```python
import spotdl.utils.spotify
spotify.Spotify.init(client_id, client_secret)
```
"""

import json
import logging
import time
from typing import Dict, Optional

import requests
from spotipy import Spotify
from spotipy.cache_handler import CacheFileHandler, MemoryCacheHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

from spotdl.utils.config import get_cache_path, get_spotify_cache_path

__all__ = [
    "SpotifyError",
    "SpotifyClient",
    "save_spotify_cache",
]

logger = logging.getLogger(__name__)

CACHEABLE_URL_FRAGMENTS = (
    "tracks/",
    "albums/",
    "artists/",
    "playlists/",
    "users/",
    '"url": "search',
    '"url": "me/tracks',
    '"url": "me/albums',
    '"url": "me/playlists',
    '"url": "me/following',
)
MAX_RATE_LIMIT_WAIT_SECONDS = 10


class SpotifyError(Exception):
    """
    Base class for all exceptions related to SpotifyClient.
    """


class Singleton(type):
    """
    Singleton metaclass for SpotifyClient. Ensures that SpotifyClient is not
    instantiated without prior initialization. Every other instantiation of
    SpotifyClient will return the same instance.
    """

    _instance = None

    def __call__(self):  # pylint: disable=bad-mcs-method-argument
        """
        Call method for Singleton metaclass.

        ### Returns
        - The instance of the SpotifyClient.
        """

        if self._instance is None:
            raise SpotifyError(
                "Spotify client not created. Call SpotifyClient.init"
                "(client_id, client_secret, user_auth, cache_path, no_cache, open_browser) first."
            )
        return self._instance

    def init(  # pylint: disable=bad-mcs-method-argument
        self,
        client_id: str,
        client_secret: str,
        user_auth: bool = False,
        no_cache: bool = False,
        headless: bool = False,
        max_retries: int = 3,
        use_cache_file: bool = False,
        auth_token: Optional[str] = None,
        cache_path: Optional[str] = None,
    ) -> "Singleton":
        """
        Initializes the SpotifyClient.

        ### Arguments
        - client_id: The client ID of the application.
        - client_secret: The client secret of the application.
        - auth_token: The access token to use.
        - user_auth: Whether or not to use user authentication.
        - cache_path: The path to the cache file.
        - no_cache: Whether or not to use the cache.
        - open_browser: Whether or not to open the browser.

        ### Returns
        - The instance of the SpotifyClient.
        """

        # check if initialization has been completed, if yes, raise an Exception
        if isinstance(self._instance, self):
            raise SpotifyError("A spotify client has already been initialized")

        credential_manager = None

        cache_handler = (
            CacheFileHandler(cache_path or get_cache_path())
            if not no_cache
            else MemoryCacheHandler()
        )
        # Use SpotifyOAuth as auth manager
        if user_auth:
            credential_manager = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri="http://127.0.0.1:9900/",
                scope="user-library-read,user-follow-read,playlist-read-private",
                cache_handler=cache_handler,
                open_browser=not headless,
            )
        # Use SpotifyClientCredentials as auth manager
        else:
            credential_manager = SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                cache_handler=cache_handler,
            )
        if auth_token is not None:
            credential_manager = None

        self.user_auth = user_auth
        self.no_cache = no_cache
        self.max_retries = max_retries
        self.use_cache_file = use_cache_file

        # Create instance
        self._instance = super().__call__(
            auth=auth_token,
            auth_manager=credential_manager,
            retries=0,
            status_retries=0,
            backoff_factor=0,
            status_forcelist=(500, 502, 503, 504, 404),
        )

        # Return instance
        return self._instance


class SpotifyClient(Spotify, metaclass=Singleton):
    """
    This is the Spotify client meant to be used in the app.
    Has to be initialized first by calling
    `SpotifyClient.init(client_id, client_secret, user_auth, cache_path, no_cache, open_browser)`.
    """

    _initialized = False
    cache: Dict[str, Optional[Dict]] = {}

    def __init__(self, *args, **kwargs):
        """
        Initializes the SpotifyClient.

        ### Arguments
        - auth: The access token to use.
        - auth_manager: The auth manager to use.
        """

        super().__init__(*args, **kwargs)
        self._initialized = True

        use_cache_file: bool = self.use_cache_file  # type: ignore # pylint: disable=E1101
        cache_file_loc = get_spotify_cache_path()

        if use_cache_file and cache_file_loc.exists():
            with open(cache_file_loc, "r", encoding="utf-8") as cache_file:
                self.cache = json.load(cache_file)
        elif use_cache_file:
            with open(cache_file_loc, "w", encoding="utf-8") as cache_file:
                json.dump(self.cache, cache_file)

    def _get(self, url, args=None, payload=None, **kwargs):
        """
        Overrides the get method of the SpotifyClient.
        Allows us to cache requests
        """

        use_cache = not self.no_cache  # type: ignore # pylint: disable=E1101

        if args:
            kwargs.update(args)

        cache_key = None
        if use_cache:
            key_obj = dict(kwargs)
            key_obj["url"] = url
            key_obj["data"] = json.dumps(payload)
            cache_key = json.dumps(key_obj)
            if cache_key is None:
                cache_key = url
            if self.cache.get(cache_key) is not None:
                return self.cache[cache_key]

        # Wrap in a try-except and retry up to `retries` times.
        response = None
        retries = self.max_retries  # type: ignore # pylint: disable=E1101
        rate_limit_backoff = 1
        while response is None:
            try:
                response = self._internal_call("GET", url, payload, kwargs)
            except SpotifyException as exc:
                if exc.http_status != 429:
                    raise exc

                if retries <= 0:
                    raise exc

                retries -= 1

                retry_after = exc.headers.get("Retry-After")
                try:
                    wait_seconds = int(retry_after) if retry_after is not None else 0
                except (TypeError, ValueError):
                    wait_seconds = 0

                if wait_seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
                    raise SpotifyError(
                        "Spotify rate limit reached. "
                        f"Retry-After was {wait_seconds} seconds, so spotDL will not block waiting that long. "
                        "Try again later or rely on cached metadata when available."
                    ) from exc

                wait_seconds = max(wait_seconds, rate_limit_backoff)
                logger.warning(
                    "Spotify rate limited request to %s. Waiting %s seconds before retrying.",
                    url,
                    wait_seconds,
                )

                time.sleep(wait_seconds)
                rate_limit_backoff = min(rate_limit_backoff * 2, 60)
            except (requests.exceptions.Timeout, requests.ConnectionError) as exc:
                retries -= 1
                if retries <= 0:
                    raise exc

        if use_cache and cache_key is not None:
            self.cache[cache_key] = response

        return response


def save_spotify_cache(cache: Dict[str, Optional[Dict]]):
    """
    Saves the Spotify cache to a file.

    ### Arguments
    - cache: The cache to save.
    """

    cache_file_loc = get_spotify_cache_path()

    logger.debug("Saving Spotify cache to %s", cache_file_loc)

    # Persist common Spotify metadata responses for reuse across runs.
    cache = {
        key: value
        for key, value in cache.items()
        if value is not None and any(fragment in key for fragment in CACHEABLE_URL_FRAGMENTS)
    }

    with open(cache_file_loc, "w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file)
