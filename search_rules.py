"""Full-corpus lexical, vector and hybrid rule search with resumable embeddings."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from time import perf_counter

import numpy as np

from embeddings import DIMENSIONS, MODEL_ID, render_rule, unit_vector


ROOT = Path(__file__).resolve().parent
STOPWORDS = set("a an the and or but if then is are was were be been being to of in on at for from with by as it its this that these those i you we they my your our their do does did can could would should will what when where which how whether into instead than una un el la los las de del en y o que se es son al por para como mi su sus".split())


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def settings_for(args):
    return {
        "model_id": MODEL_ID, "dimensions": DIMENSIONS, "normalize": True,
        "profile": args.profile, "region": args.region,
    }


def settings_id(settings):
    return digest(json.dumps(settings, sort_keys=True))[:16]


def prepare_index(settings):
    raw = (ROOT / "data/processed/rules.json").read_bytes()
    corpus_hash = hashlib.sha256(raw).hexdigest()
    corpus = json.loads(raw)
    rules = corpus["rules"]
    by_id = {rule["rule_id"]: rule for rule in rules}
    if len(rules) != len(by_id) or len(rules) != corpus["metadata"]["rule_count"]:
        raise ValueError("El corpus contiene duplicados o un total incorrecto.")
    path = ROOT / "data/indexes" / f"rules_{corpus_hash[:16]}_{settings_id(settings)}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 1, "corpus_json_sha256": corpus_hash,
        "rules_effective_date": corpus["metadata"]["rules_effective_date"],
        "rule_count": len(rules), "settings": settings,
    }
    if not path.exists():
        temporary = path.with_suffix(".sqlite.tmp")
        if temporary.exists():
            temporary.unlink()
        db = sqlite3.connect(temporary)
        try:
            with db:
                db.execute("CREATE TABLE metadata (value TEXT NOT NULL)")
                db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata, sort_keys=True),))
                db.execute("CREATE TABLE rules (rule_id TEXT PRIMARY KEY, text TEXT NOT NULL, embedding_text TEXT NOT NULL, text_hash TEXT NOT NULL)")
                db.execute("CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id UNINDEXED, text, tokenize='porter unicode61')")
                for rule in rules:
                    text = render_rule(rule, by_id)
                    db.execute("INSERT INTO rules VALUES (?, ?, ?, ?)", (rule["rule_id"], rule["text"], text, digest(text)))
                    db.execute("INSERT INTO rules_fts VALUES (?, ?)", (rule["rule_id"], text))
        finally:
            db.close()
        temporary.replace(path)
    db = sqlite3.connect(path)
    stored = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
    if stored != metadata or db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] != len(rules):
        db.close()
        raise ValueError("El indice no coincide con el corpus/configuracion actual.")
    db.row_factory = sqlite3.Row
    return db, path, metadata


class VectorCache:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.calls = self.input_tokens = 0
        key = settings_id(settings)
        path = ROOT / "data/cache" / f"embeddings_{key}.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS metadata (value TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS vectors (text_hash TEXT PRIMARY KEY, vector BLOB NOT NULL, input_tokens INTEGER NOT NULL)")
            row = self.db.execute("SELECT value FROM metadata").fetchone()
            if row is None:
                self.db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(settings, sort_keys=True),))
            elif json.loads(row[0]) != settings:
                raise ValueError("La cache pertenece a otra configuracion.")

        # Import the original smoke-test cache, including its query vectors.
        legacy = ROOT / "data/cache" / f"titan_{key}.json"
        self.imported = 0
        if legacy.exists():
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if data["settings"] != settings or data["schema_version"] != 1:
                raise ValueError("La cache anterior tiene otra configuracion.")
            with self.db:
                for text_hash, entry in data["vectors"].items():
                    if self.has(text_hash):
                        continue
                    vector = np.asarray(unit_vector(entry["embedding"]), dtype=np.float32)
                    self.db.execute("INSERT INTO vectors VALUES (?, ?, ?)", (text_hash, vector.tobytes(), entry["input_tokens"]))
                    self.imported += 1

    def close(self):
        self.db.close()

    def has(self, text_hash):
        return self.db.execute("SELECT 1 FROM vectors WHERE text_hash=?", (text_hash,)).fetchone() is not None

    def cached(self, text_hash):
        row = self.db.execute("SELECT vector FROM vectors WHERE text_hash=?", (text_hash,)).fetchone()
        if row is None:
            return None
        vector = np.frombuffer(row[0], dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm <= 0:
            raise ValueError("Hay un vector invalido en la cache.")
        return vector / norm

    def get(self, text):
        text_hash = digest(text)
        vector = self.cached(text_hash)
        if vector is not None:
            return vector
        if not text.strip() or len(text) > 50000:
            raise ValueError("El texto esta vacio o supera los 50000 caracteres.")
        if self.client is None:
            import boto3
            from botocore.config import Config

            self.client = boto3.Session(profile_name=self.settings["profile"]).client(
                "bedrock-runtime", region_name=self.settings["region"],
                config=Config(connect_timeout=10, read_timeout=60,
                              retries={"mode": "standard", "total_max_attempts": 3}),
            )
        response = self.client.invoke_model(
            modelId=MODEL_ID, contentType="application/json", accept="application/json",
            body=json.dumps({"inputText": text, "dimensions": DIMENSIONS, "normalize": True}),
        )
        self.calls += 1
        try:
            payload = json.loads(response["body"].read())
        finally:
            response["body"].close()
        vector = np.asarray(unit_vector(payload["embedding"]), dtype=np.float32)
        tokens = payload["inputTextTokenCount"]
        if type(tokens) is not int or tokens < 0:
            raise ValueError("Recuento de tokens invalido.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO vectors VALUES (?, ?, ?)", (text_hash, vector.tobytes(), tokens))
        self.input_tokens += tokens
        return vector


def lexical_search(db, question, limit=50):
    terms = list(dict.fromkeys(term for term in re.findall(r"\w+", question.lower(), re.UNICODE) if term not in STOPWORDS))[:40]
    if not terms:
        return []
    expression = " OR ".join('"' + term + '"' for term in terms)
    rows = db.execute(
        "SELECT rule_id, bm25(rules_fts) AS score FROM rules_fts WHERE rules_fts MATCH ? ORDER BY score, rule_id LIMIT ?",
        (expression, limit),
    ).fetchall()
    return [{"rule_id": row["rule_id"], "score": float(row["score"])} for row in rows]


def semantic_search(rows, cache, question, limit=50):
    vectors = [cache.cached(row["text_hash"]) for row in rows]
    missing = sum(vector is None for vector in vectors)
    if missing:
        raise ValueError(f"Faltan embeddings de {missing} reglas. Ejecuta primero build; lexical no los necesita.")
    matrix = np.stack(vectors)
    scores = matrix @ cache.get(question)
    order = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), rows[i]["rule_id"]))[:limit]
    return [{"rule_id": rows[i]["rule_id"], "score": float(scores[i])} for i in order]


def reciprocal_rank_fusion(rankings, constant=60):
    scores = {}
    for ranking in rankings:
        for position, row in enumerate(ranking, start=1):
            rid = row["rule_id"]
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (constant + position)
    return [{"rule_id": rid, "score": value} for rid, value in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="commander-rag")
    parser.add_argument("--region", default="eu-north-1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare", help="Crear el indice lexico, sin AWS")
    builder = sub.add_parser("build", help="Generar embeddings pendientes; permite reanudar")
    builder.add_argument("--dry-run", action="store_true")
    search = sub.add_parser("search", help="Buscar reglas en el corpus completo")
    search.add_argument("question")
    search.add_argument("--mode", choices=("lexical", "semantic", "hybrid", "compare"), default="compare")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--expand", action="append", default=[], help="Consulta adicional para una prueba de descomposicion; se puede repetir")
    args = parser.parse_args()
    if args.command == "search" and not 1 <= args.top_k <= 50:
        raise ValueError("top-k debe estar entre 1 y 50.")
    settings = settings_for(args)
    db, path, metadata = prepare_index(settings)
    cache = None
    try:
        rows = db.execute("SELECT * FROM rules ORDER BY rowid").fetchall()
        print(f"Corpus: {len(rows)} reglas | Version: {metadata['rules_effective_date']}")
        print(f"Indice: {path}")
        if args.command == "prepare":
            print("Indice lexico preparado. No se ha llamado a AWS.")
            return
        queries = list(dict.fromkeys([args.question, *args.expand])) if args.command == "search" else []
        if args.command == "search" and args.mode == "lexical":
            lexical_parts = [lexical_search(db, query) for query in queries]
            rankings = {"lexical": lexical_parts[0] if len(queries) == 1 else reciprocal_rank_fusion(lexical_parts)}
        else:
            cache = VectorCache(settings)
            if cache.imported:
                print(f"Vectores importados de la prueba anterior: {cache.imported}")
            if args.command == "build":
                unique = {row["text_hash"]: row["embedding_text"] for row in rows}
                pending = [text for key, text in unique.items() if not cache.has(key)]
                print(f"Reglas pendientes de embedding: {len(pending)}")
                print(f"Caracteres pendientes: {sum(map(len, pending))}")
                if args.dry_run:
                    print("DRY RUN: no se ha llamado a AWS.")
                    return
                started = perf_counter()
                for i, text in enumerate(pending, start=1):
                    cache.get(text)
                    if i == 1 or i % 25 == 0 or i == len(pending):
                        print(f"Progreso: {i}/{len(pending)} | {perf_counter()-started:.1f} s | Tokens nuevos: {cache.input_tokens}", flush=True)
                print(f"Indice vectorial completo. Solicitudes completadas nuevas: {cache.calls}")
                return
            lexical_parts = [lexical_search(db, query) for query in queries]
            semantic_parts = [semantic_search(rows, cache, query) for query in queries]
            lexical = lexical_parts[0] if len(queries) == 1 else reciprocal_rank_fusion(lexical_parts)
            semantic = semantic_parts[0] if len(queries) == 1 else reciprocal_rank_fusion(semantic_parts)
            hybrid = reciprocal_rank_fusion([*lexical_parts, *semantic_parts])
            all_rankings = {"lexical": lexical, "semantic": semantic, "hybrid": hybrid}
            rankings = all_rankings if args.mode == "compare" else {args.mode: all_rankings[args.mode]}

        by_id = {row["rule_id"]: row for row in rows}
        for name, ranking in rankings.items():
            print(f"\n{name.upper()} | {args.question}")
            if not ranking:
                print("Sin coincidencias.")
            for position, result in enumerate(ranking[:args.top_k], start=1):
                rid = result["rule_id"]
                print(f"{position}. {rid} | score={result['score']:.6f}")
                print(by_id[rid]["text"][:300])

        report = {
            "schema_version": 1, "metadata": metadata, "question": args.question,
            "queries": queries,
            "candidate_pool": 50, "rrf_constant": 60,
            "rankings": rankings,
            "new_embedding_calls": cache.calls if cache else 0,
            "new_input_tokens": cache.input_tokens if cache else 0,
        }
        output = ROOT / "data/processed" / f"retrieval_{digest(json.dumps(queries, ensure_ascii=False))[:16]}_{args.mode}.json"
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
        print(f"\nResultados: {output}")
        print("Scores BM25, coseno y RRF tienen escalas distintas; no son probabilidades.")
    finally:
        if cache:
            cache.close()
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"ERROR ({type(exc).__name__}): {exc}") from None
