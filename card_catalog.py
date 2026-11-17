"""Scryfall bulk download, atomic SQLite import and small legacy-RAG contexts.

No AWS calls or third-party dependencies. Current JSONL/GZIP exports are streamed;
legacy JSON arrays are supported too (loaded in memory).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "indexes" / "cards.sqlite"
BULK_DIR = ROOT / "data" / "raw" / "scryfall" / "bulk"
SCHEMA_VERSION = 1
NORMALIZER_VERSION = "1.1.0"
USER_AGENT = "CommanderRAG/0.2 (local educational MTG rules project)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json;q=0.9,*/*;q=0.8"}


class CatalogError(Exception):
    pass


class CardNameError(CatalogError):
    pass


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def name_key(name):
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_json(value))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_download_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "api.scryfall.com" or host == "scryfall.io" or host.endswith(".scryfall.io")):
        raise CatalogError("Unexpected Scryfall download URL.")


def open_scryfall(url):
    validate_download_url(url)
    # Bounded retries for transient failures; never repeatedly retry a 403/429.
    for attempt in range(3):
        try:
            response = urlopen(Request(url, headers=HEADERS), timeout=90)
            validate_download_url(response.geturl())
            return response
        except HTTPError as exc:
            if exc.code == 429:
                raise CatalogError("Scryfall returned HTTP 429. Wait before retrying; no automatic retry was made.") from exc
            if exc.code not in (500, 502, 503, 504) or attempt == 2:
                raise CatalogError(f"Scryfall returned HTTP {exc.code}. Check your connection and retry later.") from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 2:
                raise CatalogError(f"Cannot connect to Scryfall: {exc}") from exc
        time.sleep(attempt + 1)


def download_bulk(entry, destination, refresh=False):
    kind = entry["type"]
    url = entry.get("jsonl_download_uri") or entry.get("download_uri")
    if not isinstance(url, str):
        raise CatalogError(f"No supported download URI for {kind}.")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    version = hashlib.sha256((str(entry.get("updated_at")) + url).encode()).hexdigest()[:16]
    path = destination / f"{kind}_{version}.bulk"
    manifest_path = path.with_suffix(".manifest.json")
    if not refresh and path.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sha256_file(path) == manifest.get("sha256"):
            print(f"Reusing verified download: {path.name}", flush=True)
            return path, manifest
    print(f"Downloading {kind} (snapshot {entry.get('updated_at')})...", flush=True)
    temporary = path.with_suffix(".part")
    try:
        with open_scryfall(url) as response, temporary.open("wb") as handle:
            digest = hashlib.sha256()
            total = 0
            last_report = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                if total - last_report >= 20 * 1024 * 1024:
                    print(f"  {kind}: {total / 1024**2:.1f} MiB downloaded", flush=True)
                    last_report = total
            expected = response.headers.get("Content-Length")
            if expected and total != int(expected):
                raise CatalogError(f"Incomplete {kind} download.")
        if not total:
            raise CatalogError(f"Empty {kind} download.")
        manifest = {"type": kind, "updated_at": entry.get("updated_at"), "download_uri": url,
                    "fetched_at_utc": now_utc(), "sha256": digest.hexdigest(), "bytes": total}
        os.replace(temporary, path)
        write_json_atomic(manifest_path, manifest)
        print(f"  Saved {total / 1024**2:.1f} MiB; SHA256 {digest.hexdigest()[:16]}", flush=True)
        return path, manifest
    finally:
        temporary.unlink(missing_ok=True)


def iter_objects(path):
    """Detect compression/content, not filename suffix; stream JSONL records."""
    path = Path(path)
    with path.open("rb") as handle:
        compressed = handle.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    with opener(path, "rt", encoding="utf-8-sig") as handle:
        first = ""
        while not first:
            chunk = handle.read(1)
            if not chunk:
                raise CatalogError(f"Empty bulk file: {path.name}")
            if not chunk.isspace():
                first = chunk
        handle.seek(0)
        if first == "[":
            values = json.load(handle)
            for value in values:
                if not isinstance(value, dict):
                    raise CatalogError("Bulk array items must be JSON objects.")
                yield value
        elif first == "{":
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CatalogError(f"Invalid JSONL at {path.name}:{line_number}") from exc
                if not isinstance(value, dict):
                    raise CatalogError("JSONL items must be JSON objects.")
                yield value
        else:
            raise CatalogError(f"Unsupported bulk content in {path.name}.")


def normalized_card(raw, snapshot):
    oracle_id, card_id, name = raw.get("oracle_id"), raw.get("id"), raw.get("name")
    if not all(isinstance(value, str) and value.strip() for value in (oracle_id, card_id, name)):
        raise CatalogError("Card is missing oracle_id, id or name.")
    faces = raw.get("card_faces") or []
    if not isinstance(faces, list) or any(not isinstance(face, dict) or not face.get("name") for face in faces):
        raise CatalogError(f"Invalid card faces: {name}")
    card = {
        "source_id": f"card:{oracle_id}", "oracle_id": oracle_id, "card_id": card_id,
        "name": name, "layout": raw.get("layout", "normal"),
        "type_line": raw.get("type_line", ""), "mana_cost": raw.get("mana_cost", ""),
        "oracle_text": raw.get("oracle_text", ""),
        "card_faces": [{key: face[key] for key in ("name", "mana_cost", "type_line", "oracle_text", "power", "toughness", "loyalty", "defense", "colors") if key in face} for face in faces],
        "keywords": raw.get("keywords", []), "legalities": raw.get("legalities", {}),
        "source_url": raw.get("scryfall_uri") or f"https://scryfall.com/card/{card_id}",
        "fetched_at_utc": snapshot["fetched_at_utc"],
        "snapshot_file": snapshot["file"], "snapshot_sha256": snapshot["sha256"],
    }
    for key in ("power", "toughness", "loyalty", "defense", "colors", "color_identity", "cmc"):
        if key in raw:
            card[key] = raw[key]
    return card


def normalized_ruling(raw):
    required = ("oracle_id", "source", "published_at", "comment")
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
        raise CatalogError("Ruling is missing oracle_id, source, published_at or comment.")
    oracle_id = raw["oracle_id"]
    fingerprint = hashlib.sha256(canonical_json({key: raw[key] for key in required}).encode()).hexdigest()[:24]
    return {"source_id": f"ruling:{oracle_id}:{fingerprint}", **{key: raw[key] for key in required}}


def build_database(oracle_file, rulings_file, output=DEFAULT_DB, oracle_manifest=None, rulings_manifest=None):
    oracle_file, rulings_file, output = Path(oracle_file), Path(rulings_file), Path(output)
    oracle_sha, rulings_sha = sha256_file(oracle_file), sha256_file(rulings_file)
    for manifest, actual in ((oracle_manifest, oracle_sha), (rulings_manifest, rulings_sha)):
        if manifest and manifest.get("sha256") != actual:
            raise CatalogError("Bulk file hash does not match its manifest.")
    fingerprint = hashlib.sha256(canonical_json({"normalizer": NORMALIZER_VERSION, "oracle": oracle_sha, "rulings": rulings_sha}).encode()).hexdigest()
    if output.is_file():
        try:
            existing = stats(output)
        except CatalogError:
            existing = {}
        if existing.get("catalog_sha256") == fingerprint:
            print("Catalog already matches both snapshots; keeping the existing database and context hashes.", flush=True)
            return existing
    created = now_utc()
    oracle_snapshot = {"sha256": oracle_sha, "file": str(oracle_file.resolve()), "fetched_at_utc": (oracle_manifest or {}).get("fetched_at_utc", created)}
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="cards_build_", suffix=".sqlite", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    db = None
    try:
        db = sqlite3.connect(temporary)
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE cards (oracle_id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE aliases (name_key TEXT NOT NULL, oracle_id TEXT NOT NULL REFERENCES cards(oracle_id), is_primary INTEGER NOT NULL, PRIMARY KEY(name_key, oracle_id));
            CREATE TABLE rulings (source_id TEXT PRIMARY KEY, oracle_id TEXT NOT NULL REFERENCES cards(oracle_id), payload TEXT NOT NULL);
            CREATE INDEX rulings_oracle ON rulings(oracle_id);
        """)
        cards_count = skipped = skipped_art_series = 0
        print("Importing Oracle cards...", flush=True)
        for raw in iter_objects(oracle_file):
            # Art Series objects are illustrations, not the game cards they depict.
            if raw.get("layout") == "art_series":
                skipped_art_series += 1
                continue
            if not raw.get("oracle_id"):
                skipped += 1
                continue
            card = normalized_card(raw, oracle_snapshot)
            try:
                db.execute("INSERT INTO cards VALUES (?, ?, ?)", (card["oracle_id"], card["name"], canonical_json(card)))
            except sqlite3.IntegrityError as exc:
                raise CatalogError("Duplicate Oracle ID: use the oracle_cards bulk export, not all_cards/default_cards.") from exc
            aliases = {name_key(face["name"]): 0 for face in card["card_faces"]}
            aliases[name_key(card["name"])] = 1
            db.executemany("INSERT INTO aliases VALUES (?, ?, ?)", [(alias, card["oracle_id"], primary) for alias, primary in aliases.items()])
            cards_count += 1
            if cards_count % 10000 == 0:
                print(f"  {cards_count:,} cards", flush=True)
        if not cards_count:
            raise CatalogError("No Oracle cards were imported.")
        print("Importing rulings...", flush=True)
        ruling_count = unmatched = duplicates = 0
        for raw in iter_objects(rulings_file):
            ruling = normalized_ruling(raw)
            if db.execute("SELECT 1 FROM cards WHERE oracle_id=?", (ruling["oracle_id"],)).fetchone() is None:
                unmatched += 1
                continue
            changed = db.execute("INSERT OR IGNORE INTO rulings VALUES (?, ?, ?)", (ruling["source_id"], ruling["oracle_id"], canonical_json(ruling))).rowcount
            ruling_count += changed
            duplicates += 1 - changed
        if not ruling_count:
            raise CatalogError("No matching rulings were imported. Existing database was preserved.")
        metadata = {
            "schema_version": SCHEMA_VERSION, "normalizer_version": NORMALIZER_VERSION,
            "provider": "Scryfall", "created_at_utc": created, "catalog_sha256": fingerprint,
            "card_count": cards_count, "ruling_count": ruling_count,
            "skipped_without_oracle_id": skipped, "unmatched_rulings": unmatched, "duplicate_rulings": duplicates,
            "skipped_art_series": skipped_art_series,
            "oracle_sha256": oracle_sha, "rulings_sha256": rulings_sha,
            "oracle_updated_at": (oracle_manifest or {}).get("updated_at"),
            "rulings_updated_at": (rulings_manifest or {}).get("updated_at"),
            "oracle_download_uri": (oracle_manifest or {}).get("download_uri"),
            "rulings_download_uri": (rulings_manifest or {}).get("download_uri"),
        }
        db.executemany("INSERT INTO metadata VALUES (?, ?)", [(key, canonical_json(value)) for key, value in metadata.items()])
        db.commit()
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok" or db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CatalogError("SQLite integrity check failed.")
        db.close()
        db = None
        try:
            os.replace(temporary, output)
        except PermissionError as exc:
            raise CatalogError("Cannot replace the database. Stop Uvicorn and close SQLite viewers, then retry.") from exc
        print(f"Catalog ready: {cards_count:,} cards, {ruling_count:,} rulings. Database: {output}", flush=True)
        return metadata
    finally:
        if db is not None:
            db.close()
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-journal").unlink(missing_ok=True)


@contextmanager
def connect_catalog(path=DEFAULT_DB):
    path = Path(path)
    if not path.is_file():
        raise CatalogError("Card catalog is missing. Run: python card_catalog.py build")
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        metadata = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key, value FROM metadata")}
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise CatalogError("Unsupported card catalog schema. Rebuild it using card_catalog.py.")
        yield db, metadata
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise CatalogError("Card catalog is invalid or unreadable. Rebuild it using card_catalog.py.") from exc
    finally:
        if db is not None:
            db.close()


def stats(path=DEFAULT_DB):
    with connect_catalog(path) as (_, metadata):
        return metadata


def _lookup(db, name):
    key = name_key(name)
    rows = db.execute("SELECT c.oracle_id,c.name,c.payload,a.is_primary FROM aliases a JOIN cards c USING(oracle_id) WHERE a.name_key=? ORDER BY a.is_primary DESC,c.name", (key,)).fetchall()
    if not rows:
        rows = db.execute("SELECT oracle_id,name,payload,1 AS is_primary FROM cards WHERE oracle_id=?", (key,)).fetchall()
    if not rows:
        raise CardNameError(f"Unknown card: {name!r}. Use its English Oracle name.")
    primary = [row for row in rows if row["is_primary"]]
    matches = primary or rows
    if len(matches) != 1:
        raise CardNameError(f"Ambiguous face name {name!r}. Use the full card name: " + "; ".join(row["name"] for row in matches))
    card = json.loads(matches[0]["payload"])
    card["rulings"] = []
    for row in db.execute("SELECT payload FROM rulings WHERE oracle_id=? ORDER BY source_id", (card["oracle_id"],)):
        ruling = json.loads(row["payload"])
        ruling["source_url"] = card["source_url"]
        card["rulings"].append(ruling)
    card["rulings"].sort(key=lambda value: (value["published_at"], value["comment"], value["source"]))
    return card


def lookup_card(name, path=DEFAULT_DB):
    with connect_catalog(path) as (db, _):
        return _lookup(db, name)


def context_file(names, path=DEFAULT_DB, cache_dir=None):
    """Export just the requested cards in fetch_cards.py's legacy JSON schema.

    The existing engine loader remains responsible for rendering original sources
    and filtering WotC rulings. No new prompt or retrieval logic is introduced.
    """
    if not 1 <= len(names) <= 12:
        raise CardNameError("Provide between 1 and 12 reference cards.")
    with connect_catalog(path) as (db, metadata):
        cards = [_lookup(db, name) for name in names]
    if len({card["oracle_id"] for card in cards}) != len(cards):
        raise CardNameError("Two reference names resolve to the same card. List it only once.")
    document = {
        "metadata": {"schema_version": 1, "provider": "Scryfall", "generated_at_utc": metadata["created_at_utc"],
                     "card_count": len(cards), "api_documentation": "https://scryfall.com/docs/api", "catalog_sha256": metadata["catalog_sha256"]},
        "cards": cards,
    }
    digest = hashlib.sha256(canonical_json(document).encode()).hexdigest()
    directory = Path(cache_dir) if cache_dir is not None else ROOT / "data" / "cache" / "card_contexts"
    selected_path = directory / f"cards_{digest}.json"
    if not selected_path.is_file():
        write_json_atomic(selected_path, document)
    # Canonical full names are needed when the request used a face name.
    return selected_path, [card["name"] for card in cards], metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Download/reuse both current Scryfall bulk files and import SQLite")
    build.add_argument("--refresh", action="store_true", help="Download again even if a verified local snapshot exists")
    build.add_argument("--bulk-dir", type=Path, default=BULK_DIR)
    offline = commands.add_parser("import", help="Import already downloaded bulk files without network access")
    offline.add_argument("--oracle-file", type=Path, required=True)
    offline.add_argument("--rulings-file", type=Path, required=True)
    commands.add_parser("stats", help="Show local catalog counts and provenance; no AWS")
    lookup = commands.add_parser("lookup", help="Look up English full/face names or Oracle UUIDs; no AWS")
    lookup.add_argument("names", nargs="+")
    context = commands.add_parser("context", help="Export a small JSON compatible with the existing CLI engine")
    context.add_argument("--card", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            with open_scryfall("https://api.scryfall.com/bulk-data") as response:
                payload = json.load(response)
            entries = {entry["type"]: entry for entry in payload["data"]}
            if not {"oracle_cards", "rulings"}.issubset(entries):
                raise CatalogError("Scryfall's bulk index does not contain oracle_cards and rulings.")
            files, manifests = {}, {}
            for kind in ("oracle_cards", "rulings"):
                files[kind], manifests[kind] = download_bulk(entries[kind], args.bulk_dir, args.refresh)
            metadata = build_database(files["oracle_cards"], files["rulings"], args.db, manifests["oracle_cards"], manifests["rulings"])
            print(canonical_json(metadata))
        elif args.command == "import":
            build_database(args.oracle_file, args.rulings_file, args.db)
        elif args.command == "stats":
            print(json.dumps(stats(args.db), ensure_ascii=False, indent=2))
        elif args.command == "lookup":
            for name in args.names:
                print(json.dumps(lookup_card(name, args.db), ensure_ascii=False, indent=2))
        else:
            path, names, _ = context_file(args.card, args.db)
            print("Selected cards:", ", ".join(names))
            print("Context file:", path)
            print("For the existing CLI: add --cards-file", str(path))
    except (CatalogError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
