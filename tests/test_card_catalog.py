"""Local catalog and HTTP integration tests. All data here is synthetic."""
import copy
import gzip
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api import RulingService, Settings, create_app
from card_catalog import CardNameError, CatalogError, build_database, context_file, iter_objects, lookup_card, sha256_file, stats


def sample_card(oracle_id, name, **extra):
    return {"object": "card", "oracle_id": oracle_id, "id": "printing-" + oracle_id,
            "name": name, "type_line": "Creature", "oracle_text": "Fixture text.",
            "scryfall_uri": "https://scryfall.com/card/" + oracle_id, **extra}


CARDS = [
    sample_card("oracle-a", "Fixture Artist", power="0", toughness="1"),
    sample_card("oracle-b", "First Face // Second Face", layout="transform", card_faces=[
        {"name": "First Face", "type_line": "Creature", "oracle_text": "Front text.", "power": "1", "toughness": "2"},
        {"name": "Second Face", "type_line": "Creature", "oracle_text": "Back text.", "power": "3", "toughness": "4"},
    ]),
    sample_card("oracle-c", "Other Front // Second Face", layout="transform", card_faces=[
        {"name": "Other Front", "oracle_text": "Another front."},
        {"name": "Second Face", "oracle_text": "Another back."},
    ]),
]
RULINGS = [
    {"object": "ruling", "oracle_id": "oracle-a", "source": "wotc", "published_at": "2020-01-01", "comment": "Official fixture ruling."},
    {"object": "ruling", "oracle_id": "oracle-a", "source": "scryfall", "published_at": "2020-02-01", "comment": "Community fixture ruling."},
    {"object": "ruling", "oracle_id": "oracle-b", "source": "wotc", "published_at": "2020-03-01", "comment": "Face fixture ruling."},
]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.oracle = self.root / "oracle.jsonl.gz"
        self.rulings = self.root / "rulings.jsonl.gz"
        self.db = self.root / "cards.sqlite"
        self.write_records(self.oracle, CARDS)
        self.write_records(self.rulings, RULINGS)
        with redirect_stdout(io.StringIO()):
            self.metadata = build_database(self.oracle, self.rulings, self.db)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write_records(path, records):
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_counts_ruling_ownership_and_original_text(self):
        self.assertEqual(stats(self.db)["card_count"], 3)
        self.assertEqual(stats(self.db)["ruling_count"], 3)
        card = lookup_card("  FIXTURE   ARTIST ", self.db)
        self.assertEqual(card["oracle_text"], "Fixture text.")
        self.assertEqual(card["power"], "0")
        self.assertEqual({r["source"] for r in card["rulings"]}, {"wotc", "scryfall"})
        self.assertTrue(all(r["oracle_id"] == "oracle-a" for r in card["rulings"]))
        self.assertEqual(lookup_card("oracle-a", self.db)["name"], "Fixture Artist")

    def test_full_and_unambiguous_face_names_resolve_same_card(self):
        by_full = lookup_card("First Face // Second Face", self.db)
        by_face = lookup_card("First Face", self.db)
        self.assertEqual(by_full, by_face)
        self.assertEqual(by_face["card_faces"][1]["oracle_text"], "Back text.")
        self.assertEqual(by_face["card_faces"][1]["power"], "3")

    def test_unknown_ambiguous_and_duplicate_alias_inputs(self):
        for name in ("Unknown", "'; DROP TABLE cards; --", "Second Face"):
            with self.subTest(name=name), self.assertRaises(CardNameError):
                lookup_card(name, self.db)
        with self.assertRaises(CardNameError):
            context_file(["First Face", "First Face // Second Face"], self.db, self.root / "contexts")
        self.assertEqual(stats(self.db)["card_count"], 3)

    def test_small_legacy_context_is_deterministic_and_contains_only_requested_cards(self):
        path, names, metadata = context_file(["First Face"], self.db, self.root / "contexts")
        repeat, _, _ = context_file(["First Face // Second Face"], self.db, self.root / "contexts")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path, repeat)
        self.assertEqual(names, ["First Face // Second Face"])
        self.assertEqual(document["metadata"]["provider"], "Scryfall")
        self.assertEqual(document["metadata"]["schema_version"], 1)
        self.assertEqual(document["metadata"]["card_count"], 1)
        self.assertEqual(len(document["cards"]), 1)
        self.assertEqual(document["cards"][0]["oracle_id"], "oracle-b")
        self.assertEqual(len(document["cards"][0]["rulings"]), 1)
        self.assertEqual(document["metadata"]["catalog_sha256"], metadata["catalog_sha256"])

    def test_invalid_import_preserves_existing_database(self):
        before = sha256_file(self.db)
        self.write_records(self.oracle, CARDS + [CARDS[0]])
        with redirect_stdout(io.StringIO()), self.assertRaises(CatalogError):
            build_database(self.oracle, self.rulings, self.db)
        self.assertEqual(sha256_file(self.db), before)
        self.assertEqual(stats(self.db)["card_count"], 3)
        self.assertEqual(list(self.root.glob("cards_build_*")), [])

    def test_truncated_bulk_and_manifest_mismatch_preserve_database(self):
        before = sha256_file(self.db)
        self.oracle.write_bytes(b'{"oracle_id":"truncated"')
        with redirect_stdout(io.StringIO()), self.assertRaises(CatalogError):
            build_database(self.oracle, self.rulings, self.db)
        with self.assertRaises(CatalogError):
            build_database(self.oracle, self.rulings, self.db, oracle_manifest={"sha256": "wrong"})
        self.assertEqual(sha256_file(self.db), before)

    def test_array_and_uncompressed_jsonl_formats(self):
        array = self.root / "legacy.json"
        array.write_text(json.dumps(CARDS), encoding="utf-8")
        self.assertEqual(list(iter_objects(array)), CARDS)
        lines = self.root / "modern.jsonl"
        lines.write_text("\n" + "\n".join(json.dumps(card) for card in CARDS), encoding="utf-8")
        self.assertEqual(list(iter_objects(lines)), CARDS)

    def test_duplicate_rulings_are_deduplicated_and_orphans_reported(self):
        self.write_records(self.rulings, RULINGS + [RULINGS[0], {**RULINGS[0], "oracle_id": "absent"}])
        with redirect_stdout(io.StringIO()):
            result = build_database(self.oracle, self.rulings, self.db)
        self.assertEqual(result["ruling_count"], 3)
        self.assertEqual(result["duplicate_rulings"], 1)
        self.assertEqual(result["unmatched_rulings"], 1)

    def test_art_series_does_not_make_playable_face_name_ambiguous(self):
        art = sample_card("oracle-art", "First Face // First Face", layout="art_series", card_faces=[
            {"name": "First Face", "oracle_text": ""}, {"name": "First Face", "oracle_text": ""},
        ])
        token = sample_card("oracle-token", "Fixture Token", layout="token", legalities={"commander": "not_legal"})
        self.write_records(self.oracle, CARDS + [art, token])
        with redirect_stdout(io.StringIO()):
            result = build_database(self.oracle, self.rulings, self.db)
        self.assertEqual(result["skipped_art_series"], 1)
        self.assertEqual(result["card_count"], 4)
        self.assertEqual(lookup_card("First Face", self.db)["oracle_id"], "oracle-b")
        self.assertEqual(lookup_card("Fixture Token", self.db)["layout"], "token")

    def test_missing_database_is_not_silently_created(self):
        path = self.root / "missing.sqlite"
        with self.assertRaises(CatalogError):
            lookup_card("Anything", path)
        self.assertFalse(path.exists())

    def test_reimporting_same_snapshots_preserves_database_and_context_cache_keys(self):
        before_hash = sha256_file(self.db)
        before_context, _, _ = context_file(["Fixture Artist"], self.db, self.root / "contexts")
        with redirect_stdout(io.StringIO()):
            metadata = build_database(self.oracle, self.rulings, self.db)
        after_context, _, _ = context_file(["Fixture Artist"], self.db, self.root / "contexts")
        self.assertEqual(before_hash, sha256_file(self.db))
        self.assertEqual(before_context, after_context)
        self.assertEqual(metadata, self.metadata)

    def test_http_catalog_lookup_never_calls_engine(self):
        engine = SimpleNamespace(load_card_context=Mock(), run_card_query=Mock())
        settings = Settings(cards_db=self.db, cards_backend="sqlite", context_cache=self.root / "contexts")
        client = TestClient(create_app(RulingService(settings, engine)))
        response = client.get("/cards", params={"name": "First Face"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "First Face // Second Face")
        self.assertNotIn("snapshot_file", response.json())
        self.assertEqual(client.get("/cards", params={"name": "Unknown"}).status_code, 404)
        engine.load_card_context.assert_not_called()
        engine.run_card_query.assert_not_called()

    def test_http_ruling_uses_selected_subset_and_original_loader(self):
        # Inspect the actual selected file at the interface with the user's engine.
        def loader(names, path):
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(names, ["First Face // Second Face"])
            self.assertEqual(len(document["cards"]), 1)
            self.assertEqual(document["cards"][0]["card_faces"][1]["oracle_text"], "Back text.")
            return {"cards_json_sha256": sha256_file(path), "card_names": names, "sources": []}
        engine = SimpleNamespace(load_card_context=Mock(side_effect=loader), run_card_query=Mock(return_value={
            "pipeline_version": "1.5.0", "rules_result": {"metadata": {}},
            "answer": {"status": "insufficient_evidence", "answer": "Fixture response.", "trigger_count": None, "citations": []},
            "resolved_citations": [], "usage": {}, "elapsed_seconds": 0.01,
        }))
        settings = Settings(cards_db=self.db, cards_backend="sqlite", context_cache=self.root / "contexts")
        client = TestClient(create_app(RulingService(settings, engine)))
        response = client.post("/rulings", json={"question": "A test question", "cards": ["First Face"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["provenance"]["cards_backend"], "sqlite")
        self.assertEqual(response.json()["provenance"]["catalog_sha256"], self.metadata["catalog_sha256"])
        engine.run_card_query.assert_called_once()
        args, question, _ = engine.run_card_query.call_args.args
        self.assertTrue(args.cards_file.is_file())
        self.assertEqual(question, "A test question")
        for names in (["Unknown"], ["First Face", "First Face // Second Face"]):
            response = client.post("/rulings", json={"question": "A test question", "cards": names})
            self.assertEqual(response.status_code, 422)
        engine.run_card_query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
