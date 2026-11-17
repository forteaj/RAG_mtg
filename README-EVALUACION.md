# Evaluación del RAG — 20 casos

Este paquete añade el evaluador y sus datos. No sustituye `api.py`, `retrieve_rules.py`, los prompts, el catálogo ni los embeddings. Requiere la API 0.2.0 que ya tienes funcionando.

## 1. Copiar y comprobar el evaluador

Extrae el contenido en `C:\Users\Jose\masterIA\TFM\PythonProject`. Con `(RAG_mtg)` activo, ejecuta desde esa carpeta:

```powershell
python -m unittest discover -s tests -p test_evaluate_rag.py -v
python evaluate_rag.py --dry-run
```

Los 16 tests simulan las respuestas del motor; uno utiliza un servidor HTTP local simulado. Ninguno llama a AWS. `--dry-run` solo muestra las 20 preguntas y sus expectativas locales, sin conectarse a la API. Puedes leerlas también en `evaluation/CASES.md` o abrir `evaluation/questions.csv` en Excel.

## 2. Ejecutar las 20 consultas reales

Deja Uvicorn arrancado en una terminal:

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

En otra terminal con `(RAG_mtg)` activo y desde la carpeta del proyecto:

```powershell
python evaluate_rag.py --output data/processed/evaluations/baseline_v1
```

El programa comprueba primero `/health` y busca todas las cartas por `/cards`, validando sus identidades Oracle. Esa comprobación no usa AWS. Después envía **20 peticiones secuenciales a `/rulings`**. Cada petición puede provocar varias llamadas a Bedrock, además de llamadas de embeddings, si faltan resultados en caché. No envía las consultas en paralelo ni reintenta automáticamente los POST.

Por cada petición, el cuerpo enviado contiene exclusivamente:

```json
{"question": "La pregunta en inglés", "cards": ["Las cartas de ese caso"]}
```

Las respuestas esperadas, IDs de reglas de referencia y criterios de evaluación se mantienen fuera del contexto del modelo. Los prompts del motor no cambian para superar esta prueba.

## 3. Leer los resultados

La carpeta elegida contendrá:

| Archivo | Contenido |
|---|---|
| `report.json` | Respuestas completas, fuentes originales citadas, expectativas, comprobaciones, versiones, tiempos y tokens. Es el archivo que puedes compartir aquí para revisar los resultados. |
| `results.csv` | Resumen por pregunta para Excel. |
| `review.md` | Preguntas, resultado esperado y respuesta obtenida, listos para revisión. |

El informe se guarda después de cada caso. Las salidas `CHECKS OK` y `checks_passed` significan que pasaron comprobaciones mecánicas. **No significan que toda la explicación sea correcta ni que las fuentes la respalden.** El campo `semantic_accuracy` queda en `null` y la revisión manual queda pendiente incluso cuando todos los números coinciden.

Las comprobaciones automáticas cubren:

- Los 11 números de disparos esperados, incluyendo el valor 0.
- El estado esperado en los 20 casos y las preguntas aclaratorias en los dos casos incompletos.
- La ausencia de un conteo de disparos en preguntas que no lo solicitan.
- La correspondencia entre aliases citados y fuentes resueltas, sin duplicados.
- La presencia de respuesta, procedencia y métricas de uso.

Los siete casos cualitativos requieren leer la respuesta. El evaluador no busca simplemente las palabras "yes" o "no": ese método aceptaría explicaciones contradictorias. También hay que revisar las explicaciones de los 11 conteos y la pertinencia de sus citas.

Los grupos `support_coverage_diagnostic` permiten localizar fuentes de referencia o alternativas. Su cobertura **no** es validación semántica y su ausencia no suspende automáticamente un caso. No se exige un ID de regla específico cuando otras fuentes pueden fundamentar la misma conclusión.

Los casos MTG-019 y MTG-020 priorizan pedir el dato que falta. Una respuesta condicional correcta puede ser útil aunque el estado no coincida con el esperado: ese desacuerdo se revisa como política de respuesta, no se interpreta automáticamente como una regla de Magic mal aplicada.

## 4. Si caduca AWS o se interrumpe la ejecución

Ante HTTP 401, 403, 429 o 503, el evaluador guarda el estado y se detiene. Ante un timeout tampoco reenvía la consulta, porque el servidor podría seguir procesándola. Los errores HTTP 502 quedan registrados y se continúa con el siguiente caso.

Si aparece `aws_login_expired`, renueva el acceso:

```powershell
aws login --profile commander-rag
```

Tras resolver el problema, reanuda:

```powershell
python evaluate_rag.py --resume data/processed/evaluations/baseline_v1/report.json
```

Antes de reanudar un timeout, comprueba en la terminal de Uvicorn que la petición anterior haya terminado. El modo `--resume` conserva los resultados HTTP 200, incluidos los que no pasaron las comprobaciones; solo repite errores o pendientes. Para reevaluar un cambio del motor, crea una ejecución nueva en otra carpeta.

Una carpeta que ya contiene un informe no se sobrescribe con una ejecución nueva. El evaluador también comprueba que el dataset, la selección de preguntas y la URL de la API coincidan al reanudar.

## Opciones

```powershell
# Seleccionar un caso sin ejecutar los otros 19:
python evaluate_rag.py --case MTG-008

# Prueba parcial de tres casos:
python evaluate_rag.py --limit 3

# Timeout por POST, en segundos:
python evaluate_rag.py --timeout 900
```

Si utilizas `--base-url` para una API distinta, pásalo también al reanudar. Los códigos de salida son 0 para una ejecución completa sin fallos mecánicos, 1 si hay fallos mecánicos o errores HTTP registrados y 2 para una ejecución detenida o un problema de configuración. El código 0 no es una certificación de precisión semántica.

## Cómo interpretar tiempos y tokens

El informe recoge latencia de cliente (media, mediana y percentil 95), llamadas nuevas, aciertos de caché y tokens nuevos. Al reanudar, distingue el uso agregado de los resultados conservados del uso de esta ejecución.

Las métricas de tokens proceden de respuestas exitosas: una llamada que terminó en error puede haber consumido tokens que no estén disponibles en el informe. Una consulta servida desde caché mide el recorrido con caché; no es una medición de inferencia en frío. No se calculan euros ni dólares con tarifas supuestas.

## Referencias y alcance

El dataset contiene 11 conteos, 7 preguntas cualitativas y 2 aclaraciones. Combina pruebas de regresión ya conocidas con variaciones nuevas; es un conjunto de desarrollo, **no** una muestra independiente de todas las interacciones de Magic. Permite comparar versiones del proyecto, pero no extrapolar una tasa de acierto general a todo el juego.

Las expectativas se contrastaron con las Comprehensive Rules efectivas el **6 de noviembre de 2026** y el snapshot de cartas/rulings de Scryfall del **17 de noviembre de 2026**. El JSON incluye los hashes y los identificadores de las cartas. Si la respuesta usa otros snapshots, el informe lo indica como nota para revisar posibles cambios de reglas o de Oracle.

Fuente oficial de reglas: https://media.wizards.com/2026/downloads/MagicCompRules%2020261106.txt

La importación local y las identidades de todas las cartas se han comprobado en este entorno. El motor completo está en tu ordenador: las 20 respuestas reales de Bedrock solo se obtendrán al ejecutar el comando contra tu API. El paquete no contiene resultados de generación inventados.
