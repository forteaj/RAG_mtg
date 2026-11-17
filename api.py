"""Local HTTP adapter for retrieve_rules.py 1.5.x. Run: python -m uvicorn api:app"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from card_catalog import CatalogError, CardNameError, context_file, lookup_card, search_cards

ROOT = Path(__file__).resolve().parent
API_VERSION = "0.3.0"
LOG = logging.getLogger("mtg_api")
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SearchTerm = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]


class RulingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": {
        "question": "I control Blood Artist and two other vanilla creatures. An opponent controls one vanilla creature and Rest in Peace. Wrath of God resolves. No creature is a commander or has indestructible. There are no other relevant effects, abilities or responses. How many times does the triggered ability of my Blood Artist trigger?",
        "cards": ["Blood Artist", "Rest in Peace", "Wrath of God"],
    }})
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    cards: list[Name] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_cards(self):
        if len({name.casefold() for name in self.cards}) != len(self.cards):
            raise ValueError("List each reference card only once.")
        return self


class Source(BaseModel):
    citation_id: str
    source_id: str
    kind: str
    title: str
    source_url: str
    excerpt: str
    card_name: str | None = None
    rule_id: str | None = None
    publisher: str | None = None
    published_at: str | None = None


class Usage(BaseModel):
    new_llm_calls: int = Field(default=0, ge=0)
    llm_cache_hits: int = Field(default=0, ge=0)
    new_llm_input_tokens: int = Field(default=0, ge=0)
    new_llm_output_tokens: int = Field(default=0, ge=0)
    new_embedding_calls: int = Field(default=0, ge=0)
    new_embedding_input_tokens: int = Field(default=0, ge=0)


class Provenance(BaseModel):
    pipeline_version: str
    rules_effective_date: str | None = None
    corpus_json_sha256: str | None = None
    cards_json_sha256: str | None = None
    llm_model_id: str | None = None
    answer_reasoning: str | None = None
    cards_backend: str = "json"
    catalog_sha256: str | None = None
    oracle_updated_at: str | None = None
    rulings_updated_at: str | None = None
    oracle_sha256: str | None = None
    rulings_sha256: str | None = None


class RulingResponse(BaseModel):
    question: str
    status: Literal["answered", "needs_clarification", "insufficient_evidence"]
    answer: str = Field(min_length=1)
    trigger_count: Annotated[int, Field(strict=True, ge=0)] | None = None
    clarifying_question: str | None = None
    citations: list[str]
    sources: list[Source]
    provenance: Provenance
    usage: Usage
    elapsed_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def consistent_answer(self):
        ids = [source.citation_id for source in self.sources]
        if len(set(ids)) != len(ids) or len(set(self.citations)) != len(self.citations):
            raise ValueError("Duplicate citations.")
        if set(ids) != set(self.citations):
            raise ValueError("Citations must match resolved sources.")
        if self.status == "answered":
            if not self.citations or self.clarifying_question is not None:
                raise ValueError("Answered responses require sources and no clarification.")
        elif self.trigger_count is not None:
            raise ValueError("An unanswered request cannot claim a trigger count.")
        if self.status == "needs_clarification" and not (self.clarifying_question or "").strip():
            raise ValueError("A clarifying question is required.")
        return self


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code, self.code = status_code, code
        self.details = details or {}


@dataclass(frozen=True)
class Settings:
    profile: str = "commander-rag"
    region: str = "eu-north-1"
    cards_file: Path = ROOT / "data" / "processed" / "cards.json"
    cards_db: Path = ROOT / "data" / "indexes" / "cards.sqlite"
    cards_backend: Literal["auto", "sqlite", "json"] = "auto"
    context_cache: Path = ROOT / "data" / "cache" / "card_contexts"

    def __post_init__(self):
        if self.cards_backend not in {"auto", "sqlite", "json"}:
            raise ValueError("MTG_CARDS_BACKEND must be auto, sqlite or json.")

    @classmethod
    def from_env(cls):
        return cls(
            profile=os.getenv("MTG_AWS_PROFILE", "commander-rag"),
            region=os.getenv("MTG_AWS_REGION", "eu-north-1"),
            cards_file=Path(os.getenv("MTG_CARDS_FILE", str(cls.cards_file))),
            cards_db=Path(os.getenv("MTG_CARDS_DB", str(cls.cards_db))),
            cards_backend=os.getenv("MTG_CARDS_BACKEND", "auto"),
            context_cache=Path(os.getenv("MTG_CARD_CONTEXT_CACHE", str(cls.context_cache))),
        )

    def use_sqlite(self):
        return self.cards_backend == "sqlite" or (self.cards_backend == "auto" and self.cards_db.is_file())


class RulingService:
    def __init__(self, settings: Settings | None = None, engine: Any = None):
        self.settings = settings or Settings.from_env()
        self._engine = engine
        self._lock = Lock()

    def solve(self, request: RulingRequest) -> RulingResponse:
        # The CLI engine writes shared cache/checkpoint files; serialize local requests.
        if not self._lock.acquire(blocking=False):
            raise ServiceError(429, "busy", "Another ruling is running. Retry after it finishes.")
        try:
            if self._engine is None:
                try:
                    self._engine = importlib.import_module("retrieve_rules")
                except ImportError:
                    LOG.exception("Cannot import RAG engine")
                    raise ServiceError(503, "engine_unavailable", "Install the project dependencies and place api.py beside retrieve_rules.py.") from None
            backend = "sqlite" if self.settings.use_sqlite() else "json"
            catalog_metadata = {}
            try:
                selected_file, selected_names = self.settings.cards_file, request.cards
                if backend == "sqlite":
                    selected_file, selected_names, catalog_metadata = context_file(
                        request.cards, self.settings.cards_db, self.settings.context_cache,
                    )
                context = self._engine.load_card_context(selected_names, selected_file)
            except CardNameError as exc:
                raise ServiceError(422, "unknown_or_duplicate_card", str(exc)) from None
            except CatalogError as exc:
                raise ServiceError(503, "catalog_unavailable", str(exc)) from None
            except FileNotFoundError:
                raise ServiceError(503, "cards_unavailable", "The local cards JSON is missing. Run fetch_cards.py first.") from None
            except ValueError:
                raise ServiceError(422, "invalid_card_context", "A reference card is unknown or cards.json is invalid. Check the names and the local card data.") from None

            args = argparse.Namespace(
                profile=self.settings.profile, region=self.settings.region,
                lexical_only=False, fresh_llm=False, max_candidates=80, top_k=5,
                answer=True, answer_reasoning="low", cards_file=selected_file,
            )
            result = self._engine.run_card_query(args, request.question, context)
            try:
                answer = result["answer"]
                metadata = result["rules_result"]["metadata"]
                return RulingResponse(
                    question=request.question, status=answer["status"], answer=answer["answer"],
                    trigger_count=answer.get("trigger_count"),
                    clarifying_question=answer.get("clarifying_question"),
                    citations=answer["citations"], sources=result["resolved_citations"],
                    provenance=Provenance(
                        pipeline_version=result["pipeline_version"],
                        rules_effective_date=metadata.get("rules_effective_date"),
                        corpus_json_sha256=metadata.get("corpus_json_sha256"),
                        cards_json_sha256=context.get("cards_json_sha256"),
                        llm_model_id=result["rules_result"].get("llm_model_id"),
                        answer_reasoning=result.get("answer_reasoning"),
                        cards_backend=backend,
                        catalog_sha256=catalog_metadata.get("catalog_sha256"),
                        oracle_updated_at=catalog_metadata.get("oracle_updated_at"),
                        rulings_updated_at=catalog_metadata.get("rulings_updated_at"),
                        oracle_sha256=catalog_metadata.get("oracle_sha256"),
                        rulings_sha256=catalog_metadata.get("rulings_sha256"),
                    ),
                    usage=result["usage"], elapsed_seconds=result["elapsed_seconds"],
                )
            except (KeyError, TypeError, ValidationError):
                LOG.exception("Invalid engine response")
                raise ServiceError(502, "invalid_engine_response", "The engine returned an invalid response. Check the server log.") from None
        except ServiceError:
            raise
        except FileNotFoundError:
            LOG.exception("Missing local RAG file")
            raise ServiceError(503, "data_unavailable", "A required local RAG file is missing. Prepare the corpus and index first.") from None
        except Exception as exc:
            LOG.exception("RAG request failed")
            if type(exc).__name__ == "LoginRefreshRequired":
                raise ServiceError(503, "aws_login_expired", f"AWS login expired. Run aws login --profile {self.settings.profile}, then retry.") from None
            # Read SDK error codes without requiring boto3 when importing/testing the API.
            response = getattr(exc, "response", {})
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            code = error.get("Code") if isinstance(error, dict) else None
            if code in {"ThrottlingException", "TooManyRequestsException"}:
                raise ServiceError(429, "aws_throttled", "AWS throttled the request. Retry later.") from None
            if code in {"AccessDeniedException", "ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException"} or type(exc).__name__ in {"NoCredentialsError", "ProfileNotFound", "TokenRetrievalError", "CredentialRetrievalError", "UnauthorizedSSOTokenError", "SSOTokenLoadError"}:
                raise ServiceError(503, "aws_credentials", "Check the commander-rag AWS login, profile and Bedrock permissions in the server environment.") from None
            if isinstance(exc, ValueError):
                reason, separator, diagnostic = str(exc).partition(" Details saved in ")
                details = {
                    "engine_version": str(getattr(self._engine, "PIPELINE_VERSION", "unknown")),
                    "validation_error": reason[:1000],
                    # Keep a useful filename without exposing the machine's full path.
                    "diagnostic_file": PureWindowsPath(diagnostic.strip()).name if separator else None,
                }
                raise ServiceError(502, "engine_rejected_result", "The engine could not complete or validate the result. See validation_error and the diagnostic file.", details=details) from None
            raise ServiceError(502, "engine_failure", "The RAG engine failed. Check the server log.") from None
        finally:
            self._lock.release()


def create_app(service: RulingService | None = None) -> FastAPI:
    application = FastAPI(
        title="MTG Rulings — local API", version=API_VERSION,
        description="English questions using locally stored reference cards. Reference cards are documentation; the question defines which are active. Uses the existing Bedrock RAG engine.",
    )
    application.state.ruling_service = service if service is not None else RulingService()
    web_root = ROOT / "web"
    application.mount("/static", StaticFiles(directory=str(web_root), check_dir=False), name="static")

    @application.get("/", include_in_schema=False)
    def home():
        page = web_root / "index.html"
        if not page.is_file():
            return HTMLResponse("La interfaz no está instalada. Copia la carpeta web junto a api.py.", status_code=503)
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @application.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": str(exc), **exc.details}})

    @application.get("/health", tags=["health"])
    def health():
        """Process liveness only. Does not import the RAG engine or contact AWS."""
        return {"status": "ok", "service": "mtg-rulings", "api_version": API_VERSION}

    @application.get("/card-search", tags=["cards"], responses={503: {"description": "SQLite catalog unavailable"}})
    def card_search(q: SearchTerm, request: Request):
        """Prefix autocomplete using local English card and face names. No AWS."""
        try:
            matches = search_cards(q, request.app.state.ruling_service.settings.cards_db)
        except CatalogError as exc:
            raise ServiceError(503, "catalog_unavailable", str(exc)) from None
        return {"query": q, "results": matches}

    @application.get("/cards", tags=["cards"], responses={404: {"description": "Card not found"}, 503: {"description": "SQLite catalog unavailable"}})
    def card(name: Name, request: Request):
        """Look up a local English card/face name or Oracle UUID. No AWS calls."""
        try:
            record = lookup_card(name, request.app.state.ruling_service.settings.cards_db)
        except CardNameError as exc:
            raise ServiceError(404, "card_not_found", str(exc)) from None
        except CatalogError as exc:
            raise ServiceError(503, "catalog_unavailable", str(exc)) from None
        public_fields = {"name", "oracle_id", "mana_cost", "type_line", "oracle_text", "card_faces", "keywords", "power", "toughness", "loyalty", "defense", "colors", "color_identity", "cmc", "legalities", "source_url"}
        return {**{key: value for key, value in record.items() if key in public_fields}, "rulings": record["rulings"]}

    @application.post("/rulings", response_model=RulingResponse, tags=["rulings"], responses={
        422: {"description": "Invalid request or local card context"},
        429: {"description": "Another request is running, or AWS throttled the request"},
        502: {"description": "RAG execution or response validation failed"},
        503: {"description": "Local files, dependencies or AWS credentials unavailable"},
    })
    def ruling(body: RulingRequest, request: Request):
        return request.app.state.ruling_service.solve(body)

    return application


app = create_app()
