"""
SongZip artist discovery scout.

This tool builds a resumable research queue of artists for a genre set. It does
not download media. A downloader-ready queue can be exported only with an
explicit rights confirmation flag.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


DEFAULT_GENRES = [
    "dubstep",
    "riddim",
    "tearout dubstep",
    "brostep",
    "color bass",
    "future riddim",
    "experimental bass",
    "deathstep",
    "neurofunk",
    "bass music",
]

SEED_ARTISTS = [
    ("Skrillex", "dubstep"),
    ("Excision", "dubstep"),
    ("Zeds Dead", "dubstep"),
    ("Virtual Riot", "riddim"),
    ("Subtronics", "riddim"),
    ("Svdden Death", "riddim"),
    ("Marauda", "tearout dubstep"),
    ("Wooli", "dubstep"),
    ("Infekt", "riddim"),
    ("Eptic", "dubstep"),
    ("Barely Alive", "dubstep"),
    ("PhaseOne", "dubstep"),
    ("Leotrix", "future riddim"),
    ("Ace Aura", "color bass"),
    ("Chime", "color bass"),
    ("G Jones", "experimental bass"),
    ("EPROM", "experimental bass"),
    ("Space Laces", "dubstep"),
    ("Getter", "dubstep"),
    ("Dion Timmer", "dubstep"),
    ("Kai Wachi", "dubstep"),
    ("HOL!", "riddim"),
    ("Crankdat", "dubstep"),
    ("Ray Volpe", "dubstep"),
    ("Nimda", "tearout dubstep"),
    ("Samplifire", "riddim"),
    ("MUST DIE!", "dubstep"),
    ("Moody Good", "dubstep"),
    ("TYNAN", "experimental bass"),
    ("Effin", "dubstep"),
]

VALID_STATUSES = {"pending", "scanning", "done", "partial", "blocked", "skipped"}


@dataclass
class ArtistCandidate:
    """A normalized artist candidate in the scout queue."""

    rank: int
    name: str
    genre: str
    query: str
    source: str
    score: float
    browse_id: str = ""
    status: str = "pending"
    notes: str = ""
    discovered_at: str = ""
    updated_at: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "ArtistCandidate":
        return cls(
            rank=int(payload.get("rank") or 0),
            name=str(payload.get("name") or "").strip(),
            genre=str(payload.get("genre") or "").strip(),
            query=str(payload.get("query") or "").strip(),
            source=str(payload.get("source") or "").strip(),
            score=float(payload.get("score") or 0),
            browse_id=str(payload.get("browse_id") or "").strip(),
            status=str(payload.get("status") or "pending").strip().lower(),
            notes=str(payload.get("notes") or ""),
            discovered_at=str(payload.get("discovered_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def parse_genres(value: str | Sequence[str]) -> List[str]:
    if isinstance(value, str):
        raw_parts = re.split(r"[,;\n]+", value)
    else:
        raw_parts = []
        for item in value:
            raw_parts.extend(re.split(r"[,;\n]+", str(item)))

    genres: List[str] = []
    seen = set()
    for part in raw_parts:
        genre = re.sub(r"\s+", " ", part.strip()).lower()
        if not genre or genre in seen:
            continue
        seen.add(genre)
        genres.append(genre)
    return genres or list(DEFAULT_GENRES)


def parse_subscriber_count(value: Any) -> int:
    if value is None:
        return 0

    text = str(value).lower().replace(",", "").strip()
    match = re.search(r"([\d.]+)\s*([kmb])?", text)
    if not match:
        return 0

    number = float(match.group(1))
    suffix = match.group(2)
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def score_candidate(rank_offset: int, position: int, subscribers: int = 0) -> float:
    base = max(0, 10_000 - rank_offset * 400 - position * 90)
    subscriber_bonus = min(2_500, subscribers / 1_000)
    return round(base + subscriber_bonus, 2)


def discover_from_ytmusic(genres: Sequence[str], limit: int) -> List[ArtistCandidate]:
    """Discover artist metadata through ytmusicapi search."""

    try:
        from ytmusicapi import YTMusic
    except Exception as exc:  # pragma: no cover - import failure depends on env
        raise RuntimeError("ytmusicapi is not installed in this environment") from exc

    ytmusic = YTMusic()
    discovered: List[ArtistCandidate] = []
    now = utc_now()

    for genre_index, genre in enumerate(genres):
        search_terms = [
            f"{genre} artists",
            f"{genre} music",
            genre,
        ]
        for term_index, term in enumerate(search_terms):
            try:
                results = ytmusic.search(term, filter="artists", limit=20)
            except Exception as exc:
                print(f"Warning: could not search '{term}': {exc}", file=sys.stderr)
                continue

            for position, result in enumerate(results):
                name = str(result.get("artist") or result.get("name") or "").strip()
                if not name:
                    continue
                subscribers = parse_subscriber_count(result.get("subscribers"))
                discovered.append(
                    ArtistCandidate(
                        rank=0,
                        name=name,
                        genre=genre,
                        query=f"ytartist: {name}",
                        source="ytmusic-search",
                        score=score_candidate(genre_index + term_index, position, subscribers),
                        browse_id=str(result.get("browseId") or ""),
                        discovered_at=now,
                        updated_at=now,
                    )
                )
                if len(discovered) >= limit * 3:
                    break
            if len(discovered) >= limit * 3:
                break
        if len(discovered) >= limit * 3:
            break

    return discovered


def discover_from_seed(genres: Sequence[str]) -> List[ArtistCandidate]:
    now = utc_now()
    genre_set = {genre.casefold() for genre in genres}
    candidates: List[ArtistCandidate] = []
    for index, (name, genre) in enumerate(SEED_ARTISTS):
        if genre_set and genre.casefold() not in genre_set and not any(
            token in genre.casefold() for token in genre_set
        ):
            continue
        candidates.append(
            ArtistCandidate(
                rank=0,
                name=name,
                genre=genre,
                query=f"ytartist: {name}",
                source="curated-seed",
                score=score_candidate(0, index),
                discovered_at=now,
                updated_at=now,
            )
        )
    return candidates


def dedupe_candidates(candidates: Iterable[ArtistCandidate], limit: int) -> List[ArtistCandidate]:
    best_by_name: Dict[str, ArtistCandidate] = {}
    for candidate in candidates:
        key = normalize_name(candidate.name)
        if not key:
            continue
        current = best_by_name.get(key)
        if current is None or candidate.score > current.score:
            best_by_name[key] = candidate

    ranked = sorted(best_by_name.values(), key=lambda item: (-item.score, normalize_name(item.name)))
    for index, candidate in enumerate(ranked[:limit], start=1):
        candidate.rank = index
    return ranked[:limit]


def build_queue(genres: Sequence[str], limit: int, offline: bool = False) -> Dict[str, Any]:
    discovered: List[ArtistCandidate] = []
    if not offline:
        try:
            discovered.extend(discover_from_ytmusic(genres, limit))
        except RuntimeError as exc:
            print(f"Warning: {exc}. Falling back to curated seeds.", file=sys.stderr)

    discovered.extend(discover_from_seed(genres))
    artists = dedupe_candidates(discovered, limit)
    now = utc_now()
    return {
        "schema": 1,
        "created_at": now,
        "updated_at": now,
        "genres": list(genres),
        "limit": limit,
        "artists": [artist.to_json() for artist in artists],
    }


def load_queue(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["artists"] = [
        ArtistCandidate.from_json(item).to_json()
        for item in payload.get("artists", [])
        if str(item.get("name") or "").strip()
    ]
    return payload


def save_queue(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = utc_now()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def next_artist(payload: Dict[str, Any]) -> Optional[ArtistCandidate]:
    for item in payload.get("artists", []):
        candidate = ArtistCandidate.from_json(item)
        if candidate.status == "pending":
            return candidate
    return None


def update_artist_status(
    payload: Dict[str, Any],
    artist_name: str,
    status: str,
    notes: str = "",
) -> ArtistCandidate:
    normalized_status = status.strip().lower()
    if normalized_status not in VALID_STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(sorted(VALID_STATUSES))}")

    target = normalize_name(artist_name)
    for item in payload.get("artists", []):
        candidate = ArtistCandidate.from_json(item)
        if normalize_name(candidate.name) == target or str(candidate.rank) == artist_name.strip():
            candidate.status = normalized_status
            candidate.notes = notes or candidate.notes
            candidate.updated_at = utc_now()
            item.update(candidate.to_json())
            return candidate

    raise ValueError(f"Could not find artist in queue: {artist_name}")


def export_download_queue(payload: Dict[str, Any], path: Path, rights_confirmed: bool) -> int:
    if not rights_confirmed:
        raise ValueError(
            "Export blocked. Re-run with --i-have-rights after confirming every exported artist/source is allowed."
        )

    pending = [
        ArtistCandidate.from_json(item)
        for item in payload.get("artists", [])
        if ArtistCandidate.from_json(item).status in {"pending", "partial"}
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# SongZip approved artist queue",
        "# Only use this file for artists/tracks you have permission to process.",
        "# Generated by tools/songzip_artist_scout.py",
        "",
    ]
    for candidate in pending:
        lines.append(f"# {candidate.rank}. {candidate.name} [{candidate.genre}]")
        lines.append(candidate.query)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return len(pending)


def print_status(payload: Dict[str, Any]) -> None:
    artists = [ArtistCandidate.from_json(item) for item in payload.get("artists", [])]
    counts = {status: 0 for status in sorted(VALID_STATUSES)}
    for artist in artists:
        counts[artist.status] = counts.get(artist.status, 0) + 1

    print(f"Artists: {len(artists)}")
    print(", ".join(f"{status}={count}" for status, count in counts.items() if count))
    upcoming = [artist for artist in artists if artist.status == "pending"][:10]
    if upcoming:
        print("")
        print("Next up:")
        for artist in upcoming:
            print(f"  {artist.rank}. {artist.name} ({artist.genre})")


def build_parser() -> argparse.ArgumentParser:
    default_queue = Path(".spotdl-tools/artist-scout/artist_scout_queue.json")
    parser = argparse.ArgumentParser(description="Build and manage a SongZip artist discovery queue.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Discover artists for a genre set.")
    discover.add_argument("--genres", default=",".join(DEFAULT_GENRES))
    discover.add_argument("--limit", type=int, default=100)
    discover.add_argument("--queue", type=Path, default=default_queue)
    discover.add_argument("--offline", action="store_true", help="Use curated seeds only.")

    status = subparsers.add_parser("status", help="Show queue progress.")
    status.add_argument("--queue", type=Path, default=default_queue)

    next_cmd = subparsers.add_parser("next", help="Show the next pending artist.")
    next_cmd.add_argument("--queue", type=Path, default=default_queue)

    mark = subparsers.add_parser("mark", help="Mark an artist status.")
    mark.add_argument("--queue", type=Path, default=default_queue)
    mark.add_argument("--artist", required=True, help="Artist name or rank.")
    mark.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    mark.add_argument("--notes", default="")

    export = subparsers.add_parser("export", help="Export an approved downloader queue.")
    export.add_argument("--queue", type=Path, default=default_queue)
    export.add_argument("--output", type=Path, default=Path.home() / "Documents" / "artist in order downloads.txt")
    export.add_argument("--i-have-rights", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "discover":
        genres = parse_genres(args.genres)
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        payload = build_queue(genres=genres, limit=args.limit, offline=args.offline)
        save_queue(args.queue, payload)
        print(f"Saved {len(payload.get('artists', []))} artists to {args.queue}")
        return 0

    payload = load_queue(args.queue)

    if args.command == "status":
        print_status(payload)
        return 0

    if args.command == "next":
        artist = next_artist(payload)
        if artist is None:
            print("No pending artists remain.")
            return 0
        print(f"{artist.rank}. {artist.name} ({artist.genre})")
        print(artist.query)
        return 0

    if args.command == "mark":
        artist = update_artist_status(payload, args.artist, args.status, args.notes)
        save_queue(args.queue, payload)
        print(f"Marked {artist.name} as {artist.status}.")
        return 0

    if args.command == "export":
        count = export_download_queue(payload, args.output, args.i_have_rights)
        print(f"Exported {count} approved artist entries to {args.output}")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
