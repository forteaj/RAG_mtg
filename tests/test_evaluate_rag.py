"""Evaluator tests use simulated answers and a local stub HTTP server, never AWS."""
import copy
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from evaluate_rag import ApiClient, ApiUnavailable, HttpResult, check_response, digest, load_suite, main, new_report, run


SUITE = load_suite()


def fixture_answer(case):
    expected = case["expected"]
    clarification = case["kind"] == "clarification"
    return {
        "question": case["request"]["question"], "status": expected["status"],
        "answer": "Simulated engine output; this is not a real benchmark answer.",
        "trigger_count": expected.get("trigger_count"),
        "clarifying_question": "What is the missing board information?" if clarification else None,
        "citations": [] if clarification else ["R01"],
        "sources": [] if clarification else [{"citation_id": "R01", "source_id": "rule:700.4", "kind": "rule", "rule_id": "700.4", "title": "Fixture rule", "source_url": "https://magic.wizards.com/en/rules", "excerpt": "Synthetic source text."}],
        "provenance": {"pipeline_version": "1.5.0", "cards_backend": "sqlite", "rules_effective_date": SUITE["rules_effective_date"], "catalog_sha256": SUITE["reference_catalog_sha256"]},
        "usage": {"new_llm_calls": 1, "llm_cache_hits": 2, "new_llm_input_tokens": 100, "new_llm_output_tokens": 20, "new_embedding_calls": 0, "new_embedding_input_tokens": 0},
        "elapsed_seconds": 0.05,
    }


class FakeClient:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, overrides=None):
        self.posts = []
        self.overrides = overrides or {}
        self.identities = {ref["card_name"]: ref["oracle_id"] for case in SUITE["cases"] for ref in case["reference_cards"]}

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/health":
            return HttpResult(200, {"status": "ok", "api_version": "0.2.0"})
        if method == "GET" and path.startswith("/cards?"):
            name = parse_qs(urlparse(path).query)["name"][0]
            return HttpResult(200, {"name": name, "oracle_id": self.identities[name]})
        self.posts.append(copy.deepcopy(payload))
        case = next(case for case in SUITE["cases"] if case["request"] == payload)
        if case["id"] in self.overrides:
            value = self.overrides[case["id"]]
            if isinstance(value, Exception):
                raise value
            return value
        return HttpResult(200, fixture_answer(case), 0.1)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "report"

    def tearDown(self):
        self.temp.cleanup()

    def test_suite_has_20_cases_and_balanced_outcomes(self):
        self.assertEqual(len(SUITE["cases"]), 20)
        self.assertEqual(sum(c["kind"] == "count" for c in SUITE["cases"]), 11)
        self.assertEqual(sum(c["kind"] == "qualitative" for c in SUITE["cases"]), 7)
        self.assertEqual(sum(c["kind"] == "clarification" for c in SUITE["cases"]), 2)
        self.assertEqual(SUITE["cases"][0]["expected"]["trigger_count"], 0)
        self.assertEqual(SUITE["cases"][1]["expected"]["trigger_count"], 4)
        self.assertEqual(SUITE["cases"][7]["expected"]["trigger_count"], 8)

    def test_only_question_and_cards_are_sent_to_inference(self):
        client = FakeClient()
        report = run(SUITE, SUITE["cases"], client, self.directory, emit=lambda _: None)
        self.assertEqual(len(client.posts), 20)
        for request in client.posts:
            self.assertEqual(set(request), {"question", "cards"})
        self.assertEqual(report["summary"]["mechanical_checks_passed"], 20)
        self.assertEqual(report["summary"]["count_matches"], 11)
        self.assertIsNone(report["summary"]["semantic_accuracy"])
        self.assertEqual(report["summary"]["manual_reviews_pending"], 20)
        self.assertTrue((self.directory / "report.json").is_file())
        self.assertTrue((self.directory / "results.csv").is_file())
        self.assertIn("Revisión manual: pendiente", (self.directory / "review.md").read_text(encoding="utf-8"))

    def test_wrong_count_and_boolean_count_are_not_accepted(self):
        case = SUITE["cases"][0]
        for count in (1, True, "0", None):
            body = fixture_answer(case)
            body["trigger_count"] = count
            checks, _, _ = check_response(case, body, SUITE)
            self.assertFalse(checks["trigger_count"])

    def test_invented_or_duplicate_citation_aliases_fail_integrity(self):
        case = SUITE["cases"][0]
        for citations in (["invented"], ["R01", "R01"], [None]):
            body = fixture_answer(case)
            body["citations"] = citations
            checks, _, _ = check_response(case, body, SUITE)
            self.assertFalse(checks["citation_integrity"])

    def test_unrelated_sources_and_prose_are_left_for_manual_review(self):
        case = SUITE["cases"][10]
        body = fixture_answer(case)
        checks, _, coverage = check_response(case, body, SUITE)
        self.assertTrue(all(checks.values()))
        self.assertTrue(any(not group["covered"] for group in coverage))
        # An unrelated valid alias must never be advertised as semantic success.
        report = run(SUITE, [case], FakeClient(), self.directory, emit=lambda _: None)
        self.assertIsNone(report["summary"]["semantic_accuracy"])
        self.assertEqual(report["summary"]["manual_reviews_pending"], 1)

    def test_answered_when_clarification_needed_is_flagged(self):
        case = SUITE["cases"][18]
        body = fixture_answer(case)
        body.update(status="answered", clarifying_question=None, trigger_count=4)
        checks, _, _ = check_response(case, body, SUITE)
        self.assertFalse(checks["expected_status"])
        self.assertFalse(checks["trigger_count"])

    def test_resume_keeps_previous_success_and_only_sends_missing_requests(self):
        cases = SUITE["cases"][:3]
        first = FakeClient({cases[1]["id"]: HttpResult(503, {"error": {"code": "aws_login_expired"}})})
        report = run(SUITE, cases, first, self.directory, emit=lambda _: None)
        self.assertEqual(report["run_state"], "stopped")
        self.assertEqual(len(first.posts), 2)
        loaded = json.loads((self.directory / "report.json").read_text(encoding="utf-8"))
        second = FakeClient()
        report = run(SUITE, cases, second, self.directory, loaded, emit=lambda _: None)
        self.assertEqual(len(second.posts), 2)
        self.assertEqual(report["summary"]["usage_successful_responses_total"]["new_llm_calls"], 3)
        self.assertEqual(report["summary"]["usage_successful_responses_this_execution"]["new_llm_calls"], 2)

    def test_resume_does_not_rerun_failed_checks_with_http_200(self):
        cases = SUITE["cases"][:1]
        bad = fixture_answer(cases[0])
        bad["trigger_count"] = 7
        report = run(SUITE, cases, FakeClient({cases[0]["id"]: HttpResult(200, bad)}), self.directory, emit=lambda _: None)
        second = FakeClient()
        report = run(SUITE, cases, second, self.directory, report, emit=lambda _: None)
        self.assertEqual(second.posts, [])
        self.assertEqual(report["summary"]["mechanical_checks_passed"], 0)

    def test_timeout_stops_without_resending_and_saves_checkpoint(self):
        cases = SUITE["cases"][:2]
        client = FakeClient({cases[0]["id"]: ApiUnavailable("Simulated timeout")})
        report = run(SUITE, cases, client, self.directory, emit=lambda _: None)
        self.assertEqual(report["run_state"], "stopped")
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(report["summary"]["pending_or_interrupted"], 1)
        self.assertEqual(report["summary"]["http_or_transport_errors"], 1)

    def test_http_502_is_saved_and_remaining_cases_continue(self):
        cases = SUITE["cases"][:2]
        client = FakeClient({cases[0]["id"]: HttpResult(502, {"error": {"code": "engine_failure"}})})
        report = run(SUITE, cases, client, self.directory, emit=lambda _: None)
        self.assertEqual(report["run_state"], "finished")
        self.assertEqual(report["summary"]["http_or_transport_errors"], 1)
        self.assertEqual(report["summary"]["completed_http_200"], 1)

    def test_invalid_json_shape_does_not_break_report(self):
        cases = SUITE["cases"][:1]
        client = FakeClient({cases[0]["id"]: HttpResult(200, ["invalid response"])})
        report = run(SUITE, cases, client, self.directory, emit=lambda _: None)
        self.assertEqual(report["summary"]["mechanical_checks_passed"], 0)
        self.assertTrue((self.directory / "results.csv").is_file())

    def test_schema_prevents_gold_fields_inside_api_request(self):
        suite = copy.deepcopy(SUITE)
        suite["cases"][0]["request"]["expected_count"] = 0
        path = Path(self.temp.name) / "invalid.json"
        path.write_text(json.dumps(suite), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_suite(path)

    def test_changed_suite_cannot_be_resumed(self):
        cases = SUITE["cases"][:1]
        report = new_report(SUITE, cases, FakeClient.base_url)
        report["suite_sha256"] = "different"
        with self.assertRaises(ValueError):
            run(SUITE, cases, FakeClient(), self.directory, report, emit=lambda _: None)

    def test_dry_run_makes_no_network_calls(self):
        with redirect_stdout(io.StringIO()) as output:
            code = main(["--dry-run", "--limit", "2", "--base-url", "http://127.0.0.1:1"])
        self.assertEqual(code, 0)
        self.assertIn("2 selected cases; no HTTP/AWS calls", output.getvalue())

    def test_reference_versions_are_warnings_not_semantic_failures(self):
        case = SUITE["cases"][0]
        body = fixture_answer(case)
        body["provenance"].update(rules_effective_date="different", catalog_sha256="different")
        checks, warnings, _ = check_response(case, body, SUITE)
        self.assertTrue(all(checks.values()))
        self.assertEqual(len(warnings), 2)

    def test_real_http_transport_with_stub_service(self):
        fake = FakeClient()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.reply(fake.request("GET", self.path))
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.reply(fake.request("POST", self.path, payload))
            def reply(self, result):
                raw = json.dumps(result.body).encode()
                self.send_response(result.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ApiClient(f"http://127.0.0.1:{server.server_port}", timeout=2)
            report = run(SUITE, SUITE["cases"][:1], client, self.directory, emit=lambda _: None)
            self.assertEqual(report["summary"]["mechanical_checks_passed"], 1)
            self.assertEqual(len(fake.posts), 1)
            self.assertEqual(fake.posts[0], SUITE["cases"][0]["request"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
