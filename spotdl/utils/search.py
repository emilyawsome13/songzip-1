"""
Module for creating Song objects by interacting with Spotify API
or by parsing a query.

To use this module you must first initialize the SpotifyClient.
"""

import concurrent.futures
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from ytmusicapi import YTMusic

from spotdl.types.album import Album
from spotdl.types.artist import Artist
from spotdl.types.playlist import Playlist
from spotdl.types.saved import Saved
from spotdl.types.song import Song, SongList
from spotdl.utils.metadata import get_file_metadata
from spotdl.utils.spotify import SpotifyClient, SpotifyError

__all__ = [
    "QueryError",
    "get_search_results",
    "parse_query",
    "get_simple_songs",
    "reinit_song",
    "get_song_from_file_metadata",
    "gather_known_songs",
    "create_ytm_album",
    "create_ytm_artist",
    "create_ytm_playlist",
    "get_all_user_playlists",
    "get_user_saved_albums",
]

logger = logging.getLogger(__name__)
client = None  # pylint: disable=invalid-name


def get_ytm_client() -> YTMusic:
    """
    Lazily initialize the YTMusic client.

    ### Returns
    - the YTMusic client
    """

    global client  # pylint: disable=global-statement
    if client is None:
        client = YTMusic()

    return client


class QueryError(Exception):
    """
    Base class for all exceptions related to query.
    """


YTM_ARTIST_URL_REGEX = re.compile(
    r"^https?://(?:music\.)?youtube\.com/(?:channel|browse)/([^?&#/]+)"
)
YOUTUBE_ARTIST_URL_REGEX = re.compile(
    r"^https?://(?:(?:www|m)\.)?youtube\.com/(?:@[^?&#/]+|(?:channel|c|user)/[^?&#/]+)"
)
YOUTUBE_CHANNEL_URL_REGEX = re.compile(
    r"^https?://(?:(?:www|m)\.)?youtube\.com/channel/([^?&#/]+)"
)
YOUTUBE_HANDLE_REGEX = re.compile(r"@([^?&#/]+)")


def _get_ytm_artist_url(browse_id: str) -> str:
    """
    Build a canonical YouTube Music artist URL from a browse id.

    ### Arguments
    - browse_id: the artist browse id

    ### Returns
    - canonical YouTube Music artist url
    """

    path_type = "channel" if browse_id.startswith("UC") else "browse"
    return f"https://music.youtube.com/{path_type}/{browse_id}"


def _extract_youtube_video_id(url: str) -> Optional[str]:
    """
    Extract a single YouTube video id from common YouTube URL forms.

    ### Arguments
    - url: the source URL

    ### Returns
    - video id if present
    """

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or ""

    if host.endswith("youtu.be"):
        candidate = path.lstrip("/").split("/", 1)[0]
        return candidate or None

    if "youtube.com" not in host:
        return None

    if path == "/watch":
        values = urllib.parse.parse_qs(parsed.query).get("v") or []
        return values[0].strip() if values else None

    if path.startswith("/shorts/"):
        candidate = path.split("/shorts/", 1)[1].split("/", 1)[0]
        return candidate or None

    return None


def _youtube_video_fallback_metadata(url: str) -> Dict[str, Any]:
    """
    Read basic YouTube video metadata through yt-dlp when YT Music is incomplete.

    ### Arguments
    - url: source url

    ### Returns
    - metadata dictionary
    """

    from yt_dlp import YoutubeDL

    with YoutubeDL(
        {
            "quiet": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": 1,
        }
    ) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    return info if isinstance(info, dict) else {}


def _build_direct_youtube_song(url: str, use_ytm_data: bool = False) -> Song:
    """
    Build a single Song object from a direct YouTube video URL.

    ### Arguments
    - url: YouTube video URL
    - use_ytm_data: whether to force YouTube metadata over Spotify metadata

    ### Returns
    - Song object representing the single video
    """

    video_id = _extract_youtube_video_id(url)
    if not video_id:
        raise QueryError(f"Couldn't resolve a YouTube video id from {url}")

    is_youtube_music_url = "music.youtube.com" in url
    canonical_url = (
        f"https://music.youtube.com/watch?v={video_id}"
        if is_youtube_music_url
        else f"https://www.youtube.com/watch?v={video_id}"
    )
    track_data = get_ytm_client().get_song(video_id)
    video_details = track_data.get("videoDetails") or {}

    fallback_info: Dict[str, Any] = {}
    title = video_details.get("title") or track_data.get("title")
    author = video_details.get("author") or track_data.get("author")
    thumbnails = video_details.get("thumbnail", {}).get("thumbnails") or []
    duration_seconds = (
        track_data.get("lengthSeconds")
        or video_details.get("lengthSeconds")
    )

    if not author or not title:
        playability_status = track_data.get("playabilityStatus") or {}
        reason = playability_status.get("reason")
        if is_youtube_music_url and reason:
            raise QueryError(
                f"Couldn't read YouTube Music metadata for {url}: {reason}"
            )

        fallback_info = _youtube_video_fallback_metadata(canonical_url)
        title = title or fallback_info.get("title")
        author = (
            author
            or fallback_info.get("channel")
            or fallback_info.get("uploader")
            or fallback_info.get("channel_handle")
        )
        thumbnails = thumbnails or fallback_info.get("thumbnails") or []
        duration_seconds = duration_seconds or fallback_info.get("duration")

    if not author or not title:
        playability_status = track_data.get("playabilityStatus") or {}
        reason = playability_status.get("reason")
        if reason:
            raise QueryError(
                f"Couldn't read {'YouTube Music' if is_youtube_music_url else 'YouTube'} metadata for {url}: {reason}"
            )

        raise QueryError(
            f"Couldn't read {'YouTube Music' if is_youtube_music_url else 'YouTube'} metadata for {url}"
        )

    try:
        youtube_song = Song.from_search_term(f"{author} - {title}")
    except Exception:  # pylint: disable=broad-except
        youtube_song = Song.from_missing_data(
            name=title,
            artists=[author],
            artist=author,
            genres=[],
            disc_number=1,
            disc_count=1,
            album_name=author,
            album_artist=author,
            duration=int(duration_seconds or 0),
            year=0,
            date="",
            track_number=1,
            tracks_count=1,
            song_id=video_id,
            explicit=False,
            publisher="",
            url=canonical_url,
            isrc=None,
            cover_url=(
                thumbnails[-1].get("url")
                if thumbnails and isinstance(thumbnails[-1], dict)
                else None
            ),
        )

    if use_ytm_data or youtube_song.url == canonical_url:
        youtube_song.name = title
        youtube_song.artist = author
        youtube_song.artists = [author]
        youtube_song.album_name = youtube_song.album_name or author
        youtube_song.album_artist = youtube_song.album_artist or author
        if duration_seconds is not None:
            youtube_song.duration = int(duration_seconds)
        if youtube_song.song_id in (None, "", "None"):
            youtube_song.song_id = video_id
        if youtube_song.cover_url is None and thumbnails:
            youtube_song.cover_url = (
                thumbnails[-1].get("url")
                if isinstance(thumbnails[-1], dict)
                else None
            )

    youtube_song.download_url = canonical_url
    if not youtube_song.url:
        youtube_song.url = canonical_url
    youtube_song.source_hint = "direct_youtube_video"

    return youtube_song


def _get_ytm_artist_browse_id(request: str) -> Optional[str]:
    """
    Extract a YouTube Music artist browse id from a request string.

    ### Arguments
    - request: the query or url

    ### Returns
    - browse id if present
    """

    match = YTM_ARTIST_URL_REGEX.match(request)
    if match is None:
        return None

    return match.group(1)


def _is_supported_youtube_artist_url(request: str) -> bool:
    """
    Check whether a request looks like a YouTube artist/channel url.

    ### Arguments
    - request: the query or url

    ### Returns
    - whether the request is a supported YouTube artist url
    """

    return (
        YTM_ARTIST_URL_REGEX.match(request) is not None
        or YOUTUBE_ARTIST_URL_REGEX.match(request) is not None
    )


def _resolve_youtube_artist_browse_id(url: str) -> Optional[str]:
    """
    Resolve a regular YouTube channel or handle url to a browse id.

    ### Arguments
    - url: the youtube url to resolve

    ### Returns
    - channel / browse id if it can be resolved
    """

    direct_match = YOUTUBE_CHANNEL_URL_REGEX.match(url)
    if direct_match is not None:
        return direct_match.group(1)

    if YOUTUBE_ARTIST_URL_REGEX.match(url) is None:
        return None

    from yt_dlp import YoutubeDL

    with YoutubeDL(
        {
            "quiet": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": 1,
        }
    ) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        return None

    channel_id = info.get("channel_id")
    if channel_id:
        return channel_id

    info_id = info.get("id")
    if isinstance(info_id, str) and info_id.startswith("UC"):
        return info_id

    return None


def _normalize_artist_search_term(value: str) -> str:
    """
    Normalize artist search text for fallback matching.

    ### Arguments
    - value: source text

    ### Returns
    - normalized text
    """

    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _get_youtube_artist_search_terms(url: str) -> List[str]:
    """
    Build fallback artist search terms from a YouTube or ytartist request.

    ### Arguments
    - url: original request

    ### Returns
    - ordered unique candidate search terms
    """

    terms: List[str] = []

    if url.lower().startswith("ytartist:"):
        search_term = url.split(":", 1)[1].strip()
        if search_term:
            terms.append(search_term)
    else:
        handle_match = YOUTUBE_HANDLE_REGEX.search(url)
        if handle_match is not None:
            terms.append(handle_match.group(1).replace("_", " "))

        if _is_supported_youtube_artist_url(url):
            from yt_dlp import YoutubeDL

            with YoutubeDL(
                {
                    "quiet": True,
                    "extract_flat": True,
                    "skip_download": True,
                    "playlistend": 1,
                }
            ) as ydl:
                info = ydl.extract_info(url, download=False)

            if isinstance(info, dict):
                for key in ("channel", "title", "uploader"):
                    value = info.get(key)
                    if isinstance(value, str) and value.strip():
                        terms.append(value.strip())

                uploader_id = info.get("uploader_id")
                if isinstance(uploader_id, str) and uploader_id.startswith("@"):
                    terms.append(uploader_id[1:].replace("_", " "))

    unique_terms: List[str] = []
    seen = set()
    for term in terms:
        normalized = _normalize_artist_search_term(term)
        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        unique_terms.append(term.strip())

    return unique_terms


def _fallback_ytm_artist_browse_id(
    ytm_client: YTMusic,
    search_terms: List[str],
    failed_browse_id: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve an artist browse id by searching YouTube Music with fallback terms.

    ### Arguments
    - ytm_client: initialized YouTube Music client
    - search_terms: candidate artist terms
    - failed_browse_id: browse id that already failed, if any

    ### Returns
    - working browse id if found
    """

    for term in search_terms:
        artist_hits = ytm_client.search(term, filter="artists", limit=5)
        if len(artist_hits) == 0:
            continue

        normalized_term = _normalize_artist_search_term(term)
        best_hit = None
        best_score = -1

        for hit in artist_hits:
            browse_id = hit.get("browseId")
            if browse_id is None or browse_id == failed_browse_id:
                continue

            artist_name = (
                hit.get("artist")
                or hit.get("name")
                or hit.get("title")
                or ""
            )
            normalized_name = _normalize_artist_search_term(str(artist_name))
            if normalized_name == "":
                score = 0
            elif normalized_name == normalized_term:
                score = 100
            elif normalized_term in normalized_name or normalized_name in normalized_term:
                score = 80
            else:
                score = len(
                    set(normalized_name.split()).intersection(normalized_term.split())
                )

            if score > best_score:
                best_score = score
                best_hit = browse_id

        if best_hit:
            return best_hit

    return None


def _parse_year(value: Any) -> int:
    """
    Parse a year-like value into an integer.

    ### Arguments
    - value: the value to parse

    ### Returns
    - parsed year or 0 if unavailable
    """

    if value is None:
        return 0

    match = re.search(r"(\d{4})", str(value))
    if match is None:
        return 0

    return int(match.group(1))


def _parse_duration_seconds(value: Any) -> int:
    """
    Parse a duration value into seconds.

    ### Arguments
    - value: the value to parse

    ### Returns
    - duration in seconds
    """

    if value is None:
        return 0

    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.isdigit():
        return int(value)

    parts = str(value).split(":")
    if not all(part.isdigit() for part in parts):
        return 0

    total = 0
    for part in parts:
        total = (total * 60) + int(part)

    return total


def _get_best_thumbnail_url(items: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """
    Get the highest-resolution thumbnail url from a list.

    ### Arguments
    - items: list of thumbnail dictionaries

    ### Returns
    - thumbnail url if available
    """

    if not items:
        return None

    best_item = max(
        items,
        key=lambda item: item.get("width", 0) * item.get("height", 0),
    )
    return best_item.get("url")


def _ytm_album_matches_artist(
    result: Dict[str, Any], artist_name: str, artist_browse_id: str
) -> bool:
    """
    Check whether a YouTube Music album search result belongs to an artist.

    ### Arguments
    - result: the album search result
    - artist_name: the resolved artist name
    - artist_browse_id: the artist browse id

    ### Returns
    - whether the album belongs to the requested artist
    """

    for artist in result.get("artists") or []:
        if artist.get("id") == artist_browse_id:
            return True

        if artist.get("name", "").casefold() == artist_name.casefold():
            return True

    return False


def _build_ytm_song(
    track: Dict[str, Any],
    artist_name: str,
    artist_browse_id: str,
    album: Dict[str, Any],
    track_number: int,
) -> Optional[Song]:
    """
    Build a Song object from YouTube Music album track data.

    ### Arguments
    - track: raw track payload
    - artist_name: resolved artist name
    - artist_browse_id: artist browse id
    - album: raw album payload
    - track_number: fallback track number

    ### Returns
    - Song object or None if the track cannot be downloaded
    """

    video_id = track.get("videoId")
    if video_id is None or track.get("isAvailable") is False:
        return None

    artists = [artist["name"] for artist in track.get("artists") or [] if artist.get("name")]
    if len(artists) == 0:
        artists = [artist_name]

    primary_artist = artists[0]
    album_artist = (
        next(
            (
                artist.get("name")
                for artist in album.get("artists") or []
                if artist.get("name")
            ),
            None,
        )
        or artist_name
    )
    year = _parse_year(album.get("year"))
    cover_url = _get_best_thumbnail_url(track.get("thumbnails")) or _get_best_thumbnail_url(
        album.get("thumbnails")
    )
    download_url = f"https://music.youtube.com/watch?v={video_id}"

    return Song.from_missing_data(
        name=track["title"],
        artists=artists,
        artist=primary_artist,
        artist_id=(track.get("artists") or [{}])[0].get("id") or artist_browse_id,
        genres=[],
        disc_number=1,
        disc_count=1,
        album_name=album.get("title") or track.get("album") or artist_name,
        album_artist=album_artist,
        duration=track.get("duration_seconds")
        or _parse_duration_seconds(track.get("duration")),
        year=year,
        date=f"{year}-01-01" if year else "",
        track_number=track.get("trackNumber") or track_number,
        tracks_count=album.get("trackCount") or len(album.get("tracks") or []) or 1,
        song_id=video_id,
        explicit=bool(track.get("isExplicit")),
        publisher="",
        url=download_url,
        isrc=None,
        cover_url=cover_url,
        copyright_text=None,
        download_url=download_url,
        popularity=None,
        album_id=album.get("browseId") or album.get("audioPlaylistId") or album.get("title"),
        album_type=album.get("type"),
    )


def create_ytm_artist(url: str, fetch_songs: bool = True) -> Artist:
    """
    Create an Artist object from a YouTube Music artist url or ytartist query.

    ### Arguments
    - url: the url or ytartist query
    - fetch_songs: unused for YouTube Music artists; kept for parity

    ### Returns
    - Artist object with downloadable YouTube Music tracks
    """

    _ = fetch_songs

    ytm_client = get_ytm_client()
    fallback_search_terms = _get_youtube_artist_search_terms(url)
    browse_id = _get_ytm_artist_browse_id(url)

    if browse_id is None and _is_supported_youtube_artist_url(url):
        browse_id = _resolve_youtube_artist_browse_id(url)
        if browse_id is None:
            raise ValueError(f"Couldn't resolve artist on YouTube: {url}")

        url = _get_ytm_artist_url(browse_id)

    if browse_id is None:
        if not url.lower().startswith("ytartist:"):
            raise ValueError(f"Invalid artist url: {url}")

        search_term = url.split(":", 1)[1].strip()
        if search_term == "":
            raise ValueError("ytartist query is missing an artist name")

        artist_hits = ytm_client.search(search_term, filter="artists", limit=1)
        if len(artist_hits) == 0 or artist_hits[0].get("browseId") is None:
            raise ValueError(f"Couldn't find artist on YouTube Music: {search_term}")

        browse_id = artist_hits[0]["browseId"]
        url = _get_ytm_artist_url(browse_id)

    try:
        artist = ytm_client.get_artist(browse_id)
    except (KeyError, TypeError, ValueError) as exception:
        fallback_browse_id = _fallback_ytm_artist_browse_id(
            ytm_client,
            fallback_search_terms,
            failed_browse_id=browse_id,
        )
        if fallback_browse_id is None:
            raise ValueError(f"Couldn't fetch artist metadata: {url}") from exception

        browse_id = fallback_browse_id
        url = _get_ytm_artist_url(browse_id)
        artist = ytm_client.get_artist(browse_id)

    if artist is None:
        raise ValueError(f"Couldn't fetch artist: {url}")

    artist_name = artist.get("name")
    if artist_name is None:
        fallback_browse_id = _fallback_ytm_artist_browse_id(
            ytm_client,
            fallback_search_terms,
            failed_browse_id=browse_id,
        )
        if fallback_browse_id is None:
            raise ValueError(f"Couldn't resolve artist metadata: {url}")

        browse_id = fallback_browse_id
        url = _get_ytm_artist_url(browse_id)
        artist = ytm_client.get_artist(browse_id)
        artist_name = artist.get("name")
        if artist_name is None:
            raise ValueError(f"Couldn't resolve artist metadata: {url}")

    album_entries: Dict[str, Dict[str, Any]] = {}
    for section_name in ("albums", "singles"):
        section = artist.get(section_name) or {}
        for result in section.get("results") or []:
            result_browse_id = result.get("browseId")
            if result_browse_id:
                album_entries.setdefault(result_browse_id, result)

    for result in ytm_client.search(artist_name, filter="albums", limit=50):
        result_browse_id = result.get("browseId")
        if result_browse_id is None:
            continue

        if _ytm_album_matches_artist(result, artist_name, browse_id):
            album_entries.setdefault(result_browse_id, result)

    songs: List[Song] = []
    albums: List[str] = []
    seen_video_ids = set()

    for album_browse_id in album_entries:
        album = ytm_client.get_album(album_browse_id)
        if album is None:
            continue

        album["browseId"] = album_browse_id
        albums.append(f"https://music.youtube.com/browse/{album_browse_id}")

        for index, track in enumerate(album.get("tracks") or [], start=1):
            song = _build_ytm_song(track, artist_name, browse_id, album, index)
            if song is None or song.song_id in seen_video_ids:
                continue

            seen_video_ids.add(song.song_id)
            songs.append(song)

    if len(songs) == 0:
        top_songs = (artist.get("songs") or {}).get("results") or []
        pseudo_album = {
            "title": artist_name,
            "artists": [{"name": artist_name, "id": browse_id}],
            "year": None,
            "trackCount": len(top_songs),
            "type": "Artist",
            "thumbnails": artist.get("thumbnails"),
            "browseId": browse_id,
        }

        for index, track in enumerate(top_songs, start=1):
            song = _build_ytm_song(track, artist_name, browse_id, pseudo_album, index)
            if song is None or song.song_id in seen_video_ids:
                continue

            seen_video_ids.add(song.song_id)
            songs.append(song)

    return Artist(
        name=artist_name,
        genres=[],
        url=url,
        albums=albums,
        urls=[song.url for song in songs],
        songs=songs,
    )


def get_search_results(search_term: str) -> List[Song]:
    """
    Creates a list of Song objects from a search term.

    ### Arguments
    - search_term: the search term to use

    ### Returns
    - a list of Song objects
    """

    return Song.list_from_search_term(search_term)


def parse_query(
    query: List[str],
    threads: int = 1,
    use_ytm_data: bool = False,
    playlist_numbering: bool = False,
    album_type=None,
    playlist_retain_track_cover: bool = False,
) -> List[Song]:
    """
    Parse query and return list containing song object

    ### Arguments
    - query: List of strings containing query
    - threads: Number of threads to use

    ### Returns
    - List of song objects
    """

    songs: List[Song] = get_simple_songs(
        query,
        use_ytm_data=use_ytm_data,
        playlist_numbering=playlist_numbering,
        album_type=album_type,
        playlist_retain_track_cover=playlist_retain_track_cover,
    )

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        future_to_song = {executor.submit(reinit_song, song): song for song in songs}
        for future in concurrent.futures.as_completed(future_to_song):
            song = future_to_song[future]
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("%s generated an exception: %s", song.display_name, exc)

    return results


def get_simple_songs(
    query: List[str],
    use_ytm_data: bool = False,
    playlist_numbering: bool = False,
    albums_to_ignore=None,
    album_type=None,
    playlist_retain_track_cover: bool = False,
) -> List[Song]:
    """
    Parse query and return list containing simple song objects

    ### Arguments
    - query: List of strings containing query

    ### Returns
    - List of simple song objects
    """

    songs: List[Song] = []
    lists: List[SongList] = []
    for request in query:
        logger.info("Processing query: %s", request)

        # Remove /intl-xxx/ from Spotify URLs with regex
        request = re.sub(r"\/intl-\w+\/", "/", request)

        if (
            (  # pylint: disable=too-many-boolean-expressions
                "watch?v=" in request
                or "youtu.be/" in request
                or "soundcloud.com/" in request
                or "bandcamp.com/" in request
            )
            and "open.spotify.com" in request
            and "track" in request
            and "|" in request
        ):
            split_urls = request.split("|")
            if (
                len(split_urls) <= 1
                or not (
                    "watch?v=" in split_urls[0]
                    or "youtu.be" in split_urls[0]
                    or "soundcloud.com/" in split_urls[0]
                    or "bandcamp.com/" in split_urls[0]
                )
                or "spotify" not in split_urls[1]
            ):
                raise QueryError(
                    'Incorrect format used, please use "YouTubeURL|SpotifyURL"'
                )

            songs.append(
                Song.from_missing_data(url=split_urls[1], download_url=split_urls[0])
            )
        elif "music.youtube.com/watch?v" in request or _extract_youtube_video_id(request):
            songs.append(_build_direct_youtube_song(request, use_ytm_data=use_ytm_data))
        elif (
            "youtube.com/playlist?list=" in request
            or "youtube.com/browse/VLPL" in request
        ):
            request = request.replace(
                "https://www.youtube.com/", "https://music.youtube.com/"
            )
            request = request.replace(
                "https://youtube.com/", "https://music.youtube.com/"
            )

            split_urls = request.split("|")
            if len(split_urls) == 1:
                if "?list=OLAK5uy_" in request:
                    lists.append(create_ytm_album(request, fetch_songs=False))
                elif "?list=PL" in request or "browse/VLPL" in request:
                    lists.append(create_ytm_playlist(request, fetch_songs=False))
            else:
                if ("spotify" not in split_urls[1]) or not any(
                    x in split_urls[0]
                    for x in ["?list=PL", "?list=OLAK5uy_", "browse/VLPL"]
                ):
                    raise QueryError(
                        'Incorrect format used, please use "YouTubeMusicURL|SpotifyURL". '
                        "Currently only supports YouTube Music playlists and albums."
                    )

                if ("open.spotify.com" in request and "album" in request) and (
                    "?list=OLAK5uy_" in request
                ):
                    ytm_list: SongList = create_ytm_album(
                        split_urls[0], fetch_songs=False
                    )
                    spot_list = Album.from_url(split_urls[1], fetch_songs=False)
                elif ("open.spotify.com" in request and "playlist" in request) and (
                    "?list=PL" in request or "browse/VLPL" in request
                ):
                    ytm_list = create_ytm_playlist(split_urls[0], fetch_songs=False)
                    spot_list = Playlist.from_url(split_urls[1], fetch_songs=False)
                else:
                    raise QueryError(
                        f"URLs are not of the same type, {split_urls[0]} is not "
                        f"the same type as {split_urls[1]}."
                    )

                if ytm_list.length != spot_list.length:
                    raise QueryError(
                        f"The YouTube Music ({ytm_list.length}) "
                        f"and Spotify ({spot_list.length}) lists have different lengths. "
                    )

                if use_ytm_data:
                    for index, song in enumerate(ytm_list.songs):
                        song.url = spot_list.songs[index].url

                    lists.append(ytm_list)
                else:
                    for index, song in enumerate(spot_list.songs):
                        song.download_url = ytm_list.songs[index].download_url

                    lists.append(spot_list)
        elif _is_supported_youtube_artist_url(request):
            lists.append(create_ytm_artist(request, fetch_songs=False))
        elif "open.spotify.com" in request and "track" in request:
            songs.append(Song.from_url(url=request))
        elif "https://spotify.link/" in request:
            resp = requests.head(request, allow_redirects=True, timeout=10)
            full_url = resp.url
            full_lists = get_simple_songs(
                [full_url],
                use_ytm_data=use_ytm_data,
                playlist_numbering=playlist_numbering,
                album_type=album_type,
                playlist_retain_track_cover=playlist_retain_track_cover,
            )
            songs.extend(full_lists)
        elif "open.spotify.com" in request and "playlist" in request:
            lists.append(Playlist.from_url(request, fetch_songs=False))
        elif "open.spotify.com" in request and "album" in request:
            lists.append(Album.from_url(request, fetch_songs=False))
        elif "open.spotify.com" in request and "artist" in request:
            lists.append(Artist.from_url(request, fetch_songs=False))
        elif "open.spotify.com" in request and "user" in request:
            lists.extend(get_all_user_playlists(request))
        elif "album:" in request:
            lists.append(Album.from_search_term(request, fetch_songs=False))
        elif "playlist:" in request:
            lists.append(Playlist.from_search_term(request, fetch_songs=False))
        elif "artist:" in request:
            lists.append(Artist.from_search_term(request, fetch_songs=False))
        elif request.lower().startswith("ytartist:"):
            lists.append(create_ytm_artist(request, fetch_songs=False))
        elif request == "saved":
            lists.append(Saved.from_url(request, fetch_songs=False))
        elif request == "all-user-playlists":
            lists.extend(get_all_user_playlists())
        elif request == "all-user-followed-artists":
            lists.extend(get_user_followed_artists())
        elif request == "all-user-saved-albums":
            lists.extend(get_user_saved_albums())
        elif request == "all-saved-playlists":
            lists.extend(get_all_saved_playlists())
        elif request.endswith(".spotdl"):
            with open(request, "r", encoding="utf-8") as save_file:
                for track in json.load(save_file):
                    # Append to songs
                    songs.append(Song.from_dict(track))
        else:
            songs.append(Song.from_search_term(request))

    for song_list in lists:
        logger.info(
            "Found %s songs in %s (%s)",
            len(song_list.urls),
            song_list.name,
            song_list.__class__.__name__,
        )

        for index, song in enumerate(song_list.songs):
            song_data = song.json
            song_data["list_name"] = song_list.name
            song_data["list_url"] = song_list.url
            song_data["list_position"] = song.list_position
            song_data["list_length"] = song_list.length

            if playlist_numbering:
                song_data["track_number"] = song_data["list_position"]
                song_data["tracks_count"] = song_data["list_length"]
                song_data["album_name"] = song_data["list_name"]
                song_data["disc_number"] = 1
                song_data["disc_count"] = 1
                if isinstance(song_list, Playlist):
                    song_data["album_artist"] = song_list.author_name
                    song_data["cover_url"] = song_list.cover_url

            if playlist_retain_track_cover:
                song_data["track_number"] = song_data["list_position"]
                song_data["tracks_count"] = song_data["list_length"]
                song_data["album_name"] = song_data["list_name"]
                song_data["disc_number"] = 1
                song_data["disc_count"] = 1
                song_data["cover_url"] = song_data["cover_url"]
                if isinstance(song_list, Playlist):
                    song_data["album_artist"] = song_list.author_name

            songs.append(Song.from_dict(song_data))

    # removing songs for --ignore-albums
    original_length = len(songs)
    if albums_to_ignore:
        songs = [
            song
            for song in songs
            if all(
                keyword not in song.album_name.lower() for keyword in albums_to_ignore
            )
        ]
        logger.info("Skipped %s songs (Ignored albums)", (original_length - len(songs)))

    if album_type:
        songs = [song for song in songs if song.album_type == album_type]

        logger.info(
            "Skipped %s songs for Album Type %s",
            (original_length - len(songs)),
            album_type,
        )

    logger.debug("Found %s songs in %s lists", len(songs), len(lists))

    return songs


def songs_from_albums(albums: List[str]):
    """
    Get all songs from albums ids/urls/etc.

    ### Arguments
    - albums: List of albums ids

    ### Returns
    - List of songs
    """

    songs: List[Song] = []
    for album_id in albums:
        album = Album.from_url(album_id, fetch_songs=False)

        songs.extend([Song.from_missing_data(**song.json) for song in album.songs])

    return songs


def get_all_user_playlists(user_url: str = "") -> List[Playlist]:
    """
    Get all user playlists.

    ### Args (optional)
    - user_url: Spotify user profile url.
        If a url is mentioned, get all public playlists of that specific user.

    ### Returns
    - List of all user playlists
    """

    spotify_client = SpotifyClient()
    if spotify_client.user_auth is False:  # type: ignore
        raise SpotifyError("You must be logged in to use this function")

    if user_url and not user_url.startswith("https://open.spotify.com/user/"):
        raise ValueError(f"Invalid user profile url: {user_url}")

    user_id = user_url.split("https://open.spotify.com/user/")[-1].replace("/", "")

    if user_id:
        user_playlists_response = spotify_client.user_playlists(user_id)
    else:
        user_playlists_response = spotify_client.current_user_playlists()
        user_resp = spotify_client.current_user()
        if user_resp is None:
            raise SpotifyError("Couldn't get user info")

        user_id = user_resp["id"]

    if user_playlists_response is None:
        raise SpotifyError("Couldn't get user playlists")

    user_playlists = user_playlists_response["items"]

    # Fetch all saved tracks
    while user_playlists_response and user_playlists_response["next"]:
        response = spotify_client.next(user_playlists_response)
        if response is None:
            break

        user_playlists_response = response
        user_playlists.extend(user_playlists_response["items"])

    return [
        Playlist.from_url(playlist["external_urls"]["spotify"], fetch_songs=False)
        for playlist in user_playlists
        if playlist["owner"]["id"] == user_id
    ]


def get_user_saved_albums() -> List[Album]:
    """
    Get all user saved albums

    ### Returns
    - List of all user saved albums
    """

    spotify_client = SpotifyClient()
    if spotify_client.user_auth is False:  # type: ignore
        raise SpotifyError("You must be logged in to use this function")

    user_saved_albums_response = spotify_client.current_user_saved_albums()
    if user_saved_albums_response is None:
        raise SpotifyError("Couldn't get user saved albums")

    user_saved_albums = user_saved_albums_response["items"]

    # Fetch all saved tracks
    while user_saved_albums_response and user_saved_albums_response["next"]:
        response = spotify_client.next(user_saved_albums_response)
        if response is None:
            break

        user_saved_albums_response = response
        user_saved_albums.extend(user_saved_albums_response["items"])

    return [
        Album.from_url(item["album"]["external_urls"]["spotify"], fetch_songs=False)
        for item in user_saved_albums
    ]


def get_user_followed_artists() -> List[Artist]:
    """
    Get all user playlists

    ### Returns
    - List of all user playlists
    """

    spotify_client = SpotifyClient()
    if spotify_client.user_auth is False:  # type: ignore
        raise SpotifyError("You must be logged in to use this function")

    user_followed_response = spotify_client.current_user_followed_artists()
    if user_followed_response is None:
        raise SpotifyError("Couldn't get user followed artists")

    user_followed_response = user_followed_response["artists"]
    user_followed = user_followed_response["items"]

    # Fetch all artists
    while user_followed_response and user_followed_response["next"]:
        response = spotify_client.next(user_followed_response)
        if response is None:
            break

        user_followed_response = response["artists"]
        user_followed.extend(user_followed_response["items"])

    return [
        Artist.from_url(followed_artist["external_urls"]["spotify"], fetch_songs=False)
        for followed_artist in user_followed
    ]


def get_all_saved_playlists() -> List[Playlist]:
    """
    Get all user playlists.

    ### Args (optional)
    - user_url: Spotify user profile url.
        If a url is mentioned, get all public playlists of that specific user.

    ### Returns
    - List of all user playlists
    """

    spotify_client = SpotifyClient()
    if spotify_client.user_auth is False:  # type: ignore
        raise SpotifyError("You must be logged in to use this function")

    user_playlists_response = spotify_client.current_user_playlists()

    if user_playlists_response is None:
        raise SpotifyError("Couldn't get user playlists")

    user_playlists = user_playlists_response["items"]
    user_id = user_playlists_response["href"].split("users/")[-1].split("/")[0]

    # Fetch all saved tracks
    while user_playlists_response and user_playlists_response["next"]:
        response = spotify_client.next(user_playlists_response)
        if response is None:
            break

        user_playlists_response = response
        user_playlists.extend(user_playlists_response["items"])

    return [
        Playlist.from_url(playlist["external_urls"]["spotify"], fetch_songs=False)
        for playlist in user_playlists
        if playlist["owner"]["id"] != user_id
    ]


def reinit_song(song: Song) -> Song:
    """
    Update song object with new data
    from Spotify

    ### Arguments
    - song: Song object

    ### Returns
    - Updated song object
    """

    data = song.json
    if data.get("url"):
        new_data = Song.from_url(data["url"]).json
    elif data.get("song_id"):
        new_data = Song.from_url(
            "https://open.spotify.com/track/" + data["song_id"]
        ).json
    elif data.get("name") and data.get("artist"):
        new_data = Song.from_search_term(f"{data['artist']} - {data['name']}").json
    else:
        raise QueryError("Song object is missing required data to be reinitialized")

    for key in Song.__dataclass_fields__:  # type: ignore # pylint: disable=E1101
        val = data.get(key)
        new_val = new_data.get(key)
        if new_val is not None and val is None:
            data[key] = new_val
        elif new_val is not None and val is not None:
            data[key] = val

    # return reinitialized song object
    return Song(**data)


def get_song_from_file_metadata(file: Path, id3_separator: str = "/") -> Optional[Song]:
    """
    Get song based on the file metadata or file name

    ### Arguments
    - file: Path to file

    ### Returns
    - Song object
    """

    file_metadata = get_file_metadata(file, id3_separator)

    if file_metadata is None:
        return None

    return Song.from_missing_data(**file_metadata)


def gather_known_songs(output: str, output_format: str) -> Dict[str, List[Path]]:
    """
    Gather all known songs from the output directory

    ### Arguments
    - output: Output path template
    - output_format: Output format

    ### Returns
    - Dictionary containing all known songs and their paths
    """

    # Get the base directory from the path template
    # Path("/Music/test/{artist}/{artists} - {title}.{output-ext}") -> "/Music/test"
    base_dir = output.split("{", 1)[0]
    paths = Path(base_dir).glob(f"**/*.{output_format}")

    known_songs: Dict[str, List[Path]] = {}
    for path in paths:
        # Try to get the song from the metadata
        song = get_song_from_file_metadata(path)

        # If the songs doesn't have metadata, try to get it from the filename
        if song is None or song.url is None:
            search_results = get_search_results(path.stem)
            if len(search_results) == 0:
                continue

            song = search_results[0]

        known_paths = known_songs.get(song.url)
        if known_paths is None:
            known_songs[song.url] = [path]
        else:
            known_songs[song.url].append(path)

    return known_songs


def create_ytm_album(url: str, fetch_songs: bool = True) -> Album:
    """
    Creates a list of Song objects from an album query.

    ### Arguments
    - album_query: the url of the album

    ### Returns
    - a list of Song objects
    """

    if "?list=" not in url or not url.startswith("https://music.youtube.com/"):
        raise ValueError(f"Invalid album url: {url}")

    browse_id = get_ytm_client().get_album_browse_id(
        url.split("?list=")[1].split("&")[0]
    )
    if browse_id is None:
        raise ValueError(f"Invalid album url: {url}")

    album = get_ytm_client().get_album(browse_id)

    if album is None:
        raise ValueError(f"Couldn't fetch album: {url}")

    metadata = {
        "artist": album["artists"][0]["name"],
        "name": album["title"],
        "url": url,
    }

    songs = []
    for track in album["tracks"]:
        artists = [artist["name"] for artist in track["artists"]]

        song = Song.from_missing_data(
            name=track["title"],
            artists=artists,
            artist=artists[0],
            album_name=metadata["name"],
            album_artist=metadata["artist"],
            duration=track["duration_seconds"],
            download_url=f"https://music.youtube.com/watch?v={track['videoId']}",
        )

        if fetch_songs:
            song = Song.from_search_term(f"{song.artist} - {song.name}")

        songs.append(song)

    return Album(**metadata, songs=songs, urls=[song.url for song in songs])


def create_ytm_playlist(url: str, fetch_songs: bool = True) -> Playlist:
    """
    Returns a playlist object from a youtube playlist url

    ### Arguments
    - url: the url of the playlist

    ### Returns
    - a Playlist object
    """

    if not ("?list=" in url or "/browse/VLPL" in url) or not url.startswith(
        "https://music.youtube.com/"
    ):
        raise ValueError(f"Invalid playlist url: {url}")

    if "/browse/VLPL" in url:
        playlist_id = url.split("/browse/")[1]
    else:
        playlist_id = url.split("?list=")[1]
    playlist = get_ytm_client().get_playlist(playlist_id, None)  # type: ignore

    if playlist is None:
        raise ValueError(f"Couldn't fetch playlist: {url}")

    metadata = {
        "description": (
            playlist["description"] if playlist["description"] is not None else ""
        ),
        "author_url": (
            f"https://music.youtube.com/channel/{playlist['author']['id']}"
            if playlist.get("author") is not None
            else "Missing author url"
        ),
        "author_name": (
            playlist["author"]["name"]
            if playlist.get("author") is not None
            else "Missing author"
        ),
        "cover_url": (
            playlist["thumbnails"][0]["url"]
            if playlist.get("thumbnails") is not None
            else "Missing thumbnails"
        ),
        "name": playlist["title"],
        "url": url,
    }

    songs = []
    for track in playlist["tracks"]:
        if track["videoId"] is None or track["isAvailable"] is False:
            continue

        song = Song.from_missing_data(
            name=track["title"],
            artists=(
                [artist["name"] for artist in track["artists"]]
                if track.get("artists") is not None
                else []
            ),
            artist=(
                track["artists"][0]["name"]
                if track.get("artists") is not None
                else None
            ),
            album_name=(
                track.get("album", {}).get("name")
                if track.get("album") is not None
                else None
            ),
            duration=track.get("duration_seconds"),
            explicit=track.get("isExplicit"),
            download_url=f"https://music.youtube.com/watch?v={track['videoId']}",
        )

        if fetch_songs:
            song = reinit_song(song)

        songs.append(song)

    return Playlist(**metadata, songs=songs, urls=[song.url for song in songs])
