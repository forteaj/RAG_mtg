"""Run the MTG development suite against the local HTTP API, sequentially.

Only question/cards are sent to the API. Gold answers remain local. Automatic
checks are mechanical checks, never a substitute for reviewing explanations.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_SUITE = ROOT / "evaluation" / "cases_v1.json"
USAGE_KEYS = ("new_llm_calls", "llm_cache_hits", "new_llm_input_tokens", "new_llm_output_tokens", "new_embedding_calls", "new_embedding_input_tokens")
STATES = {"answered", "needs_clarification", "insufficient_evidence"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_suite(path=DEFAULT_SUITE):
    suite = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if suite.get("schema_version") != 1 or not suite.get("cases"):
        raise ValueError("Unsupported or empty evaluation suite.")
    ids = set()
    for case in suite["cases"]:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("Case IDs must be nonempty and unique.")
        ids.add(case_id)
        request = case.get("request", {})
        if set(request) != {"question", "cards"}:
            raise ValueError(f"{case_id}: only question/cards may be sent to the API.")
        if not isinstance(request["question"], str) or not 1 <= len(request["question"].strip()) <= 4000:
            raise ValueError(f"{case_id}: invalid question.")
        cards = request["cards"]
        if not isinstance(cards, list) or not 1 <= len(cards) <= 12 or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 for name in cards):
            raise ValueError(f"{case_id}: invalid cards.")
        if len({name.strip().casefold() for name in cards}) != len(cards):
            raise ValueError(f"{case_id}: duplicate cards.")
        kind, expected = case.get("kind"), case.get("expected", {})
        if kind not in {"count", "qualitative", "clarification"} or expected.get("status") not in STATES:
            raise ValueError(f"{case_id}: invalid kind/status.")
        if kind == "count" and (type(expected.get("trigger_count")) is not int or expected["trigger_count"] < 0):
            raise ValueError(f"{case_id}: count cases need a nonnegative integer gold count.")
        if kind != "count" and "trigger_count" in expected:
            raise ValueError(f"{case_id}: a non-count case must not specify a trigger count.")
        if kind == "clarification" and expected["status"] != "needs_clarification":
            raise ValueError(f"{case_id}: clarification cases must expect clarification.")
        if not expected.get("summary_es"):
            raise ValueError(f"{case_id}: missing human review expectation.")
    return suite


@dataclass
class HttpResult:
    status_code: int
    body: object
    elapsed_seconds: float = 0


class ApiUnavailable(Exception):
    pass


class ApiClient:
    def __init__(self, base_url="http://127.0.0.1:8000", timeout=600):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Use an HTTP(S) base URL without a query or fragment.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
        start = time.perf_counter()
        try:
            response = urlopen(request, timeout=self.timeout if method == "POST" else min(self.timeout, 15))
        except HTTPError as exc:
            response = exc
        except (URLError, TimeoutError, OSError) as exc:
            # A timeout may leave inference running: never automatically resend.
            raise ApiUnavailable(f"Cannot complete {method} {path}: {exc}. No automatic retry was made.") from exc
        try:
            with response:
                raw = response.read().decode("utf-8", errors="replace")
        except (TimeoutError, OSError) as exc:
            raise ApiUnavailable(f"Cannot read {method} {path}: {exc}. No automatic retry was made.") from exc
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"non_json_body": raw[:20000]}
        return HttpResult(response.code, body, round(time.perf_counter() - start, 4))


def check_response(case, body, suite):
    """Do not regex-match prose or grade citation relevance by ID membership."""
    if not isinstance(body, dict):
        return {"json_object": False}, [], []
    expected = case["expected"]
    status, answer = body.get("status"), body.get("answer")
    checks = {
        "question_echo": body.get("question") == case["request"]["question"],
        "valid_status": status in STATES if isinstance(status, str) else False,
        "expected_status": status == expected["status"],
        "nonempty_answer": isinstance(answer, str) and bool(answer.strip()),
    }
    count = body.get("trigger_count")
    checks["trigger_count"] = type(count) is int and count == expected["trigger_count"] if case["kind"] == "count" else count is None
    clarification = body.get("clarifying_question")
    checks["clarification_field"] = isinstance(clarification, str) and bool(clarification.strip()) if status == "needs_clarification" else clarification is None
    citations, sources = body.get("citations"), body.get("sources")
    source_ids = []
    valid_sources = isinstance(sources, list)
    if valid_sources:
        for source in sources:
            if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key] for key in ("citation_id", "source_id", "title", "source_url", "excerpt")):
                valid_sources = False
                break
            if source.get("kind") not in {"rule", "oracle_card", "card_ruling"}:
                valid_sources = False
                break
            source_ids.append(source["citation_id"])
    valid_citations = isinstance(citations, list) and all(isinstance(alias, str) and alias for alias in citations)
    checks["citation_integrity"] = bool(valid_sources and valid_citations and len(source_ids) == len(set(source_ids)) and len(citations) == len(set(citations)) and set(source_ids) == set(citations))
    checks["answered_has_sources"] = status != "answered" or bool(valid_sources and valid_citations and citations)
    provenance = body.get("provenance")
    usage = body.get("usage")
    checks["has_provenance"] = isinstance(provenance, dict) and bool(provenance.get("pipeline_version"))
    checks["valid_usage"] = isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in USAGE_KEYS)
    warnings = []
    if isinstance(provenance, dict):
        if provenance.get("rules_effective_date") != suite.get("rules_effective_date"):
            warnings.append("Rules version differs from the gold reference (or is missing); review expectations.")
        if provenance.get("cards_backend") != "sqlite":
            warnings.append("The response does not identify the SQLite cards backend.")
        if provenance.get("catalog_sha256") != suite.get("reference_catalog_sha256"):
            warnings.append("Card snapshot differs from the reference snapshot (or is missing); compare Oracle/rulings if needed.")
    coverage = []
    usable = sources if valid_sources else []
    for group in case.get("support_groups_diagnostic", []):
        matches = [source["citation_id"] for source in usable if source.get("rule_id") in group.get("rule_ids", []) or source.get("card_name") in group.get("card_names", [])]
        coverage.append({"label": group["label"], "matched_citations": matches, "covered": bool(matches)})
    return checks, warnings, coverage


def preflight(client, cases):
    health = client.request("GET", "/health")
    if health.status_code != 200 or not isinstance(health.body, dict) or health.body.get("status") != "ok":
        raise ApiUnavailable(f"GET /health failed: HTTP {health.status_code}. Start Uvicorn first.")
    cards = {}
    identities = {}
    for case in cases:
        for ref in case.get("reference_cards", []):
            identities[ref["card_name"]] = ref["oracle_id"]
    for name in sorted({name for case in cases for name in case["request"]["cards"]}):
        result = client.request("GET", "/cards?" + urlencode({"name": name}))
        if result.status_code != 200 or not isinstance(result.body, dict) or not result.body.get("oracle_id"):
            raise ApiUnavailable(f"Card preflight failed for {name!r}: HTTP {result.status_code}. Build the complete catalog and use API 0.2.0.")
        if name in identities and identities[name] != result.body["oracle_id"]:
            raise ApiUnavailable(f"Oracle identity differs for {name!r}; review the dataset before running paid inference.")
        cards[name] = {"name": result.body.get("name"), "oracle_id": result.body["oracle_id"]}
    return {"health": health.body, "cards": cards, "checked_at_utc": utc_now()}


def summarize(report):
    records = report["results"]
    completed = [record for record in records if record["execution_state"] == "completed"]
    counted = [record for record in completed if record["kind"] == "count"]
    clarifications = [record for record in completed if record["kind"] == "clarification"]
    latencies = [record["client_elapsed_seconds"] for record in completed]
    usage = {key: 0 for key in USAGE_KEYS}
    current_usage = {key: 0 for key in USAGE_KEYS}
    for record in completed:
        values = record["response"].get("usage", {}) if isinstance(record["response"], dict) else {}
        if not isinstance(values, dict):
            continue
        for key in USAGE_KEYS:
            value = values.get(key, 0)
            if type(value) is int and value >= 0:
                usage[key] += value
                if not record.get("reused_from_report", False):
                    current_usage[key] += value
    return {
        "selected_cases": len(records), "completed_http_200": len(completed),
        "http_or_transport_errors": sum(record["execution_state"] == "error" for record in records),
        "pending_or_interrupted": sum(record["execution_state"] in {"pending", "running", "interrupted"} for record in records),
        "mechanical_checks_passed": sum(record["checks_passed"] for record in completed),
        "expected_status_matches": sum(record["checks"].get("expected_status", False) for record in completed),
        "count_matches": sum(record["checks"].get("trigger_count", False) for record in counted),
        "count_cases_completed": len(counted),
        "clarification_matches": sum(record["checks"].get("expected_status", False) and record["checks"].get("clarification_field", False) for record in clarifications),
        "clarification_cases_completed": len(clarifications),
        "manual_reviews_pending": len(completed), "semantic_accuracy": None,
        "client_latency_seconds": {"mean": round(statistics.mean(latencies), 3), "median": round(statistics.median(latencies), 3), "p95_nearest_rank": round(sorted(latencies)[math.ceil(.95 * len(latencies)) - 1], 3)} if latencies else None,
        "usage_successful_responses_total": usage,
        "usage_successful_responses_this_execution": current_usage,
        "accounting_note": "Successful HTTP responses only. Failed calls may have incurred unreported usage. Resume retains earlier successful results; caches can make these timings warm-cache timings.",
    }


def write_report(directory, report):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report["updated_at_utc"] = utc_now()
    report["summary"] = summarize(report)
    save_json(directory / "report.json", report)
    with (directory / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["case_id", "title", "kind", "execution_state", "http_status", "checks_passed", "expected_status", "actual_status", "expected_count", "actual_count", "failed_checks", "expected_answer_es", "actual_answer", "clarifying_question", "cited_rules", "cited_cards", "elapsed_seconds", "new_llm_calls", "cache_hits", "input_tokens", "output_tokens", "manual_review"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in report["results"]:
            body = record.get("response")
            body = body if isinstance(body, dict) else {}
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            sources = body.get("sources") if isinstance(body.get("sources"), list) else []
            sources = [source for source in sources if isinstance(source, dict)]
            writer.writerow(dict(zip(fields, [
                record["case_id"], record["title"], record["kind"], record["execution_state"], record.get("http_status"), record.get("checks_passed"),
                record["expected"]["status"], body.get("status"), record["expected"].get("trigger_count"), body.get("trigger_count"),
                "; ".join(key for key, value in record.get("checks", {}).items() if not value), record["expected"]["summary_es"], body.get("answer"), body.get("clarifying_question"),
                "; ".join(str(source["rule_id"]) for source in sources if source.get("rule_id")), "; ".join(sorted({str(source["card_name"]) for source in sources if source.get("card_name")})),
                record.get("client_elapsed_seconds"), usage.get("new_llm_calls"), usage.get("llm_cache_hits"), usage.get("new_llm_input_tokens"), usage.get("new_llm_output_tokens"), "pending",
            ])))
    lines = ["# Revisión del RAG", "", "Las comprobaciones automáticas no miden por sí solas la corrección de las explicaciones o de las citas. Todos los resultados necesitan revisión semántica.", "", "```json", json.dumps(report["summary"], ensure_ascii=False, indent=2), "```", ""]
    if report.get("stopped_reason"):
        lines += ["Ejecución detenida: " + report["stopped_reason"], ""]
    for record in report["results"]:
        lines += [f"## {record['case_id']} — {record['title']}", "", "Pregunta:", "", record["request"]["question"], "", "Esperado: " + record["expected"]["summary_es"], "", "Estado de ejecución: " + record["execution_state"], ""]
        body = record.get("response")
        if isinstance(body, dict) and record["execution_state"] == "completed":
            lines += ["Respuesta obtenida:", "", str(body.get("answer", "")), "", f"Status: {body.get('status')}; disparos: {body.get('trigger_count')}; aclaración: {body.get('clarifying_question')}", ""]
            failed = [name for name, passed in record["checks"].items() if not passed]
            lines += ["Comprobaciones: " + (", ".join(failed) if failed else "sin fallos mecánicos"), ""]
            for warning in record.get("warnings", []):
                lines += ["Nota: " + warning, ""]
            lines += ["Fuentes citadas (revisar pertinencia y respaldo):", ""]
            for source in body.get("sources", []) if isinstance(body.get("sources"), list) else []:
                if isinstance(source, dict):
                    lines += [f"- [{source.get('citation_id')}] {source.get('title')} — {source.get('source_url')}"]
            lines += ["", "Los textos completos de las fuentes están en report.json.", "", "Revisión manual: pendiente.", ""]
        elif record.get("error"):
            lines += ["Error: " + str(record["error"]), ""]
    (directory / "review.md").write_text("\n".join(lines), encoding="utf-8")


def new_report(suite, cases, base_url):
    return {
        "schema_version": 1, "benchmark_id": suite["benchmark_id"], "suite_sha256": digest(suite),
        "rules_reference_version": suite.get("rules_effective_date"),
        "base_url": base_url.rstrip("/"), "started_at_utc": utc_now(), "run_state": "running",
        "notice": "Development/regression suite, not held-out general accuracy. Gold expectations are local and never included in HTTP requests. Semantic/source-support review remains pending.",
        "results": [{"case_id": case["id"], "title": case["title"], "category": case["category"], "kind": case["kind"],
                     "request": copy.deepcopy(case["request"]), "request_sha256": digest(case["request"]), "expected": copy.deepcopy(case["expected"]),
                     "reference_rule_ids": case.get("reference_rule_ids", []), "reference_cards": case.get("reference_cards", []),
                     "execution_state": "pending", "manual_review": "pending"} for case in cases],
    }


def run(suite, cases, client, directory, report=None, emit=print):
    report = report if report is not None else new_report(suite, cases, client.base_url)
    if report["suite_sha256"] != digest(suite) or report["base_url"] != client.base_url:
        raise ValueError("Resume requires the same suite and API URL.")
    expected_ids = [case["id"] for case in cases]
    if [record["case_id"] for record in report["results"]] != expected_ids or any(record["request_sha256"] != digest(case["request"]) for record, case in zip(report["results"], cases)):
        raise ValueError("Resume case selection/request hashes do not match.")
    for record in report["results"]:
        record["reused_from_report"] = record["execution_state"] == "completed"
    report["run_state"] = "running"
    report.pop("stopped_reason", None)
    write_report(directory, report)
    try:
        emit("Checking API and local card identities (no Bedrock calls)...")
        report["preflight"] = preflight(client, cases)
        for index, (case, record) in enumerate(zip(cases, report["results"]), 1):
            if record["execution_state"] == "completed":
                emit(f"[{index}/{len(cases)}] {case['id']}: kept existing HTTP 200 result")
                continue
            record.update(execution_state="running", started_at_utc=utc_now())
            for key in ("error", "response", "http_status", "checks", "checks_passed", "warnings", "support_coverage_diagnostic"):
                record.pop(key, None)
            write_report(directory, report)
            emit(f"[{index}/{len(cases)}] {case['id']}: {case['title']}...")
            try:
                # Intentionally whitelist the two API fields; no gold/rules/hints.
                payload = {"question": case["request"]["question"], "cards": list(case["request"]["cards"])}
                response = client.request("POST", "/rulings", payload)
            except ApiUnavailable as exc:
                record.update(execution_state="error", error=str(exc))
                raise
            record.update(http_status=response.status_code, response=response.body, client_elapsed_seconds=response.elapsed_seconds, finished_at_utc=utc_now())
            if response.status_code != 200:
                record.update(execution_state="error", error=response.body)
                emit(f"  HTTP {response.status_code}; saved error.")
                write_report(directory, report)
                if response.status_code in {401, 403, 429, 503}:
                    raise ApiUnavailable(f"Stopped after HTTP {response.status_code}; fix the error and resume. No automatic retry was made.")
                continue
            checks, warnings, coverage = check_response(case, response.body, suite)
            record.update(execution_state="completed", checks=checks, checks_passed=all(checks.values()), warnings=warnings, support_coverage_diagnostic=coverage)
            failed = [name for name, passed in checks.items() if not passed]
            emit("  " + ("CHECKS FAILED: " + ", ".join(failed) if failed else "CHECKS OK; semantic review pending"))
            write_report(directory, report)
        report["run_state"] = "finished"
    except (ApiUnavailable, KeyboardInterrupt) as exc:
        report["run_state"] = "stopped"
        report["stopped_reason"] = str(exc) or "Interrupted by user. Check whether the last server request is still running before resuming."
        for record in report["results"]:
            if record["execution_state"] == "running":
                record["execution_state"] = "interrupted"
        emit(report["stopped_reason"])
    finally:
        write_report(directory, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true", help="List selected questions/expectations; no network calls")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", type=Path, help="Resume a report.json; preserves all HTTP 200 results, even failed checks")
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.suite)
        if args.timeout <= 0 or (args.limit is not None and args.limit <= 0):
            raise ValueError("Timeout and limit must be positive.")
        report = None
        if args.resume:
            if args.case or args.limit or args.output:
                raise ValueError("Do not combine --resume with --case, --limit or --output.")
            report = json.loads(args.resume.read_text(encoding="utf-8-sig"))
            if args.base_url != report["base_url"]:
                raise ValueError("Pass the original --base-url when resuming a non-default API URL.")
            selected = {record["case_id"] for record in report["results"]}
            cases = [case for case in suite["cases"] if case["id"] in selected]
            directory = args.resume.parent
        else:
            unknown = set(args.case) - {case["id"] for case in suite["cases"]}
            if unknown:
                raise ValueError("Unknown case IDs: " + ", ".join(sorted(unknown)))
            cases = [case for case in suite["cases"] if not args.case or case["id"] in args.case]
            if args.limit:
                cases = cases[:args.limit]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            directory = args.output or ROOT / "data" / "processed" / "evaluations" / stamp
            if (directory / "report.json").exists() and not args.dry_run:
                raise ValueError("Output already contains a report. Choose a new directory or use --resume.")
        if args.dry_run:
            for case in cases:
                print(f"{case['id']} | {case['title']} | expected: {case['expected']['summary_es']}")
                print("  " + case["request"]["question"])
            print(f"{len(cases)} selected cases; no HTTP/AWS calls.")
            return 0
        print("Sequential evaluation; uncached requests may incur Bedrock usage. Semantic review remains pending.")
        report = run(suite, cases, ApiClient(args.base_url, args.timeout), directory, report)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print("Report:", directory / "report.json")
        print("CSV:", directory / "results.csv")
        print("Review:", directory / "review.md")
        if report["run_state"] != "finished":
            return 2
        return 0 if report["summary"]["mechanical_checks_passed"] == len(cases) else 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
