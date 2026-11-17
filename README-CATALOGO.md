# Catálogo completo de cartas — API 0.2.0

Este cambio añade `card_catalog.py` y actualiza `api.py`. Conserva el motor `retrieve_rules.py` 1.5.0 y su forma de resolver preguntas. La API recibe el mismo JSON de siempre, con `question` y `cards`.

## Instalación y primera prueba

1. Detén Uvicorn con Ctrl+C.
2. Extrae el contenido de este paquete en `C:\Users\Jose\masterIA\TFM\PythonProject`, junto a `retrieve_rules.py`. Acepta sustituir `api.py`; conserva los scripts de tu motor y tus datos actuales.
3. Con `(RAG_mtg)` activo, ejecuta desde la carpeta del proyecto:

```powershell
python -m pip install -r requirements-api.txt
python -m unittest discover -s tests -v
python card_catalog.py build
python card_catalog.py stats
python card_catalog.py lookup "Blood Artist" "Sol Ring" "Delver of Secrets"
```

`build` descarga los exports **Oracle Cards** y **Rulings**, y crea `data/indexes/cards.sqlite`. El índice de reglas, los embeddings y `data/processed/cards.json` se conservan. La descarga/importación no usa AWS. Los exports actuales de Scryfall están en JSONL comprimido con GZIP; el importador procesa los registros progresivamente. También acepta los antiguos arrays JSON, que sí carga en memoria.

4. Arranca la API:

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

En http://127.0.0.1:8000/docs verás `GET /cards` además de `POST /rulings`.

## Comprobar cartas sin gastar llamadas de Bedrock

Abre `GET /cards` → **Try it out**, introduce `Sol Ring` en `name` y pulsa **Execute**. Recibirás su texto, características y rulings guardados en SQLite.

También puedes hacerlo desde otra PowerShell:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/cards?name=Sol%20Ring' | ConvertTo-Json -Depth 10
```

Se aceptan nombres ingleses sin distinguir mayúsculas, nombres completos de cartas de varias caras, nombres de caras que sean inequívocos y UUIDs Oracle. Los nombres ambiguos se rechazan; no se elige una carta por similitud. Si dos nombres corresponden a la misma carta, `POST /rulings` los rechaza antes de llamar al modelo.

## Comprobar el RAG con cartas nuevas

Prueba de nuevo los JSON `request_with_rip.json` y `request_without_rip.json`: se esperan 0 y 4 disparos respectivamente. Comprueba `provenance.cards_backend`: debe ser `sqlite`. También aparecerán el hash del catálogo y las fechas de los dos exports.

Después envía `request_reach_flying.json`, que usa cartas que no estaban entre las tres originales:

```powershell
$body = Get-Content -Raw -Encoding UTF8 .\request_reach_flying.json
$result = Invoke-RestMethod -Uri http://127.0.0.1:8000/rulings -Method Post -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 600
$result | ConvertTo-Json -Depth 10
```

Se espera `status: answered` y una respuesta afirmativa: Giant Spider tiene reach y puede bloquear a Wind Drake, que tiene flying, en las condiciones indicadas. Aquí `trigger_count` puede ser null. Revisa las fuentes: Oracle de las cartas y/o reglas sobre flying y reach. Los resultados esperados no se incluyen en la petición al modelo.

`GET /cards`, `stats`, `lookup`, `context` y los tests no utilizan AWS. `POST /rulings` sí puede generar llamadas de pago a Bedrock. Si la sesión caduca, renueva el acceso con `aws login --profile commander-rag`.

## Cómo se conecta al motor existente

- La descarga se hace por exports completos; no se solicita una carta a Scryfall por cada consulta.
- SQLite almacena una entrada por Oracle ID, sus aliases de nombre y sus rulings relacionados por Oracle ID. Se excluyen los objetos `art_series` (láminas de ilustración), para que no compitan con los nombres de cartas jugables. El número excluido aparece en `stats`.
- Para cada petición, la API extrae solo las cartas solicitadas a un JSON pequeño en `data/cache/card_contexts/`.
- Ese JSON utiliza el formato del `fetch_cards.py` existente; la API llama a tu `load_card_context()` original y después a `run_card_query()`.
- El adaptador conserva la distinción de procedencia de los rulings (`wotc` / `scryfall`). El motor existente sigue eligiendo cuáles utiliza. Los nombres de caras se convierten en el nombre completo antes de llamar al loader.
- La documentación de una carta no activa sus habilidades: la pregunta sigue definiendo el estado de la partida.
- Los datos de Commander se guardan en `legalities`, pero no filtramos el catálogo por legalidad. Así también podrás resolver consultas generales o históricas sobre cartas que no sean legales en Commander. El motor necesita más contexto para preguntas sobre una fecha o reglamento histórico.

El CLI `retrieve_rules.py` conserva su JSON de tres cartas por defecto. Si quieres usar cartas nuevas desde ese CLI, primero exporta un contexto:

```powershell
python card_catalog.py context --card "Giant Spider" --card "Wind Drake"
```

El comando muestra la ruta del JSON seleccionado. Pásala mediante `--cards-file "RUTA_MOSTRADA"`, junto a los `--card` habituales. La API hace esa exportación automáticamente.

## Configuración y actualización

Sin configuración adicional, `MTG_CARDS_BACKEND=auto` usa SQLite si existe `data/indexes/cards.sqlite`; si no existe, utiliza el JSON anterior. Un SQLite inválido genera un error, no una sustitución silenciosa por otro catálogo.

Para exigir el catálogo completo antes de arrancar:

```powershell
$env:MTG_CARDS_BACKEND = 'sqlite'
# Opcional si moviste la base de datos:
# $env:MTG_CARDS_DB = 'C:\ruta\cards.sqlite'
```

Para volver temporalmente al JSON: `$env:MTG_CARDS_BACKEND = 'json'`.

Para actualizar el catálogo, detén Uvicorn y ejecuta otra vez `python card_catalog.py build`. El programa comprueba la versión remota, reutiliza descargas cuyo SHA256 coincide y conserva la base existente si los dos snapshots no han cambiado. Si cambian los datos, construye una base temporal y la sustituye solo al terminar la importación y verificar SQLite. Un fallo intermedio conserva la base anterior.

Los exports originales y manifiestos se guardan en `data/raw/scryfall/bulk/`. No se descargan imágenes ni se generan embeddings de cartas. Mantén `data/` fuera de Git según el `.gitignore` de tu proyecto; puedes reproducir el catálogo con el comando de construcción.

Para importar archivos ya descargados sin red:

```powershell
python card_catalog.py import --oracle-file "RUTA_ORACLE.jsonl.gz" --rulings-file "RUTA_RULINGS.jsonl.gz"
```

El orden de argumentos para una base alternativa es `python card_catalog.py --db "RUTA.sqlite" build` (o `stats`, `lookup`, `import`, `context`).

## Alcance de las pruebas

Los tests comprueban formatos, conservación de textos/caras, nombres ambiguos, relación de rulings, contextos pequeños, integridad de importación y rutas HTTP. La generación del motor se simula en los tests HTTP. No prueban la calidad del razonamiento de Magic; las tres consultas reales anteriores siguen siendo necesarias en tu entorno con Bedrock.

Fuentes de datos: https://api.scryfall.com/bulk-data y https://scryfall.com/docs/api/bulk-data. Política de acceso: https://scryfall.com/docs/faqs/i-m-having-trouble-accessing-the-scryfall-api-or-i-m-blocked-17
