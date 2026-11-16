"""Small Titan embedding/retrieval smoke test; --dry-run makes no AWS calls."""

import argparse
import hashlib
import json
import math
from pathlib import Path
from time import perf_counter


ROOT = Path(__file__).resolve().parent
MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024
SAMPLE_IDS = (
    "700.4", "614.6", "603.10a", "614.5", "903.1",
    "702.2b", "702.9b", "701.8a", "117.3b", "704.5f",
)
QUESTIONS = (
    "Una criatura que se exilia en lugar de ir al cementerio, cuenta como muerta?",
    "Does a creature die if it is exiled instead of going to the graveyard?",
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def unit_vector(vector):
    if not isinstance(vector, list) or len(vector) != DIMENSIONS:
        raise ValueError("El vector no tiene las dimensiones esperadas.")
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise ValueError("El vector contiene valores invalidos.")
    norm = math.sqrt(sum(x * x for x in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("El vector no tiene una norma valida.")
    return [x / norm for x in vector]


def render_rule(rule, by_id):
    parts = [f"Magic: The Gathering. Section {rule['section_id']}: {rule['section_title']}."]
    parent = rule.get("parent_rule_id")
    if parent:
        parts.append(f"Parent rule {parent}: {by_id[parent]['text']}")
    parts.append(f"Rule {rule['rule_id']}: {rule['text']}")
    return "\n".join(parts)


class EmbeddingCache:
    def __init__(self, path, settings):
        self.path = path
        self.settings = settings
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "schema_version": 1, "settings": settings, "vectors": {},
        }
        if self.data["settings"] != settings or self.data["schema_version"] != 1:
            raise ValueError("La cache no corresponde a esta configuracion.")
        self.client = None
        self.calls = self.input_tokens = 0

    def has(self, text):
        return sha(text) in self.data["vectors"]

    def get(self, text):
        key = sha(text)
        if self.has(text):
            return unit_vector(self.data["vectors"][key]["embedding"])
        if self.client is None:
            import boto3
            from botocore.config import Config

            session = boto3.Session(profile_name=self.settings["profile"])
            self.client = session.client(
                "bedrock-runtime", region_name=self.settings["region"],
                config=Config(connect_timeout=10, read_timeout=60,
                              retries={"mode": "standard", "total_max_attempts": 1}),
            )
        response = self.client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": text, "dimensions": DIMENSIONS, "normalize": True}),
        )
        self.calls += 1
        try:
            payload = json.loads(response["body"].read())
        finally:
            response["body"].close()
        vector = unit_vector(payload["embedding"])
        tokens = payload["inputTextTokenCount"]
        if type(tokens) is not int or tokens < 0:
            raise ValueError("El modelo no devolvio un recuento de tokens valido.")
        self.input_tokens += tokens
        self.data["vectors"][key] = {"embedding": vector, "input_tokens": tokens}
        save_json(self.path, self.data)
        return vector


def rank_rules(question_vector, vectors, ids):
    ranked = [
        {"rule_id": rid, "cosine_similarity": sum(a * b for a, b in zip(question_vector, vector))}
        for rid, vector in zip(ids, vectors)
    ]
    return sorted(ranked, key=lambda row: (-row["cosine_similarity"], row["rule_id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspeccion local, sin llamadas a AWS")
    parser.add_argument("--question", action="append", help="Consulta personalizada; se puede repetir")
    parser.add_argument("--profile", default="commander-rag")
    parser.add_argument("--region", default="eu-north-1")
    args = parser.parse_args()

    raw = (ROOT / "data/processed/rules.json").read_bytes()
    corpus = json.loads(raw)
    by_id = {rule["rule_id"]: rule for rule in corpus["rules"]}
    documents = [render_rule(by_id[rid], by_id) for rid in SAMPLE_IDS]
    questions = args.question or list(QUESTIONS)
    texts = documents + questions
    if any(not text.strip() or len(text) > 50000 for text in texts):
        raise ValueError("Hay un texto vacio o que supera los 50000 caracteres.")
    settings = {
        "model_id": MODEL_ID, "dimensions": DIMENSIONS, "normalize": True,
        "profile": args.profile, "region": args.region,
    }
    settings_hash = sha(json.dumps(settings, sort_keys=True))[:16]
    cache = EmbeddingCache(ROOT / "data/cache" / f"titan_{settings_hash}.json", settings)
    missing = len({sha(text) for text in texts if not cache.has(text)})

    print(f"Modelo: {MODEL_ID} | Region: {args.region} | Dimensiones: {DIMENSIONS}")
    print(f"Prueba limitada a {len(SAMPLE_IDS)} reglas y {len(questions)} consultas.")
    print(f"Llamadas nuevas previstas: {missing}; se reutilizara la cache disponible.")
    if args.dry_run:
        print("\nFragmentos que se enviarian:")
        for rid, text in zip(SAMPLE_IDS, documents):
            print(f"\n{rid} ({len(text)} caracteres):\n{text}")
        print("\nConsultas:")
        for question in questions:
            print(question)
        print("\nDRY RUN: no se han realizado llamadas a AWS.")
        return

    started = perf_counter()
    vectors = []
    for rid, text in zip(SAMPLE_IDS, documents):
        print(f"Regla {rid}: {'cache' if cache.has(text) else 'AWS'}", flush=True)
        vectors.append(cache.get(text))

    results = []
    for question in questions:
        ranked = rank_rules(cache.get(question), vectors, SAMPLE_IDS)
        results.append({"question": question, "ranking": ranked})
        print(f"\nPREGUNTA: {question}")
        for position, row in enumerate(ranked[:5], start=1):
            rid = row["rule_id"]
            print(f"{position}. {rid} | similitud={row['cosine_similarity']:.4f}")
            print(by_id[rid]["text"][:350])

    output = ROOT / "data/processed/embedding_smoke_results.json"
    save_json(output, {
        "schema_version": 1,
        "settings": settings,
        "corpus_json_sha256": hashlib.sha256(raw).hexdigest(),
        "rules_effective_date": corpus["metadata"]["rules_effective_date"],
        "candidate_rule_ids": list(SAMPLE_IDS),
        "embedding_text_version": 1,
        "results": results,
        "new_api_calls": cache.calls,
        "new_input_tokens": cache.input_tokens,
        "elapsed_seconds": perf_counter() - started,
    })
    print(f"\nLlamadas nuevas: {cache.calls} | Tokens de entrada nuevos: {cache.input_tokens}")
    print(f"Resultados guardados en: {output}")
    print("La similitud no es una probabilidad de acierto.")
    print("Esta muestra comprueba el flujo; no evalua la busqueda en las 3182 reglas.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"ERROR ({type(exc).__name__}): {exc}") from None
