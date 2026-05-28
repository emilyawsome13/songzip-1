import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.songzip_artist_scout import (
    ArtistCandidate,
    dedupe_candidates,
    export_download_queue,
    next_artist,
    parse_genres,
    update_artist_status,
)


class ArtistScoutTest(unittest.TestCase):
    def test_parse_genres_dedupes_and_normalizes(self):
        self.assertEqual(
            parse_genres("Dubstep, riddim; DUBSTEP\ncolor bass"),
            ["dubstep", "riddim", "color bass"],
        )

    def test_dedupe_keeps_highest_scoring_artist(self):
        candidates = [
            ArtistCandidate(
                rank=0,
                name="Example Artist",
                genre="dubstep",
                query="ytartist: Example Artist",
                source="low",
                score=10,
            ),
            ArtistCandidate(
                rank=0,
                name="example artist",
                genre="riddim",
                query="ytartist: example artist",
                source="high",
                score=99,
            ),
        ]

        result = dedupe_candidates(candidates, limit=10)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "high")
        self.assertEqual(result[0].rank, 1)

    def test_next_artist_skips_non_pending_rows(self):
        payload = {
            "artists": [
                ArtistCandidate(1, "Done Artist", "dubstep", "ytartist: Done Artist", "test", 10, status="done").to_json(),
                ArtistCandidate(2, "Next Artist", "riddim", "ytartist: Next Artist", "test", 9).to_json(),
            ]
        }

        artist = next_artist(payload)

        self.assertIsNotNone(artist)
        self.assertEqual(artist.name, "Next Artist")

    def test_update_artist_status_supports_rank_lookup(self):
        payload = {
            "artists": [
                ArtistCandidate(7, "Ranked Artist", "dubstep", "ytartist: Ranked Artist", "test", 10).to_json()
            ]
        }

        artist = update_artist_status(payload, "7", "blocked", "not approved")

        self.assertEqual(artist.status, "blocked")
        self.assertEqual(payload["artists"][0]["notes"], "not approved")

    def test_export_requires_rights_confirmation(self):
        payload = {
            "artists": [
                ArtistCandidate(1, "Approved Artist", "dubstep", "ytartist: Approved Artist", "test", 10).to_json()
            ]
        }

        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "artists.txt"
            with self.assertRaises(ValueError):
                export_download_queue(payload, output, rights_confirmed=False)

    def test_export_writes_downloader_ready_queries_when_confirmed(self):
        payload = {
            "artists": [
                ArtistCandidate(1, "Approved Artist", "dubstep", "ytartist: Approved Artist", "test", 10).to_json(),
                ArtistCandidate(2, "Skipped Artist", "dubstep", "ytartist: Skipped Artist", "test", 9, status="skipped").to_json(),
            ]
        }

        with TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "artists.txt"
            count = export_download_queue(payload, output, rights_confirmed=True)

            self.assertEqual(count, 1)
            self.assertIn("ytartist: Approved Artist", output.read_text(encoding="utf-8"))
            self.assertNotIn("ytartist: Skipped Artist", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
