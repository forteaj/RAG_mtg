"""Extract numbered MTG rules from the official English TXT. No AWS calls."""

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RULE = re.compile(r"^(\d{3}\.\d+[a-z]?)(?:\.)?\s+(.+)$")
SECTION = re.compile(r"^(\d{3})\.\s+(.+)$")
REFERENCE = re.compile(r"\b\d{3}\.\d+[a-z]?\b")
CHAPTERS = {
    "1. Game Concepts", "2. Parts of a Card", "3. Card Types",
    "4. Zones", "5. Turn Structure", "6. Spells, Abilities, and Effects",
    "7. Additional Rules", "8. Multiplayer Rules", "9. Casual Variants",
}
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def parse_rules(text):
    """Ignore the contents and glossary; retain rule continuations/examples."""
    rules = []
    seen = set()
    current = None
    section_id = section_title = None
    started = finished = False

    def flush():
        nonlocal current
        if current is None:
            return
        body = "\n".join(current.pop("lines")).strip()
        current["text"] = body
        current["references"] = sorted(
            set(REFERENCE.findall(body)) - {current["rule_id"]}
        )
        rules.append(current)
        current = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if started and line == "Glossary":
            flush()
            finished = True
            break

        if line in CHAPTERS:
            flush()
            section_id = section_title = None
            continue

        heading = SECTION.fullmatch(line)
        if heading:
            flush()
            section_id, section_title = heading.groups()
            continue

        match = RULE.fullmatch(line)
        if match:
            rule_id, body = match.groups()
            if not started:
                if rule_id != "100.1":
                    continue
                started = True

            flush()
            if rule_id in seen:
                raise ValueError(f"Regla duplicada: {rule_id}")
            if section_id != rule_id.split(".")[0]:
                raise ValueError(f"Seccion ausente o incorrecta para {rule_id}")
            seen.add(rule_id)
            current = {
                "rule_id": rule_id,
                "section_id": section_id,
                "section_title": section_title,
                "parent_rule_id": rule_id[:-1] if rule_id[-1].isalpha() else None,
                "source_start_line": line_number,
                "source_end_line": line_number,
                "lines": [body],
            }
        elif current is not None:
            current["lines"].append(raw_line.rstrip())
            if line:
                current["source_end_line"] = line_number

    if not finished:
        raise ValueError("No se encontro el glosario final; revisa el TXT completo.")
    for rule in rules:
        parent = rule["parent_rule_id"]
        if parent is not None and parent not in seen:
            raise ValueError(f"Falta la regla padre {parent}")
    return rules


def locate_source():
    directory = ROOT / "data" / "raw"
    candidates = sorted({
        *directory.glob("MagicCompRules*.txt"),
        *directory.glob("MagicCompRules"),
    })
    candidates = [path for path in candidates if path.is_file()]
    if len(candidates) != 1:
        raise ValueError(
            f"Esperaba un TXT MagicCompRules en {directory}; "
            f"encontre {len(candidates)}. Indica el archivo con --input."
        )
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Ruta al TXT oficial")
    args = parser.parse_args()
    source = (args.input or locate_source()).resolve()
    raw = source.read_bytes()
    text = raw.decode("utf-8-sig")
    if not text.startswith("Magic: The Gathering Comprehensive Rules"):
        raise ValueError("El archivo no parece el TXT oficial en ingles.")

    date_match = re.search(
        r"effective as of ([A-Za-z]+) (\d{1,2}), (\d{4})\.", text
    )
    if not date_match:
        raise ValueError("No se pudo identificar la fecha de vigencia.")
    month_name, day, year = date_match.groups()
    version = date(
        int(year), MONTHS.index(month_name) + 1, int(day)
    ).isoformat()
    rules = parse_rules(text)
    by_id = {rule["rule_id"]: rule for rule in rules}
    required = {"100.1", "100.1a", "614.6", "700.4", "903.1"}
    missing = required - by_id.keys()
    if missing:
        raise ValueError(f"Faltan reglas de control: {sorted(missing)}")

    document = {
        "metadata": {
            "schema_version": 1,
            "parser_version": "1.0.0",
            "rules_effective_date": version,
            "source_file": source.name,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_page": "https://magic.wizards.com/en/rules",
            "language": "en",
            "rule_count": len(rules),
        },
        "rules": rules,
    }
    output = ROOT / "data" / "processed" / "rules.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)

    print(f"Origen: {source}")
    print(f"Version: {version}")
    print(f"Reglas extraidas: {len(rules)}")
    print(f"Salida: {output}")
    for rule_id in ("700.4", "614.6"):
        print(f"\n{rule_id}: {by_id[rule_id]['text']}")


if __name__ == "__main__":
    main()
