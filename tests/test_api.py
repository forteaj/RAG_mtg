"""HTTP/adapter tests with simulated engine results; no Bedrock calls."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api import RulingService, Settings, create_app  # noqa: E402


PAYLOAD = {"question": "How many times does Blood Artist trigger?", "cards": ["Blood Artist", "Rest in Peace", "Wrath of God"]}
CONTEXT = {"card_names": PAYLOAD["cards"], "cards_json_sha256": "cards-hash", "sources": []}
RESULT = {
    "pipeline_version": "1.5.0", "answer_reasoning": "low",
    "rules_result": {"metadata": {"rules_effective_date": "2026-11-06", "corpus_json_sha256": "rules-hash"}, "llm_model_id": "eu.amazon.nova-2-lite-v1:0"},
    "answer": {"status": "answered", "answer": "No creatures die, so there are no triggers.", "trigger_count": 0, "citations": ["C02"]},
    "resolved_citations": [{"citation_id": "C02", "source_id": "card:example", "kind": "oracle_card", "title": "Rest in Peace — Oracle text", "card_name": "Rest in Peace", "source_url": "https://scryfall.com/", "excerpt": "Local source fixture.", "snapshot_sha256": "private-extra-field"}],
    "usage": {"new_llm_calls": 1, "llm_cache_hits": 2, "new_llm_input_tokens": 2483, "new_llm_output_tokens": 872, "new_embedding_calls": 0, "new_embedding_input_tokens": 0},
    "elapsed_seconds": 1.25,
    "candidate_checkpoint": "C:/private/path/checkpoint.json",
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(
            load_card_context=Mock(return_value=copy.deepcopy(CONTEXT)),
            run_card_query=Mock(return_value=copy.deepcopy(RESULT)),
        )
        self.settings = Settings(cards_file=Path("fixture-cards.json"), cards_backend="json")
        self.service = RulingService(self.settings, self.engine)
        self.client = TestClient(create_app(self.service))

    def test_health_and_openapi_do_not_call_engine(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/rulings", spec["paths"])
        self.assertEqual(self.client.get("/docs").status_code, 200)
        self.engine.load_card_context.assert_not_called()
        self.engine.run_card_query.assert_not_called()

    def test_adapter_passes_inputs_settings_and_returns_sources(self):
        response = self.client.post("/rulings", json=PAYLOAD)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["trigger_count"], 0)
        self.assertEqual(data["citations"], ["C02"])
        self.assertEqual(data["sources"][0]["title"], "Rest in Peace — Oracle text")
        self.assertNotIn("snapshot_sha256", data["sources"][0])
        self.assertNotIn("candidate_checkpoint", data)
        self.assertEqual(data["provenance"]["cards_json_sha256"], "cards-hash")
        self.assertEqual(data["usage"]["new_llm_output_tokens"], 872)
        self.engine.load_card_context.assert_called_once_with(PAYLOAD["cards"], self.settings.cards_file)
        args, question, context = self.engine.run_card_query.call_args.args
        self.assertEqual(question, PAYLOAD["question"])
        self.assertEqual(context, CONTEXT)
        self.assertEqual(args.profile, "commander-rag")
        self.assertEqual(args.region, "eu-north-1")
        self.assertEqual(args.answer_reasoning, "low")
        self.assertEqual(args.max_candidates, 80)
        self.assertFalse(args.lexical_only)
        self.assertFalse(args.fresh_llm)

    def test_returned_count_is_engine_result_not_api_constant(self):
        result = copy.deepcopy(RESULT)
        result["answer"]["trigger_count"] = 4
        result["answer"]["answer"] = "Four creatures die, giving four triggers."
        self.engine.run_card_query.return_value = result
        response = self.client.post("/rulings", json={**PAYLOAD, "question": "There is no Rest in Peace on the battlefield. How many triggers?"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trigger_count"], 4)

    def test_invalid_bodies_are_rejected_before_engine(self):
        invalid = [
            {}, {**PAYLOAD, "question": "   "}, {**PAYLOAD, "question": "x" * 4001},
            {**PAYLOAD, "cards": []}, {**PAYLOAD, "cards": [" "]},
            {**PAYLOAD, "cards": ["Blood Artist", " blood artist "]},
            {**PAYLOAD, "cards": [str(i) for i in range(13)]},
            {**PAYLOAD, "profile": "untrusted-profile"},
        ]
        for body in invalid:
            with self.subTest(body=str(body)[:70]):
                self.assertEqual(self.client.post("/rulings", json=body).status_code, 422)
        self.engine.load_card_context.assert_not_called()
        self.engine.run_card_query.assert_not_called()

    def test_unknown_card_rejected_before_generation(self):
        self.engine.load_card_context.side_effect = ValueError("Unknown card: private details")
        response = self.client.post("/rulings", json=PAYLOAD)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_card_context")
        self.assertNotIn("private details", response.text)
        self.engine.run_card_query.assert_not_called()

    def test_missing_card_file_is_503(self):
        self.engine.load_card_context.side_effect = FileNotFoundError("private/path")
        response = self.client.post("/rulings", json=PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.engine.run_card_query.assert_not_called()

    def test_clarification_is_preserved(self):
        result = copy.deepcopy(RESULT)
        result["answer"].update(status="needs_clarification", trigger_count=None, answer="The active permanents are unspecified.", clarifying_question="Is Rest in Peace on the battlefield?", citations=[])
        result["resolved_citations"] = []
        self.engine.run_card_query.return_value = result
        response = self.client.post("/rulings", json=PAYLOAD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "needs_clarification")
        self.assertIsNone(response.json()["trigger_count"])

    def test_invalid_engine_citations_and_counts_fail_closed(self):
        mutations = [
            {"citations": ["invented"]}, {"citations": []},
            {"trigger_count": -1}, {"trigger_count": True},
            {"status": "needs_clarification", "trigger_count": 0},
        ]
        for mutation in mutations:
            result = copy.deepcopy(RESULT)
            result["answer"].update(mutation)
            self.engine.run_card_query.return_value = result
            with self.subTest(mutation=mutation), self.assertLogs("mtg_api", level="ERROR"):
                response = self.client.post("/rulings", json=PAYLOAD)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["error"]["code"], "invalid_engine_response")

    def test_busy_request_is_rejected_without_engine_calls(self):
        self.service._lock.acquire()
        try:
            response = self.client.post("/rulings", json=PAYLOAD)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()["error"]["code"], "busy")
            self.engine.load_card_context.assert_not_called()
        finally:
            self.service._lock.release()

    def test_failure_does_not_leave_service_locked(self):
        self.engine.run_card_query.side_effect = RuntimeError("secret path or credentials")
        with self.assertLogs("mtg_api", level="ERROR"):
            response = self.client.post("/rulings", json=PAYLOAD)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret", response.text)
        self.engine.run_card_query.side_effect = None
        self.assertEqual(self.client.post("/rulings", json=PAYLOAD).status_code, 200)

    def test_aws_auth_and_throttling_errors_are_mapped(self):
        for code, status, expected in [("ExpiredTokenException", 503, "aws_credentials"), ("ThrottlingException", 429, "aws_throttled")]:
            error = RuntimeError("private AWS response")
            error.response = {"Error": {"Code": code}}
            self.engine.run_card_query.side_effect = error
            with self.subTest(code=code), self.assertLogs("mtg_api", level="ERROR"):
                response = self.client.post("/rulings", json=PAYLOAD)
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], expected)

    def test_environment_settings(self):
        with patch.dict("os.environ", {"MTG_AWS_PROFILE": "custom", "MTG_AWS_REGION": "eu-west-1", "MTG_CARDS_FILE": "custom/cards.json"}):
            settings = Settings.from_env()
        self.assertEqual(settings.profile, "custom")
        self.assertEqual(settings.region, "eu-west-1")
        self.assertEqual(settings.cards_file, Path("custom/cards.json"))

    def test_expired_aws_login_returns_reauthentication_command(self):
        # Simulate botocore's named exception without requiring AWS dependencies.
        error_type = type("LoginRefreshRequired", (Exception,), {})
        self.engine.run_card_query.side_effect = error_type("Session expired")
        with self.assertLogs("mtg_api", level="ERROR"):
            response = self.client.post("/rulings", json=PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "aws_login_expired")
        self.assertIn("aws login --profile commander-rag", response.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
