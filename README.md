# Commander RAG

Asistente web para resolver interacciones de cartas en **Magic: The Gathering**. Describe una situación de partida en inglés y obtén una explicación con citas al reglamento, al texto Oracle y a los rulings de las cartas.

## Ejemplo de consulta

> Can Giant Spider block Wind Drake if there are no other blocking restrictions?

Sí. Giant Spider tiene alcance, que le permite bloquear a una criatura con volar. La interfaz permite consultar las fuentes junto a la respuesta. [Ver el ejemplo en JSON](request_reach_flying.json)

## Cómo funciona

1. **FastAPI** recibe la pregunta y las cartas seleccionadas en la web.
2. El motor **RAG** combina búsqueda léxica en SQLite y similitud entre embeddings para recuperar reglas. El catálogo de Scryfall aporta texto Oracle y rulings.
3. **Amazon Bedrock** genera la explicación a partir de las fuentes recuperadas. La API comprueba que las citas correspondan a esas fuentes.

La [evaluación de 20 casos](evaluation/CASES.md) incluye interacciones en las que hay que contar disparos, aplicar efectos de reemplazo o pedir una aclaración.

## Demo en AWS

La versión **0.5.0** está preparada para desplegarse en AWS Lambda con detección automática de cartas, acceso HTTPS por invitación y un cupo configurable de consultas. La URL de la demo está pendiente de publicación.

[Ejecutar en local](README-INTERFAZ.md) · [Documentación de la API](README-API.md) · [Guía de evaluación](README-EVALUACION.md)
