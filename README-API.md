# API local de MTG

Adaptador para `retrieve_rules.py` **1.5.0**. Reutiliza `load_card_context()` y `run_card_query()`; conserva los prompts, el índice, la caché y el razonamiento `low` del motor existente.

## 1. Copiar e instalar

Copia el contenido de este paquete en `C:\Users\Jose\masterIA\TFM\PythonProject`, junto a tu `retrieve_rules.py` actual. Conserva los archivos existentes del motor y la carpeta `data`.

En PowerShell, con `(RAG_mtg)` activo y desde esa carpeta:

```powershell
python -m pip install -r requirements-api.txt
python -m unittest discover -s tests -p test_api.py -v
```

Los tests usan respuestas simuladas del motor. Comprueban el contrato HTTP, el adaptador, las validaciones y los errores; no llaman a AWS ni comprueban el razonamiento de Magic. Para el uso real deben estar instaladas las dependencias que ya utiliza tu proyecto, y disponibles `data/processed/cards.json`, el corpus y los embeddings del índice que ya preparaste.

## 2. Arrancar

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Ejecuta **un solo proceso**, sin `--workers`: el motor comparte archivos de caché y checkpoints. Una petición RAG ocupa el servicio; una segunda simultánea recibe HTTP 429. Para detenerlo: Ctrl+C.

Abre http://127.0.0.1:8000/docs y despliega `POST /rulings` → **Try it out**. Pega uno de los dos JSON incluidos y pulsa **Execute**. `GET /health` comprueba que la API responde; no valida AWS ni el índice.

## 3. Prueba real desde otra terminal

Con el servidor abierto, en otra PowerShell desde la carpeta del proyecto:

```powershell
$body = Get-Content -Raw -Encoding UTF8 .\request_with_rip.json
$result = Invoke-RestMethod -Uri http://127.0.0.1:8000/rulings -Method Post -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 600
$result | ConvertTo-Json -Depth 10
if ($result.status -ne 'answered' -or $result.trigger_count -ne 0) { throw 'Fallo: con Rest in Peace se esperaban 0 disparos.' }

$body = Get-Content -Raw -Encoding UTF8 .\request_without_rip.json
$result = Invoke-RestMethod -Uri http://127.0.0.1:8000/rulings -Method Post -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 600
$result | ConvertTo-Json -Depth 10
if ($result.status -ne 'answered' -or $result.trigger_count -ne 4) { throw 'Fallo: sin Rest in Peace se esperaban 4 disparos.' }
```

Estas peticiones sí ejecutan el RAG y pueden consumir llamadas de Bedrock cuando no haya caché. El adaptador nunca recibe los resultados esperados 0/4: solo se comprueban en la terminal después de obtener la respuesta. Revisa también la explicación y las fuentes; acertar el número no demuestra que toda la respuesta sea correcta.

## Contrato

- `question`: pregunta en inglés, entre 1 y 4000 caracteres.
- `cards`: de 1 a 12 nombres de cartas ya presentes en tu JSON local, sin duplicados. Son referencias documentales; **la pregunta define cuáles están en el campo de batalla**. Por eso ambos ejemplos pueden tener las mismas referencias.
- Respuesta: `status`, `answer`, `trigger_count` (número o null), `clarifying_question`, `citations`, `sources` (texto original y URL), `provenance` (versión, hashes y modelo), `usage` y `elapsed_seconds`.
- HTTP 200 puede contener `needs_clarification` o `insufficient_evidence`; comprueba siempre `status`.
- HTTP 422: cuerpo incorrecto, carta desconocida o contexto local de cartas inválido. No llega a la generación si falla la carga de cartas.
- HTTP 429: servicio ocupado o limitación de AWS. HTTP 503: faltan archivos, dependencias o acceso AWS. HTTP 502: fallo del motor o respuesta inválida. El detalle técnico queda en la consola del servidor.

La API comprueba que las citas coincidan con las fuentes resueltas. La corrección de las reglas sigue dependiendo del motor y debe evaluarse con casos variados.

## Configuración opcional

Los valores por defecto son perfil `commander-rag`, región `eu-north-1` y `data/processed/cards.json` junto a `api.py`. Si necesitas cambiarlos, establece estas variables **antes de arrancar** el servidor:

```powershell
$env:MTG_AWS_PROFILE = 'commander-rag'
$env:MTG_AWS_REGION = 'eu-north-1'
# Solo si moviste el JSON:
# $env:MTG_CARDS_FILE = 'C:\ruta\cards.json'
```

Si el login de AWS caduca, renueva el acceso como hacías al ejecutar el script. La API usa las mismas credenciales. Este paso sirve la aplicación en tu equipo; el despliegue público queda para una fase posterior.
