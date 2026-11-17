"""Local UI and prefix lookup contracts; synthetic catalog, no AWS/network."""
import gzip
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from api import RulingService, Settings, create_app
from card_catalog import build_database, search_cards, sha256_file


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'cards.sqlite'
        cards = [
            {'object': 'card', 'oracle_id': 'a', 'id': 'printing-a', 'name': 'First Face // First Other', 'type_line': 'Creature', 'layout': 'transform', 'card_faces': [{'name': 'First Face', 'oracle_text': 'Front.'}, {'name': 'First Other', 'oracle_text': 'Back.'}]},
            {'object': 'card', 'oracle_id': 'b', 'id': 'printing-b', 'name': 'First Fixture', 'type_line': 'Creature', 'oracle_text': 'Fixture.'},
        ]
        for filename, rows in [('oracle.jsonl.gz', cards), ('rulings.jsonl.gz', [{'object': 'ruling', 'oracle_id': 'a', 'source': 'wotc', 'published_at': '2020-01-01', 'comment': 'Fixture.'}])]:
            with gzip.open(self.root / filename, 'wt', encoding='utf-8') as handle:
                for row in rows:
                    handle.write(json.dumps(row) + '\n')
        with redirect_stdout(io.StringIO()):
            build_database(self.root / 'oracle.jsonl.gz', self.root / 'rulings.jsonl.gz', self.db)
        self.engine = SimpleNamespace(load_card_context=Mock(), run_card_query=Mock())
        self.client = TestClient(create_app(RulingService(Settings(cards_db=self.db), self.engine)))

    def tearDown(self):
        self.temp.cleanup()

    def test_prefix_search_matches_faces_and_deduplicates_card(self):
        self.assertEqual([c['oracle_id'] for c in search_cards(' FIRST ', self.db)], ['a', 'b'])
        self.assertEqual([c['oracle_id'] for c in search_cards('First Other', self.db)], ['a'])
        self.assertEqual(len(search_cards('First', self.db, limit=1)), 1)

    def test_search_is_read_only_and_metacharacters_are_literal(self):
        before = sha256_file(self.db)
        for query in ['%', "'; DROP TABLE cards; --", '_F', 'xx', 'F']:
            self.assertEqual(search_cards(query, self.db), [])
        self.assertEqual(sha256_file(self.db), before)

    def test_search_http_and_validation_never_call_engine(self):
        response = self.client.get('/card-search', params={'q': 'First'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 2)
        for value in ['', 'f', '   ', 'a' * 201]:
            self.assertEqual(self.client.get('/card-search', params={'q': value}).status_code, 422)
        self.engine.load_card_context.assert_not_called()
        self.engine.run_card_query.assert_not_called()

    def test_missing_catalog_returns_actionable_503(self):
        self.db.unlink()
        response = self.client.get('/card-search', params={'q': 'First'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'catalog_unavailable')
        self.engine.run_card_query.assert_not_called()

    def test_home_assets_and_docs_without_engine(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/html', response.headers['content-type'])
        self.assertIn('/static/app.js', response.text)
        for resource in ['/static/styles.css', '/static/app.js', '/static/mark.svg', '/docs']:
            self.assertEqual(self.client.get(resource).status_code, 200)
        self.assertEqual(self.client.get('/static/../api.py').status_code, 404)
        self.engine.run_card_query.assert_not_called()


if __name__ == '__main__':
    unittest.main()
