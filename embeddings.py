"""Shared embedding configuration and rule rendering; importing makes no API calls."""

import math


MODEL_ID = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024


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
