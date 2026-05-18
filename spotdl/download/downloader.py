"""
Downloader module, this is where all the downloading pre/post processing happens etc.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlparse
from argparse import Namespace
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type, Union

from yt_dlp.postprocessor.modify_chapters import ModifyChaptersPP
from yt_dlp.postprocessor.sponsorblock import SponsorBlockPP

from spotdl.download.progress_handler import ProgressHandler
from spotdl.providers.audio import (
    AudioProvider,
    AudioProviderError,
    BandCamp,
    Piped,
    SoundCloud,
    YouTube,
    YouTubeMusic,
)
from spotdl.providers.lyrics import AzLyrics, Genius, LyricsProvider, MusixMatch, Synced
from spotdl.types.options import DownloaderOptionalOptions, DownloaderOptions
from spotdl.types.song import Song
from spotdl.utils.archive import Archive
from spotdl.utils.config import (
    DOWNLOADER_OPTIONS,
    GlobalConfig,
    create_settings_type,
    get_errors_path,
    get_temp_path,
    modernize_settings,
)
from spotdl.utils.ffmpeg import FFmpegError, convert, get_ffmpeg_path
from spotdl.utils.formatter import create_file_name
from spotdl.utils.lrc import generate_lrc
from spotdl.utils.m3u import gen_m3u_files
from spotdl.utils.metadata import MetadataError, embed_metadata
from spotdl.utils.search import (
    gather_known_songs,
    get_ytm_client,
    reinit_song,
    songs_from_albums,
)

__all__ = [
    "AUDIO_PROVIDERS",
    "LYRICS_PROVIDERS",
    "Downloader",
    "DownloaderError",
    "SPONSOR_BLOCK_CATEGORIES",
]

AUDIO_PROVIDERS: Dict[str, Type[AudioProvider]] = {
    "youtube": YouTube,
    "youtube-music": YouTubeMusic,
    "soundcloud": SoundCloud,
    "bandcamp": BandCamp,
    "piped": Piped,
}

LYRICS_PROVIDERS: Dict[str, Type[LyricsProvider]] = {
    "genius": Genius,
    "musixmatch": MusixMatch,
    "azlyrics": AzLyrics,
    "synced": Synced,
}

SPONSOR_BLOCK_CATEGORIES = {
    "sponsor": "Sponsor",
    "intro": "Intermission/Intro Animation",
    "outro": "Endcards/Credits",
    "selfpromo": "Unpaid/Self Promotion",
    "preview": "Preview/Recap",
    "filler": "Filler Tangent",
    "interaction": "Interaction Reminder",
    "music_offtopic": "Non-Music Section",
}


logger = logging.getLogger(__name__)


class DownloaderError(Exception):
    """
    Base class for all exceptions related to downloaders.
    """


class Downloader:
    """
    Downloader class, this is where all the downloading pre/post processing happens etc.
    It handles the downloading/moving songs, multithreading, metadata embedding etc.
    """

    def __init__(
        self,
        settings: Optional[Union[DownloaderOptionalOptions, DownloaderOptions]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """
        Initialize the Downloader class.

        ### Arguments
        - settings: The settings to use.
        - loop: The event loop to use.

        ### Notes
        - `search-query` uses the same format as `output`.
        - if `audio_provider` or `lyrics_provider` is a list, then if no match is found,
            the next provider in the list will be used.
        """

        if settings is None:
            settings = {}

        # Create settings dictionary, fill in missing values with defaults
        # from spotdl.types.options.DOWNLOADER_OPTIONS
        self.settings: DownloaderOptions = DownloaderOptions(
            **create_settings_type(
                Namespace(config=False), dict(settings), DOWNLOADER_OPTIONS
            )  # type: ignore
        )

        # Handle deprecated values in config file
        modernize_settings(self.settings)
        logger.debug("Downloader settings: %s", self.settings)

        # If no audio providers specified, raise an error
        if len(self.settings["audio_providers"]) == 0:
            raise DownloaderError(
                "No audio providers specified. Please specify at least one."
            )

        # If ffmpeg is the default value and it's not installed
        # try to use the spotdl's ffmpeg
        self.ffmpeg = self.settings["ffmpeg"]
        if self.ffmpeg == "ffmpeg" and shutil.which("ffmpeg") is None:
            ffmpeg_exec = get_ffmpeg_path()
            if ffmpeg_exec is None:
                raise DownloaderError("ffmpeg is not installed")

            self.ffmpeg = str(ffmpeg_exec.absolute())

        logger.debug("FFmpeg path: %s", self.ffmpeg)

        self.loop = loop or (
            asyncio.new_event_loop()
            if sys.platform != "win32"
            else asyncio.ProactorEventLoop()  # type: ignore
        )

        if loop is None:
            asyncio.set_event_loop(self.loop)

        # semaphore is required to limit concurrent asyncio executions
        self.semaphore = asyncio.Semaphore(self.settings["threads"])

        self.progress_handler = ProgressHandler(self.settings["simple_tui"])

        # Gather already present songs
        self.scan_formats = self.settings["detect_formats"] or [self.settings["format"]]
        self.known_songs: Dict[str, List[Path]] = {}
        if self.settings["scan_for_songs"]:
            logger.info("Scanning for known songs, this might take a while...")
            for scan_format in self.scan_formats:
                logger.debug("Scanning for %s files", scan_format)

                found_files = gather_known_songs(self.settings["output"], scan_format)

                logger.debug("Found %s %s files", len(found_files), scan_format)

                for song_url, song_paths in found_files.items():
                    known_paths = self.known_songs.get(song_url)
                    if known_paths is None:
                        self.known_songs[song_url] = song_paths
                    else:
                        self.known_songs[song_url].extend(song_paths)

        logger.debug("Found %s known songs", len(self.known_songs))

        # Initialize lyrics providers
        self.lyrics_providers: List[LyricsProvider] = []
        for lyrics_provider in self.settings["lyrics_providers"]:
            lyrics_class = LYRICS_PROVIDERS.get(lyrics_provider)
            if lyrics_class is None:
                raise DownloaderError(f"Invalid lyrics provider: {lyrics_provider}")
            if lyrics_provider == "genius":
                access_token = self.settings.get("genius_token")
                if not access_token:
                    raise DownloaderError("Genius token not found in settings")
                self.lyrics_providers.append(Genius(access_token))
            else:
                self.lyrics_providers.append(lyrics_class())

        # Initialize audio providers
        self.audio_providers: List[AudioProvider] = []
        for audio_provider in self.settings["audio_providers"]:
            audio_class = AUDIO_PROVIDERS.get(audio_provider)
            if audio_class is None:
                raise DownloaderError(f"Invalid audio provider: {audio_provider}")

            self.audio_providers.append(
                audio_class(
                    output_format=self.settings["format"],
                    cookie_file=self.settings["cookie_file"],
                    search_query=self.settings["search_query"],
                    filter_results=self.settings["filter_results"],
                    yt_dlp_args=self.settings["yt_dlp_args"],
                )
            )

        # Initialize list of errors
        self.errors: List[str] = []
        self.youtube_rate_limit_message: Optional[str] = None
        self.download_request_lock = threading.Lock()
        try:
            self.download_request_interval_seconds = max(
                0.0,
                float(os.environ.get("SPOTDL_DOWNLOAD_GAP_SECONDS", "12")),
            )
        except ValueError:
            self.download_request_interval_seconds = 12.0
        self.last_download_request_at: Optional[float] = None

        # Initialize proxy server
        proxy = self.settings["proxy"]
        proxies = None
        if proxy:
            if not re.match(
                pattern=r"^(http|https):\/\/(?:(\w+)(?::(\w+))?@)?((?:\d{1,3})(?:\.\d{1,3}){3})(?::(\d{1,5}))?$",  # pylint: disable=C0301
                string=proxy,
            ):
                raise DownloaderError(f"Invalid proxy server: {proxy}")
            proxies = {"http": proxy, "https": proxy}
            logger.info("Setting proxy server: %s", proxy)

        GlobalConfig.set_parameter("proxies", proxies)

        # Initialize archive
        self.url_archive = Archive()
        if self.settings["archive"]:
            self.url_archive.load(self.settings["archive"])

        logger.debug("Archive: %d urls", len(self.url_archive))

        logger.debug("Downloader initialized")

    @staticmethod
    def _is_youtube_rate_limit_error(message: str) -> bool:
        """
        Check whether an error message indicates a YouTube request cooldown.

        ### Arguments
        - message: error message text

        ### Returns
        - whether the message indicates YouTube has rate-limited the session
        """

        lowered_message = message.casefold()
        return (
            "rate-limited by youtube" in lowered_message
            or "this content isn't available, try again later" in lowered_message
        )

    def download_song(self, song: Song) -> Tuple[Song, Optional[Path]]:
        """
        Download a single song.

        ### Arguments
        - song: The song to download.

        ### Returns
        - tuple with the song and the path to the downloaded file if successful.
        """

        self.progress_handler.set_song_count(1)

        results = self.download_multiple_songs([song])

        return results[0]

    def download_multiple_songs(
        self, songs: List[Song]
    ) -> List[Tuple[Song, Optional[Path]]]:
        """
        Download multiple songs to the temp directory.

        ### Arguments
        - songs: The songs to download.

        ### Returns
        - list of tuples with the song and the path to the downloaded file if successful.
        """

        if self.settings["fetch_albums"]:
            albums = set(song.album_id for song in songs if song.album_id is not None)
            logger.info(
                "Fetching %d album%s", len(albums), "s" if len(albums) > 1 else ""
            )

            songs.extend(songs_from_albums(list(albums)))

            # Remove duplicates
            return_obj = {}
            for song in songs:
                return_obj[song.url] = song

            songs = list(return_obj.values())

        logger.debug("Downloading %d songs", len(songs))

        if self.settings["archive"]:
            songs = [song for song in songs if song.url not in self.url_archive]
            logger.debug("Filtered %d songs with archive", len(songs))

        self.progress_handler.set_song_count(len(songs))

        # Create tasks list
        tasks = [self.pool_download(song) for song in songs]

        # Call all task asynchronously, and wait until all are finished
        results = list(self.loop.run_until_complete(asyncio.gather(*tasks)))

        # Print errors
        if self.settings["print_errors"]:
            for error in self.errors:
                logger.error(error)

        if self.youtube_rate_limit_message is not None:
            logger.error(
                "YouTube cooldown detected for this run. "
                "Wait for the session cooldown to expire, then rerun the queue."
            )

        if self.settings["save_errors"]:
            with open(
                self.settings["save_errors"], "a", encoding="utf-8"
            ) as error_file:
                if len(self.errors) > 0:
                    error_file.write(
                        f"{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}\n"
                    )
                for error in self.errors:
                    error_file.write(f"{error}\n")

            logger.info("Saved errors to %s", self.settings["save_errors"])

        # Save archive
        if self.settings["archive"]:
            for result in results:
                if result[1] or self.settings["add_unavailable"]:
                    self.url_archive.add(result[0].url)

            self.url_archive.save(self.settings["archive"])
            logger.info(
                "Saved archive with %d urls to %s",
                len(self.url_archive),
                self.settings["archive"],
            )

        # Create m3u playlist
        if self.settings["m3u"]:
            song_list = [
                song
                for song, path in results
                if path or self.settings["add_unavailable"]
            ]

            gen_m3u_files(
                song_list,
                self.settings["m3u"],
                self.settings["output"],
                self.settings["format"],
                self.settings["restrict"],
                False,
                self.settings["detect_formats"],
            )

        # Save results to a file
        if self.settings["save_file"]:
            with open(self.settings["save_file"], "w", encoding="utf-8") as save_file:
                json.dump([song.json for song, _ in results], save_file, indent=4)

            logger.info("Saved results to %s", self.settings["save_file"])

        return results

    async def pool_download(self, song: Song) -> Tuple[Song, Optional[Path]]:
        """
        Run asynchronous task in a pool to make sure that all processes.

        ### Arguments
        - song: The song to download.

        ### Returns
        - tuple with the song and the path to the downloaded file if successful.

        ### Notes
        - This method calls `self.search_and_download` in a new thread.
        """

        # tasks that cannot acquire semaphore will wait here until it's free
        # only certain amount of tasks can acquire the semaphore at the same time
        async with self.semaphore:
            return await self.loop.run_in_executor(None, self.search_and_download, song)

    def search(self, song: Song) -> str:
        """
        Search for a song using all available providers.

        ### Arguments
        - song: The song to search for.

        ### Returns
        - tuple with download url and audio provider if successful.
        """

        for audio_provider in self.audio_providers:
            url = audio_provider.search(song, self.settings["only_verified_results"])
            if url:
                return url

            logger.debug("%s failed to find %s", audio_provider.name, song.display_name)

        raise LookupError(f"No results found for song: {song.display_name}")

    def search_all(self, song: Song) -> List[str]:
        """
        Search for a song using all available providers and return unique candidates.

        ### Arguments
        - song: The song to search for.

        ### Returns
        - list of candidate download URLs in provider order
        """

        results: List[str] = []
        provider_candidates: List[List[str]] = []

        ordered_audio_providers = list(self.audio_providers)
        if song.download_url and (
            "youtube.com" in song.download_url or "youtu.be" in song.download_url
        ):
            ordered_audio_providers = sorted(
                ordered_audio_providers,
                key=lambda provider: provider.name in ("YouTubeMusic", "YouTube"),
            )

        for audio_provider in ordered_audio_providers:
            try:
                candidate_urls = audio_provider.search_candidates(
                    song,
                    self.settings["only_verified_results"],
                    limit=4,
                )
            except Exception as exc:  # pragma: no cover - defensive provider fallback
                logger.debug(
                    "%s failed to search %s: %s",
                    audio_provider.name,
                    song.display_name,
                    exc,
                )
                continue

            if not candidate_urls:
                logger.debug("%s failed to find %s", audio_provider.name, song.display_name)
                continue

            provider_candidates.append(candidate_urls)

        for candidate_index in range(
            max((len(candidate_urls) for candidate_urls in provider_candidates), default=0)
        ):
            for candidate_urls in provider_candidates:
                if candidate_index >= len(candidate_urls):
                    continue

                url = candidate_urls[candidate_index]
                if url not in results:
                    results.append(url)

        if not results:
            raise LookupError(f"No results found for song: {song.display_name}")

        return results

    @staticmethod
    def _is_youtube_download_url(url: Optional[str]) -> bool:
        """
        Check whether a download URL points at a YouTube host.

        ### Arguments
        - url: candidate download URL

        ### Returns
        - whether the URL points at YouTube
        """

        if not url:
            return False

        host = urlparse(url).netloc.lower()
        return host.endswith("youtube.com") or host.endswith("youtu.be")

    def _target_download_interval_seconds(self, url: Optional[str]) -> float:
        """
        Resolve the delay to apply before the next yt-dlp request.

        ### Arguments
        - url: candidate download URL

        ### Returns
        - recommended delay in seconds
        """

        interval = max(0.0, self.download_request_interval_seconds)
        if not self._is_youtube_download_url(url):
            return interval

        try:
            youtube_interval = max(
                0.0,
                float(os.environ.get("SPOTDL_YOUTUBE_DOWNLOAD_GAP_SECONDS", "5")),
            )
        except ValueError:
            youtube_interval = 5.0

        return max(interval, youtube_interval)

    @staticmethod
    def _is_hosted_render_environment() -> bool:
        """
        Check whether SongZip is running in a hosted Render-style environment.

        ### Returns
        - whether hosted environment heuristics are active
        """

        return any(
            bool(os.environ.get(name))
            for name in ("RENDER", "RENDER_EXTERNAL_HOSTNAME", "RENDER_SERVICE_ID")
        )

    @staticmethod
    def _direct_youtube_search_first_enabled() -> bool:
        """
        Check whether hosted direct YouTube links should search for matched sources first.

        ### Returns
        - whether the search-first behavior is enabled
        """

        value = os.environ.get("SONGZIP_DIRECT_YOUTUBE_SEARCH_FIRST")
        if value is not None:
            return value.strip().lower() in {"1", "true", "yes", "on"}

        return Downloader._is_hosted_render_environment()

    def _should_search_before_direct_download(self, song: Song) -> bool:
        """
        Decide whether provider search results should be tried before a direct URL.

        ### Arguments
        - song: song being downloaded

        ### Returns
        - whether SongZip should prefer metadata-matched sources first
        """

        if not self._direct_youtube_search_first_enabled():
            return False

        if str(getattr(song, "source_hint", "") or "").strip().lower() != "direct_youtube_video":
            return False

        if not self._is_youtube_download_url(song.download_url):
            return False

        if not song.name or not (song.artists or song.artist):
            return False

        return len(self._youtube_alternate_watch_urls(song.download_url)) > 0

    def _wait_for_next_download_attempt(self, url: Optional[str] = None):
        """
        Add a small gap between yt-dlp download attempts to reduce YouTube bot checks.
        """

        with self.download_request_lock:
            now = time.monotonic()
            target_interval = self._target_download_interval_seconds(url)
            if self.last_download_request_at is not None:
                elapsed = now - self.last_download_request_at
                wait_seconds = target_interval - elapsed
                if wait_seconds > 0:
                    logger.debug(
                        "Sleeping %.1f seconds before next yt-dlp request",
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)

            self.last_download_request_at = time.monotonic()

    def _direct_youtube_metadata_candidate_urls(self, song: Song) -> List[str]:
        """
        Search YouTube Music directly for candidate song URLs using extracted metadata.

        ### Arguments
        - song: song being downloaded

        ### Returns
        - ordered candidate URLs
        """

        title = (song.name or "").strip()
        artists = [artist.strip() for artist in (song.artists or []) if artist and artist.strip()]
        fallback_artist = (song.artist or "").strip()
        if not artists and fallback_artist:
            artists = [fallback_artist]

        if not title or not artists:
            return []

        query = f"{', '.join(artists)} - {title}"
        candidate_urls: List[str] = []

        try:
            client = get_ytm_client()
            for result_filter in ("songs", "videos"):
                results = client.search(query, filter=result_filter, limit=6) or []
                for result in results:
                    video_id = result.get("videoId")
                    if not video_id:
                        continue

                    if result_filter == "songs":
                        candidate_url = f"https://music.youtube.com/watch?v={video_id}"
                    else:
                        candidate_url = f"https://www.youtube.com/watch?v={video_id}"

                    if candidate_url not in candidate_urls:
                        candidate_urls.append(candidate_url)
        except Exception as exc:  # pragma: no cover - defensive hosted fallback
            logger.debug(
                "Direct YouTube metadata candidate search failed for %s: %s",
                song.display_name,
                exc,
            )

        return candidate_urls

    @staticmethod
    def _youtube_alternate_watch_urls(url: Optional[str]) -> List[str]:
        """
        Build alternate YouTube watch URLs for a source URL.

        ### Arguments
        - url: the source url

        ### Returns
        - alternate watch URLs in retry order
        """

        if not url:
            return []

        parsed_url = urlparse(url)
        host = parsed_url.netloc.lower()
        video_id: Optional[str] = None

        if host.endswith("youtu.be"):
            video_id = parsed_url.path.lstrip("/").split("/", 1)[0] or None
        elif parsed_url.path == "/watch":
            video_id = parse_qs(parsed_url.query).get("v", [None])[0]

        if not video_id:
            return []

        alternates: List[str] = []
        youtube_watch_url = f"https://www.youtube.com/watch?v={video_id}"
        music_watch_url = f"https://music.youtube.com/watch?v={video_id}"

        if host in ("music.youtube.com", "www.music.youtube.com"):
            alternates.append(youtube_watch_url)
        elif host.endswith("youtube.com") or host.endswith("youtu.be"):
            alternates.append(music_watch_url)
            if not host.endswith("youtube.com"):
                alternates.append(youtube_watch_url)

        return alternates

    def _build_audio_downloader(self) -> Union[AudioProvider, Piped]:
        """
        Build the audio downloader for the current provider configuration.

        ### Returns
        - initialized downloader instance
        """

        if self.settings["audio_providers"][0] == "piped":
            return Piped(
                output_format=self.settings["format"],
                cookie_file=self.settings["cookie_file"],
                search_query=self.settings["search_query"],
                filter_results=self.settings["filter_results"],
                yt_dlp_args=self.settings["yt_dlp_args"],
            )

        return AudioProvider(
            output_format=self.settings["format"],
            cookie_file=self.settings["cookie_file"],
            search_query=self.settings["search_query"],
            filter_results=self.settings["filter_results"],
            yt_dlp_args=self.settings["yt_dlp_args"],
        )

    def search_lyrics(self, song: Song) -> Optional[str]:
        """
        Search for lyrics using all available providers.

        ### Arguments
        - song: The song to search for.

        ### Returns
        - lyrics if successful else None.
        """

        for lyrics_provider in self.lyrics_providers:
            lyrics = lyrics_provider.get_lyrics(song.name, song.artists)
            if lyrics:
                logger.debug(
                    "Found lyrics for %s on %s", song.display_name, lyrics_provider.name
                )

                return lyrics

            logger.debug(
                "%s failed to find lyrics for %s",
                lyrics_provider.name,
                song.display_name,
            )

        return None

    def search_and_download(  # pylint: disable=R0911
        self, song: Song
    ) -> Tuple[Song, Optional[Path]]:
        """
        Search for the song and download it.

        ### Arguments
        - song: The song to download.

        ### Returns
        - tuple with the song and the path to the downloaded file if successful.

        ### Notes
        - This function is synchronous.
        """

        # Check if song has name/artist and url/song_id
        if not (song.name and (song.artists or song.artist)) and not (
            song.url or song.song_id
        ):
            logger.error("Song is missing required fields: %s", song.display_name)
            self.errors.append(f"Song is missing required fields: {song.display_name}")
            return song, None

        # Reinitialize the song object if it's missing metadata
        # Or if we are fetching albums
        if (
            (song.name is None and song.url)
            or self.settings["fetch_albums"]
            or any(
                x is None
                for x in [
                    song.genres,
                    song.disc_count,
                    song.tracks_count,
                    song.track_number,
                    song.album_id,
                    song.album_artist,
                ]
            )
        ):
            song = reinit_song(song)

        # Create the output file path
        output_file = create_file_name(
            song=song,
            template=self.settings["output"],
            file_extension=self.settings["format"],
            restrict=self.settings["restrict"],
            file_name_length=self.settings["max_filename_length"],
        )

        if song.explicit is True and self.settings["skip_explicit"] is True:
            logger.info("Skipping explicit song: %s", song.display_name)
            return song, None

        # Initialize the progress tracker
        display_progress_tracker = self.progress_handler.get_new_tracker(song)

        if self.youtube_rate_limit_message is not None:
            logger.warning(
                "Skipping %s because a YouTube cooldown was already detected for this run.",
                song.display_name,
            )
            display_progress_tracker.notify_download_skip("Rate limited")
            return song, None

        try:
            # Create the temp folder path
            temp_folder = get_temp_path()

            # Check if there is an already existing song file, with the same spotify URL in its
            # metadata, but saved under a different name. If so, save its path.
            dup_song_paths: List[Path] = self.known_songs.get(song.url, [])

            # Remove files from the list that have the same path as the output file
            dup_song_paths = [
                dup_song_path
                for dup_song_path in dup_song_paths
                if (dup_song_path.absolute() != output_file.absolute())
                and dup_song_path.exists()
            ]

            # Checking if file already exists in all subfolders of output directory
            file_exists = output_file.exists() or dup_song_paths
            if not self.settings["scan_for_songs"]:
                for file_extension in self.scan_formats:
                    ext_path = output_file.with_suffix(f".{file_extension}")
                    if ext_path.exists():
                        dup_song_paths.append(ext_path)

            if dup_song_paths:
                logger.debug(
                    "Found duplicate songs for %s at %s",
                    song.display_name,
                    ", ".join(
                        [f"'{str(dup_song_path)}'" for dup_song_path in dup_song_paths]
                    ),
                )

            # If the file already exists and we don't want to overwrite it,
            # we can skip the download
            if (  # pylint: disable=R1705
                Path(str(output_file.absolute()) + ".skip").exists()
                and self.settings["respect_skip_file"]
            ):
                logger.info(
                    "Skipping %s (skip file found) %s",
                    song.display_name,
                    "",
                )

                existing_path = output_file if output_file.exists() else None
                if existing_path is None and len(dup_song_paths) > 0:
                    existing_path = next(
                        (dup_song_path for dup_song_path in dup_song_paths if dup_song_path.exists()),
                        None,
                    )

                return song, existing_path

            elif file_exists and self.settings["overwrite"] == "skip":
                logger.info(
                    "Skipping %s (file already exists) %s",
                    song.display_name,
                    "(duplicate)" if dup_song_paths else "",
                )

                display_progress_tracker.notify_download_skip()
                existing_path = output_file if output_file.exists() else None
                if existing_path is None and len(dup_song_paths) > 0:
                    existing_path = next(
                        (dup_song_path for dup_song_path in dup_song_paths if dup_song_path.exists()),
                        None,
                    )

                return song, existing_path

            # Don't skip if the file exists and overwrite is set to force
            if file_exists and self.settings["overwrite"] == "force":
                logger.info(
                    "Overwriting %s %s",
                    song.display_name,
                    " (duplicate)" if dup_song_paths else "",
                )

                # If the duplicate song path is not None, we can delete the old file
                for dup_song_path in dup_song_paths:
                    try:
                        logger.info("Removing duplicate file: %s", dup_song_path)

                        dup_song_path.unlink()
                    except (PermissionError, OSError, Exception) as exc:
                        logger.debug(
                            "Could not remove duplicate file: %s, error: %s",
                            dup_song_path,
                            exc,
                        )

            # Find song lyrics and add them to the song object
            try:
                lyrics = self.search_lyrics(song)
                if lyrics is None:
                    logger.debug(
                        "No lyrics found for %s, lyrics providers: %s",
                        song.display_name,
                        ", ".join(
                            [lprovider.name for lprovider in self.lyrics_providers]
                        ),
                    )
                else:
                    song.lyrics = lyrics
            except Exception as exc:
                logger.debug("Could not search for lyrics: %s", exc)

            # If the file already exists and we want to overwrite the metadata,
            # we can skip the download
            if file_exists and self.settings["overwrite"] == "metadata":
                most_recent_duplicate: Optional[Path] = None
                if dup_song_paths:
                    # Get the most recent duplicate song path and remove the rest
                    most_recent_duplicate = max(
                        dup_song_paths,
                        key=lambda dup_song_path: dup_song_path.stat().st_mtime
                        and dup_song_path.suffix == output_file.suffix,
                    )

                    # Remove the rest of the duplicate song paths
                    for old_song_path in dup_song_paths:
                        if most_recent_duplicate == old_song_path:
                            continue

                        try:
                            logger.info("Removing duplicate file: %s", old_song_path)
                            old_song_path.unlink()
                        except (PermissionError, OSError) as exc:
                            logger.debug(
                                "Could not remove duplicate file: %s, error: %s",
                                old_song_path,
                                exc,
                            )

                    # Move the old file to the new location
                    if (
                        most_recent_duplicate
                        and most_recent_duplicate.suffix == output_file.suffix
                    ):
                        most_recent_duplicate.replace(
                            output_file.with_suffix(f".{self.settings['format']}")
                        )

                if (
                    most_recent_duplicate
                    and most_recent_duplicate.suffix != output_file.suffix
                ):
                    logger.info(
                        "Could not move duplicate file: %s, different file extension",
                        most_recent_duplicate,
                    )

                    display_progress_tracker.notify_complete()

                    return song, None

                # Update the metadata
                embed_metadata(
                    output_file=output_file,
                    song=song,
                    skip_album_art=self.settings["skip_album_art"],
                )

                logger.info(
                    f"Updated metadata for {song.display_name}"
                    f", moved to new location: {output_file}"
                    if most_recent_duplicate
                    else ""
                )

                display_progress_tracker.notify_complete()

                return song, output_file

            # Create the output directory if it doesn't exist
            output_file.parent.mkdir(parents=True, exist_ok=True)
            pending_download_urls: List[str] = []
            attempted_download_urls = set()
            last_download_error: Optional[Exception] = None
            searched_for_fallback = False
            prefer_search_before_direct = self._should_search_before_direct_download(song)

            def enqueue_download_url(candidate_url: Optional[str]):
                if (
                    candidate_url
                    and candidate_url not in attempted_download_urls
                    and candidate_url not in pending_download_urls
                ):
                    pending_download_urls.append(candidate_url)

            def enqueue_alternate_watch_urls(candidate_url: Optional[str]):
                for alternate_url in self._youtube_alternate_watch_urls(candidate_url):
                    enqueue_download_url(alternate_url)

            if prefer_search_before_direct:
                searched_for_fallback = True
                searched_download_urls = self._direct_youtube_metadata_candidate_urls(song)

                if len(searched_download_urls) == 0:
                    try:
                        searched_download_urls = self.search_all(song)
                    except LookupError:
                        searched_download_urls = []

                for searched_download_url in searched_download_urls:
                    enqueue_download_url(searched_download_url)
                    enqueue_alternate_watch_urls(searched_download_url)

            enqueue_download_url(song.download_url)
            enqueue_alternate_watch_urls(song.download_url)

            audio_downloader = self._build_audio_downloader()

            # Add progress hook to the audio provider
            audio_downloader.audio_handler.add_progress_hook(
                display_progress_tracker.yt_dlp_progress_hook
            )
            download_url: Optional[str] = None
            download_info = None

            while download_info is None:
                if len(pending_download_urls) == 0:
                    if searched_for_fallback:
                        if last_download_error is not None:
                            raise last_download_error

                        raise LookupError(
                            f"No results found for song: {song.display_name}"
                        )

                    searched_for_fallback = True

                    try:
                        searched_download_urls = self.search_all(song)
                    except LookupError as exc:
                        if last_download_error is not None:
                            raise last_download_error from exc

                        raise exc

                    for searched_download_url in searched_download_urls:
                        enqueue_download_url(searched_download_url)
                        enqueue_alternate_watch_urls(searched_download_url)
                    continue

                download_url = pending_download_urls.pop(0)
                attempted_download_urls.add(download_url)

                logger.debug("Downloading %s using %s", song.display_name, download_url)

                try:
                    self._wait_for_next_download_attempt(download_url)
                    download_info = audio_downloader.get_download_metadata(
                        download_url, download=True
                    )
                except AudioProviderError as exc:
                    last_download_error = exc
                    if self._is_youtube_rate_limit_error(str(exc)):
                        self.youtube_rate_limit_message = str(exc)
                        logger.warning(
                            "YouTube cooldown detected while downloading %s. "
                            "Stopping additional source retries for this run.",
                            song.display_name,
                        )
                        raise exc

                    logger.info(
                        "Download attempt failed for %s using %s, trying the next source",
                        song.display_name,
                        download_url,
                    )

            temp_file = Path(
                temp_folder / f"{download_info['id']}.{download_info['ext']}"
            )

            if download_info is None:
                logger.debug(
                    "No download info found for %s, url: %s",
                    song.display_name,
                    download_url,
                )

                raise DownloaderError(
                    f"yt-dlp failed to get metadata for: {song.name} - {song.artist}"
                )

            display_progress_tracker.notify_download_complete()

            # Copy the downloaded file to the output file
            # if the temp file and output file have the same extension
            # and the bitrate is set to auto or disable
            # Don't copy if the audio provider is piped
            # unless the bitrate is set to disable
            if (
                self.settings["bitrate"] in ["auto", "disable", None]
                and temp_file.suffix == output_file.suffix
            ) and not (
                self.settings["audio_providers"][0] == "piped"
                and self.settings["bitrate"] != "disable"
            ):
                shutil.move(str(temp_file), output_file)
                success = True
                result = None
            else:
                if self.settings["bitrate"] in ["auto", None]:
                    # Use the bitrate from the download info if it exists
                    # otherwise use `copy`
                    bitrate = (
                        f"{int(download_info['abr'])}k"
                        if download_info.get("abr")
                        else "128k"
                    )
                elif self.settings["bitrate"] == "disable":
                    bitrate = None
                else:
                    bitrate = str(self.settings["bitrate"])

                # Convert the downloaded file to the output format
                success, result = convert(
                    input_file=temp_file,
                    output_file=output_file,
                    ffmpeg=self.ffmpeg,
                    output_format=self.settings["format"],
                    bitrate=bitrate,
                    ffmpeg_args=self.settings["ffmpeg_args"],
                    progress_handler=display_progress_tracker.ffmpeg_progress_hook,
                )

                if self.settings["create_skip_file"]:
                    with open(
                        str(output_file) + ".skip", mode="w", encoding="utf-8"
                    ) as _:
                        pass

            # Remove the temp file
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except (PermissionError, OSError) as exc:
                    logger.debug(
                        "Could not remove temp file: %s, error: %s", temp_file, exc
                    )

                    raise DownloaderError(
                        f"Could not remove temp file: {temp_file}, possible duplicate song"
                    ) from exc

            if not success and result:
                # If the conversion failed and there is an error message
                # create a file with the error message
                # and save it in the errors directory
                # raise an exception with file path
                file_name = (
                    get_errors_path()
                    / f"ffmpeg_error_{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.txt"
                )

                error_message = ""
                for key, value in result.items():
                    error_message += f"### {key}:\n{str(value).strip()}\n\n"

                with open(file_name, "w", encoding="utf-8") as error_path:
                    error_path.write(error_message)

                # Remove the file that failed to convert
                if output_file.exists():
                    output_file.unlink()

                raise FFmpegError(
                    f"Failed to convert {song.display_name}, "
                    f"you can find error here: {str(file_name.absolute())}"
                )

            download_info["filepath"] = str(output_file)

            # Set the song's download url
            if song.download_url is None:
                song.download_url = download_url

            display_progress_tracker.notify_conversion_complete()

            # SponsorBlock post processor
            if self.settings["sponsor_block"]:
                # Initialize the sponsorblock post processor
                post_processor = SponsorBlockPP(
                    audio_downloader.audio_handler, SPONSOR_BLOCK_CATEGORIES
                )

                # Run the post processor to get the sponsor segments
                _, download_info = post_processor.run(download_info)
                chapters = download_info["sponsorblock_chapters"]

                # If there are sponsor segments, remove them
                if len(chapters) > 0:
                    logger.info(
                        "Removing %s sponsor segments for %s",
                        len(chapters),
                        song.display_name,
                    )

                    # Initialize the modify chapters post processor
                    modify_chapters = ModifyChaptersPP(
                        downloader=audio_downloader.audio_handler,
                        remove_sponsor_segments=SPONSOR_BLOCK_CATEGORIES,
                    )

                    # Run the post processor to remove the sponsor segments
                    # this returns a list of files to delete
                    files_to_delete, download_info = modify_chapters.run(download_info)

                    # Delete the files that were created by the post processor
                    for file_to_delete in files_to_delete:
                        Path(file_to_delete).unlink()

            try:
                embed_metadata(
                    output_file,
                    song,
                    id3_separator=self.settings["id3_separator"],
                    skip_album_art=self.settings["skip_album_art"],
                )
            except Exception as exception:
                raise MetadataError(
                    "Failed to embed metadata to the song"
                ) from exception

            if self.settings["generate_lrc"]:
                generate_lrc(song, output_file)

            display_progress_tracker.notify_complete()

            # Add the song to the known songs
            self.known_songs.get(song.url, []).append(output_file)

            logger.info('Downloaded "%s": %s', song.display_name, song.download_url)

            return song, output_file
        except (Exception, UnicodeEncodeError) as exception:
            if isinstance(exception, UnicodeEncodeError):
                exception_cause = exception
                exception = DownloaderError(
                    "You may need to add PYTHONIOENCODING=utf-8 to your environment"
                )

                exception.__cause__ = exception_cause

            display_progress_tracker.notify_error(
                traceback.format_exc(), exception, True
            )
            self.errors.append(
                f"{song.url} - {exception.__class__.__name__}: {exception}"
            )
            return song, None
