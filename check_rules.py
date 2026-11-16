"""Validate the extracted corpus against its original TXT. No AWS calls."""

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ID_PATTERN = re.compile(r"\d{3}\.\d+[a-z]?")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(document, source):
    metadata = document["metadata"]
    rules = document["rules"]
    require(isinstance(rules, list) and rules, "La lista de reglas esta vacia.")
    raw = source.read_bytes()
    require(
        hashlib.sha256(raw).hexdigest() == metadata["source_sha256"],
        "El TXT no coincide con el hash registrado. Repite la ingesta si cambio.",
    )
    lines = raw.decode("utf-8-sig").splitlines()
    ids = [rule["rule_id"] for rule in rules]
    require(all(ID_PATTERN.fullmatch(rid) for rid in ids), "Hay IDs invalidos.")
    require(len(ids) == len(set(ids)), "Hay reglas duplicadas.")
    require(metadata["rule_count"] == len(rules), "El total no coincide con metadata.")
    by_id = {rule["rule_id"]: rule for rule in rules}

    # Independently inventory rule labels in the numbered body of the TXT.
    start = next(i for i, line in enumerate(lines) if line.startswith("100.1. "))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "Glossary")
    original_ids = []
    for line in lines[start:end]:
        tokens = line.strip().split(maxsplit=1)
        if tokens:
            label = tokens[0].rstrip(".")
            if ID_PATTERN.fullmatch(label):
                original_ids.append(label)
    require(ids == original_ids, "Faltan, sobran o se han reordenado reglas del TXT.")

    subrules = 0
    for rule in rules:
        rid = rule["rule_id"]
        first = rule["source_start_line"]
        last = rule["source_end_line"]
        require(
            type(first) is int and type(last) is int
            and start + 1 <= first <= last <= end,
            f"Lineas de origen incorrectas: {rid}",
        )
        fragment = lines[first - 1:last]
        label, first_text = fragment[0].strip().split(maxsplit=1)
        require(label.rstrip(".") == rid, f"La linea de origen no corresponde a {rid}.")
        original_text = "\n".join(
            [first_text, *[line.rstrip() for line in fragment[1:]]]
        ).strip()
        require(original_text == rule["text"], f"El texto no coincide con el TXT: {rid}")
        require(rule["text"].strip(), f"Regla sin texto: {rid}")
        require(rule["section_id"] == rid.split(".")[0], f"Seccion incorrecta: {rid}")
        expected_parent = rid[:-1] if rid[-1].isalpha() else None
        require(rule["parent_rule_id"] == expected_parent, f"Regla padre incorrecta: {rid}")
        if expected_parent:
            require(expected_parent in by_id, f"Falta la regla padre de {rid}.")
            subrules += 1

    for rid in ("100.1a", "603.10a", "614.5", "614.6", "700.4", "903.1"):
        require(rid in by_id, f"Falta la regla de control {rid}.")
    require("Example:" in by_id["614.5"]["text"], "Se perdio el ejemplo de 614.5.")
    require(
        "graveyard from the battlefield" in by_id["700.4"]["text"],
        "Revisa el contenido de la definicion de morir en 700.4.",
    )
    require(
        "If an event is replaced, it never happens." in by_id["614.6"]["text"],
        "Revisa el contenido de 614.6.",
    )
    return by_id, subrules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/processed/rules.json")
    parser.add_argument("--source", type=Path, help="TXT original si lo has movido")
    parser.add_argument("--rule", action="append", help="ID a inspeccionar; se puede repetir")
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    source = args.source or ROOT / "data/raw" / document["metadata"]["source_file"]
    by_id, subrules = validate(document, source)
    requested = args.rule or ["700.4", "614.6"]
    require(all(rid in by_id for rid in requested), "Alguna regla solicitada no existe.")

    print("OK: el hash del TXT coincide con la ingesta.")
    print(f"OK: {len(by_id)} reglas, sin duplicados y con cobertura del cuerpo numerado.")
    print("OK: todos los textos coinciden con sus lineas del TXT.")
    print(f"OK: {subrules} subreglas con su regla padre.")
    print("OK: reglas de control y ejemplo de 614.5 presentes.")
    print(f"Version: {document['metadata']['rules_effective_date']}")
    for rid in requested:
        rule = by_id[rid]
        print(f"\n{rid} | {rule['section_title']}")
        print(rule["text"])
    print("\nRESULTADO: corpus validado; recuperacion por ID correcta.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, AttributeError, OSError, StopIteration) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
