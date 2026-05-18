import unittest
from unittest.mock import patch

from spotdl.console.entry_point import _disable_blocked_ytmusic_provider
from spotdl.download.downloader import Downloader
from spotdl.providers.audio.base import AudioProvider, AudioProviderError
from spotdl.types.result import Result
from spotdl.types.song import Song
from spotdl.utils.matching import calc_album_match
from spotdl.utils.search import QueryError, create_ytm_artist, get_simple_songs


class DummyAudioProvider(AudioProvider):
    SUPPORTS_ISRC = False
    GET_RESULTS_OPTS = []

    def get_results(self, search_term: str, **kwargs):
        return []


class YouTubeMusicResilienceTest(unittest.TestCase):
    def test_blocked_ytmusic_falls_back_to_remaining_providers(self):
        settings = {
            "audio_providers": ["youtube-music", "soundcloud", "youtube", "bandcamp"]
        }

        with patch(
            "spotdl.console.entry_point.check_ytmusic_connection",
            return_value=False,
        ):
            changed = _disable_blocked_ytmusic_provider(settings)

        self.assertTrue(changed)
        self.assertEqual(
            settings["audio_providers"],
            ["soundcloud", "youtube", "bandcamp"],
        )

    def test_blocked_ytmusic_uses_youtube_when_no_other_provider_exists(self):
        settings = {"audio_providers": ["youtube-music"]}

        with patch(
            "spotdl.console.entry_point.check_ytmusic_connection",
            return_value=False,
        ):
            changed = _disable_blocked_ytmusic_provider(settings)

        self.assertTrue(changed)
        self.assertEqual(settings["audio_providers"], ["youtube"])

    def test_missing_video_details_uses_top_level_fields(self):
        song_stub = Song.from_missing_data(
            name="Placeholder",
            artist="Placeholder Artist",
            artists=["Placeholder Artist"],
        )

        with patch("spotdl.utils.search.get_ytm_client") as mock_get_ytm_client:
            mock_get_ytm_client.return_value.get_song.return_value = {
                "title": "29 Intro",
                "author": "J. Cole",
                "lengthSeconds": "94",
            }

            with patch(
                "spotdl.utils.search.Song.from_search_term",
                return_value=song_stub,
            ) as mock_from_search:
                songs = get_simple_songs(
                    ["https://music.youtube.com/watch?v=JDVRv15xqRI"],
                )

        mock_from_search.assert_called_once_with("J. Cole - 29 Intro")
        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].download_url, "https://music.youtube.com/watch?v=JDVRv15xqRI")

    def test_missing_video_details_raises_query_error_with_reason(self):
        with patch("spotdl.utils.search.get_ytm_client") as mock_get_ytm_client:
            mock_get_ytm_client.return_value.get_song.return_value = {
                "playabilityStatus": {
                    "reason": "Sign in to confirm you're not a bot",
                }
            }

            with self.assertRaises(QueryError) as error:
                get_simple_songs(["https://music.youtube.com/watch?v=JDVRv15xqRI"])

        self.assertIn("Sign in to confirm you're not a bot", str(error.exception))

    def test_youtu_be_link_resolves_as_single_song(self):
        song_stub = Song.from_missing_data(
            name="Placeholder",
            artist="Placeholder Artist",
            artists=["Placeholder Artist"],
        )

        with patch("spotdl.utils.search.get_ytm_client") as mock_get_ytm_client:
            mock_get_ytm_client.return_value.get_song.return_value = {
                "videoDetails": {
                    "title": "Test Song",
                    "author": "Test Artist",
                    "lengthSeconds": "187",
                }
            }

            with patch(
                "spotdl.utils.search.Song.from_search_term",
                return_value=song_stub,
            ) as mock_from_search:
                songs = get_simple_songs(
                    ["https://youtu.be/9jI2CZ0bMuM?si=IF5YsIBvKQFYCsrH"],
                )

        mock_from_search.assert_called_once_with("Test Artist - Test Song")
        self.assertEqual(len(songs), 1)
        self.assertEqual(
            songs[0].download_url,
            "https://www.youtube.com/watch?v=9jI2CZ0bMuM",
        )

    def test_direct_youtube_watch_url_adds_music_watch_retry(self):
        self.assertEqual(
            Downloader._youtube_alternate_watch_urls(
                "https://www.youtube.com/watch?v=9jI2CZ0bMuM"
            ),
            ["https://music.youtube.com/watch?v=9jI2CZ0bMuM"],
        )

    def test_direct_music_watch_url_adds_regular_youtube_retry(self):
        self.assertEqual(
            Downloader._youtube_alternate_watch_urls(
                "https://music.youtube.com/watch?v=9jI2CZ0bMuM"
            ),
            ["https://www.youtube.com/watch?v=9jI2CZ0bMuM"],
        )

    def test_short_youtube_url_adds_both_watch_variants(self):
        self.assertEqual(
            Downloader._youtube_alternate_watch_urls("https://youtu.be/9jI2CZ0bMuM"),
            [
                "https://music.youtube.com/watch?v=9jI2CZ0bMuM",
                "https://www.youtube.com/watch?v=9jI2CZ0bMuM",
            ],
        )

    def test_audio_provider_accepts_js_runtime_and_ejs_remote_component_args(self):
        provider = DummyAudioProvider(
            yt_dlp_args="--js-runtimes node --remote-components ejs:github"
        )

        self.assertIn("node", provider.audio_handler.params["js_runtimes"])
        self.assertIn("ejs:github", provider.audio_handler.params["remote_components"])

    def test_hosted_direct_youtube_links_prefer_search_before_direct_download(self):
        downloader = Downloader.__new__(Downloader)
        song = Song.from_missing_data(
            name="Airplane pt.2",
            artist="BTS",
            artists=["BTS"],
            download_url="https://www.youtube.com/watch?v=CxnJf0tWu48",
        )

        with patch.dict(
            "os.environ",
            {"SONGZIP_DIRECT_YOUTUBE_SEARCH_FIRST": "true"},
            clear=False,
        ):
            self.assertTrue(downloader._should_search_before_direct_download(song))

    def test_local_direct_youtube_links_do_not_prefer_search_without_flag(self):
        downloader = Downloader.__new__(Downloader)
        song = Song.from_missing_data(
            name="Airplane pt.2",
            artist="BTS",
            artists=["BTS"],
            download_url="https://www.youtube.com/watch?v=CxnJf0tWu48",
        )

        with patch.dict(
            "os.environ",
            {"SONGZIP_DIRECT_YOUTUBE_SEARCH_FIRST": "false"},
            clear=False,
        ):
            self.assertFalse(downloader._should_search_before_direct_download(song))

    def test_album_match_handles_missing_song_album_name(self):
        song = Song.from_missing_data(
            name="Airplane pt.2",
            artist="BTS",
            artists=["BTS"],
        )
        result = Result(
            source="YouTubeMusic",
            url="https://music.youtube.com/watch?v=RlgSeFSGXes",
            verified=True,
            name="Airplane pt.2 (Japanese ver.)",
            duration=228,
            author="BTS",
            result_id="RlgSeFSGXes",
            artists=("BTS",),
            album="FACE YOURSELF",
        )

        self.assertEqual(calc_album_match(song, result), 0.0)

    def test_direct_youtube_metadata_candidates_prefer_ytm_song_urls(self):
        downloader = Downloader.__new__(Downloader)
        song = Song.from_missing_data(
            name="Airplane pt.2 (Japanese ver.)",
            artist="BTS",
            artists=["BTS"],
            download_url="https://www.youtube.com/watch?v=CxnJf0tWu48",
        )

        with patch("spotdl.download.downloader.get_ytm_client") as mock_get_ytm_client:
            mock_get_ytm_client.return_value.search.side_effect = [
                [
                    {"videoId": "RlgSeFSGXes"},
                    {"videoId": "3J9G-PWo2oE"},
                ],
                [
                    {"videoId": "CxnJf0tWu48"},
                ],
            ]

            self.assertEqual(
                downloader._direct_youtube_metadata_candidate_urls(song),
                [
                    "https://music.youtube.com/watch?v=RlgSeFSGXes",
                    "https://music.youtube.com/watch?v=3J9G-PWo2oE",
                    "https://www.youtube.com/watch?v=CxnJf0tWu48",
                ],
            )

    def test_youtube_link_falls_back_to_direct_metadata_when_spotify_lookup_fails(self):
        with patch("spotdl.utils.search.get_ytm_client") as mock_get_ytm_client:
            mock_get_ytm_client.return_value.get_song.return_value = {
                "videoDetails": {
                    "title": "Fallback Song",
                    "author": "Fallback Artist",
                    "lengthSeconds": "245",
                    "thumbnail": {"thumbnails": [{"url": "https://img.test/cover.jpg"}]},
                }
            }

            with patch(
                "spotdl.utils.search.Song.from_search_term",
                side_effect=QueryError("spotify unavailable"),
            ):
                songs = get_simple_songs(
                    ["https://www.youtube.com/watch?v=9jI2CZ0bMuM"],
                )

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0].name, "Fallback Song")
        self.assertEqual(songs[0].artist, "Fallback Artist")
        self.assertEqual(songs[0].duration, 245)
        self.assertEqual(
            songs[0].download_url,
            "https://www.youtube.com/watch?v=9jI2CZ0bMuM",
        )

    def test_best_result_ignores_view_lookup_failures(self):
        provider = DummyAudioProvider(output_format="mp3")
        results = {
            Result(
                source="YouTubeMusic",
                url="https://music.youtube.com/watch?v=one",
                verified=True,
                name="Song One",
                duration=100,
                author="Artist",
                result_id="one",
            ): 91.0,
            Result(
                source="YouTubeMusic",
                url="https://music.youtube.com/watch?v=two",
                verified=True,
                name="Song Two",
                duration=100,
                author="Artist",
                result_id="two",
            ): 90.0,
        }

        with patch.object(
            provider,
            "get_views",
            side_effect=AudioProviderError("rate limited"),
        ):
            result, score = provider.get_best_result(results)

        self.assertEqual(result.url, "https://music.youtube.com/watch?v=one")
        self.assertEqual(score, 91.0)

    def test_youtube_handle_artist_falls_back_to_search_result(self):
        fallback_artist_id = "UCCStjML8gy9D1ZDrSNFuy0A"
        mock_artist_payload = {
            "name": "Vahtang beatbox",
            "albums": {"results": []},
            "singles": {"results": []},
            "songs": {
                "results": [
                    {
                        "title": "Live Loop",
                        "videoId": "video-1",
                        "artists": [
                            {"name": "Vahtang beatbox", "id": fallback_artist_id}
                        ],
                        "duration": "1:05",
                    }
                ]
            },
            "thumbnails": [],
        }

        with patch("spotdl.utils.search.get_ytm_client") as mock_get_ytm_client:
            mock_client = mock_get_ytm_client.return_value
            mock_client.get_artist.side_effect = [
                KeyError("musicImmersiveHeaderRenderer"),
                mock_artist_payload,
            ]
            mock_client.search.side_effect = [
                [
                    {
                        "artist": "Vahtang beatbox",
                        "browseId": fallback_artist_id,
                    }
                ],
                [],
            ]
            mock_client.get_album.return_value = None

            with patch(
                "spotdl.utils.search._resolve_youtube_artist_browse_id",
                return_value="UC8s2D9Kp5S0fZ0Cxb11oDCA",
            ), patch(
                "spotdl.utils.search._get_youtube_artist_search_terms",
                return_value=["Vahtang beatbox"],
            ):
                artist = create_ytm_artist(
                    "https://www.youtube.com/@vahtang_beatbox",
                    fetch_songs=False,
                )

        self.assertEqual(artist.name, "Vahtang beatbox")
        self.assertEqual(
            artist.url,
            "https://music.youtube.com/channel/UCCStjML8gy9D1ZDrSNFuy0A",
        )
        self.assertEqual(len(artist.songs), 1)
        self.assertEqual(
            artist.songs[0].download_url,
            "https://music.youtube.com/watch?v=video-1",
        )


if __name__ == "__main__":
    unittest.main()
