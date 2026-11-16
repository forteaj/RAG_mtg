"""Download a few named English cards and their rulings. No AWS calls.

Keeps dated raw snapshots, reuses them by default and merges cards.json by
oracle_id. Use Scryfall bulk data instead for a whole-card-database import.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import UUID


ROOT = Path(__file__).resolve().parent
DEFAULT_CARDS = ("Blood Artist", "Rest in Peace", "Wrath of God")


def canonical_hash(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Scryfall:
    def __init__(self):
        self.last_request = None
        self.requests = 0

    def get(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.scryfall.com":
            raise ValueError("Expected an HTTPS URL on api.scryfall.com.")
        if self.last_request is not None:
            sleep(max(0, 0.55 - (monotonic() - self.last_request)))
        self.last_request = monotonic()
        self.requests += 1
        request = Request(url, headers={
            "User-Agent": "CommanderRAG/0.1 (local rules research prototype)",
            "Accept": "application/json;q=0.9,*/*;q=0.8",
        })
        try:
            with urlopen(request, timeout=30) as response:
                value = json.load(response)
        except HTTPError as exc:
            if exc.code == 429:
                raise RuntimeError("Scryfall rate limited the request. Stopping; retry later.") from exc
            raise RuntimeError(f"Scryfall returned HTTP {exc.code} for {url}") from exc
        if not isinstance(value, dict) or value.get("object") == "error":
            raise ValueError(f"Unexpected Scryfall response: {value.get('details') if isinstance(value, dict) else type(value).__name__}")
        return value


def normalize(bundle, snapshot):
    card = bundle["card_response"]["payload"]
    if card.get("object") != "card":
        raise ValueError("The named lookup did not return a card.")
    oracle_id = str(UUID(card["oracle_id"]))
    card_id = str(UUID(card["id"]))
    if card.get("lang") != "en":
        raise ValueError("This prototype currently accepts English card data only.")
    faces = [{key: face.get(key) for key in (
        "name", "type_line", "mana_cost", "oracle_text", "power", "toughness", "loyalty",
    )} for face in card.get("card_faces", [])]
    if not isinstance(card.get("oracle_text"), str) and not faces:
        raise ValueError(f"Oracle text/faces missing for {card.get('name')}.")
    rulings = {}
    for page in bundle["ruling_responses"]:
        if page["payload"].get("object") != "list":
            raise ValueError("The rulings endpoint did not return a list.")
        for ruling in page["payload"]["data"]:
            if ruling.get("oracle_id") != oracle_id:
                raise ValueError("A ruling belongs to another oracle_id.")
            if not isinstance(ruling.get("comment"), str) or not ruling["comment"].strip():
                raise ValueError("A ruling has no text.")
            core = {key: ruling[key] for key in ("oracle_id", "source", "published_at", "comment")}
            key = canonical_hash(core)
            rulings[key] = {
                "source_id": f"ruling:{oracle_id}:{key[:16]}", **core,
                "source_url": card["scryfall_uri"],
            }
    return {
        "source_id": f"card:{oracle_id}", "oracle_id": oracle_id,
        "card_id": card_id, "name": card["name"],
        "layout": card["layout"], "type_line": card.get("type_line"),
        "mana_cost": card.get("mana_cost"), "oracle_text": card.get("oracle_text"),
        "card_faces": faces, "keywords": card.get("keywords", []),
        "source_url": card["scryfall_uri"],
        "fetched_at_utc": bundle["fetched_at_utc"],
        "snapshot_file": snapshot.relative_to(ROOT).as_posix(),
        "snapshot_sha256": canonical_hash(bundle),
        "rulings": sorted(rulings.values(), key=lambda row: (row["published_at"], row["source_id"])),
    }


def fetch_card(name, client, refresh=False, offline=False):
    directory = ROOT / "data/raw/scryfall"
    name_key = hashlib.sha256(name.strip().casefold().encode("utf-8")).hexdigest()[:16]
    pointer = directory / f"name_{name_key}.json"
    if pointer.exists() and not refresh:
        filename = json.loads(pointer.read_text(encoding="utf-8"))["snapshot"]
        if Path(filename).name != filename:
            raise ValueError("Invalid snapshot pointer.")
        snapshot = directory / filename
        bundle = json.loads(snapshot.read_text(encoding="utf-8"))
        if canonical_hash(bundle) != snapshot.stem:
            raise ValueError("Snapshot hash mismatch.")
        return normalize(bundle, snapshot), True
    if offline:
        raise ValueError(f"No cached snapshot for {name!r}.")
    url = "https://api.scryfall.com/cards/named?" + urlencode({"exact": name})
    card = client.get(url)
    pages = []
    rulings_url = card["rulings_uri"]
    visited = set()
    while rulings_url:
        if rulings_url in visited or len(visited) >= 20:
            raise ValueError("Invalid/overlong rulings pagination.")
        visited.add(rulings_url)
        page = client.get(rulings_url)
        pages.append({"url": rulings_url, "payload": page})
        if page.get("has_more") and not page.get("next_page"):
            raise ValueError("A rulings page is missing next_page.")
        rulings_url = page["next_page"] if page.get("has_more") else None
    bundle = {
        "schema_version": 1, "requested_name": name,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "card_response": {"url": url, "payload": card},
        "ruling_responses": pages,
    }
    snapshot = directory / f"{canonical_hash(bundle)}.json"
    normalized = normalize(bundle, snapshot)
    save_json(snapshot, bundle)
    save_json(pointer, {"snapshot": snapshot.name})
    return normalized, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", action="append", help="Exact English name; repeat to add cards")
    parser.add_argument("--refresh", action="store_true", help="Fetch a new snapshot and preserve the old one")
    parser.add_argument("--offline", action="store_true", help="Only use cached snapshots")
    args = parser.parse_args()
    if args.refresh and args.offline:
        raise ValueError("Choose --refresh or --offline, not both.")
    output = ROOT / "data/processed/cards.json"
    cards = {}
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing["metadata"]["schema_version"] != 1 or existing["metadata"]["provider"] != "Scryfall":
            raise ValueError("The existing cards.json has another format.")
        cards = {card["oracle_id"]: card for card in existing["cards"]}
    client = Scryfall()
    for name in dict.fromkeys(args.card or DEFAULT_CARDS):
        card, cached = fetch_card(name, client, args.refresh, args.offline)
        cards[card["oracle_id"]] = card
        official = sum(row["source"] == "wotc" for row in card["rulings"])
        print(f"{card['name']}: {'cache' if cached else 'downloaded'} | {len(card['rulings'])} rulings ({official} from Wizards)", flush=True)
    save_json(output, {
        "metadata": {"schema_version": 1, "provider": "Scryfall",
                     "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                     "card_count": len(cards), "api_documentation": "https://scryfall.com/docs/api"},
        "cards": sorted(cards.values(), key=lambda row: row["name"]),
    })
    print(f"\nCards available: {len(cards)} | New HTTP requests: {client.requests}")
    print(f"Output: {output}")
    print("No AWS calls were made.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"ERROR ({type(exc).__name__}): {exc}") from None
