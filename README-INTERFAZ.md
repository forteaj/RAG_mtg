# Commander RAG — interfaz local 0.3.0

La web utiliza tu API FastAPI y tu motor RAG actual. HTML, CSS y JavaScript se sirven desde el mismo proceso y el mismo puerto. No requiere Node, npm ni un servidor adicional para utilizarla.

## Instalación en tu proyecto de Windows

1. Detén Uvicorn con **Ctrl+C**.
2. Descomprime este paquete y copia su contenido dentro de `C:\Users\Jose\masterIA\TFM\PythonProject`. Sustituye `api.py` y `card_catalog.py`, y conserva la carpeta `web` junto a ellos. Las carpetas `tests` y `tools` contienen comprobaciones opcionales.
3. El proyecto seguirá usando tu `retrieve_rules.py`, las reglas, el catálogo `data/indexes/cards.sqlite` y las cachés existentes. Este paquete no incluye ni sobrescribe esos datos. No necesitas regenerar embeddings ni reconstruir el catálogo.
4. Desde PowerShell, con tu entorno `(RAG_mtg)` activo y situado en la carpeta del proyecto, ejecuta:

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

5. Abre **http://127.0.0.1:8000/** en tu navegador. La documentación continúa en **http://127.0.0.1:8000/docs**.

Si ya usabas la API, las dependencias instaladas son suficientes. El perfil y la región mantienen los valores existentes: `commander-rag` y `eu-north-1`, o sus correspondientes variables `MTG_AWS_PROFILE` y `MTG_AWS_REGION`.

## Primera prueba

Pulsa **¿Mueren o se exilian?**. Se cargarán la pregunta y tres cartas desde tu catálogo. Revisa el texto y pulsa **Consultar las reglas** para enviar la petición real al motor. Este envío sí puede generar consumo en Bedrock.

Con la situación del ejemplo, el resultado esperado es **0 disparos**. El ejemplo no envía ese resultado al motor: la petición solo contiene `question` y `cards`.

También puedes escribir una pregunta propia en inglés, buscar cartas por el comienzo de su nombre en inglés, elegirlas con el ratón o con flechas y Enter, y desplegar su texto Oracle antes de consultar. El botón + permite añadir un nombre completo exacto. El límite actual de la API es de 12 cartas y 4000 caracteres.

Las cartas seleccionadas son documentación de referencia. La pregunta debe indicar quién las controla y cuáles están activas. La interfaz no infiere el estado de la mesa a partir de la selección.

La resolución muestra la respuesta en inglés, el recuento cuando corresponde, las citas y los extractos de las fuentes. Si falta información, muestra la pregunta de aclaración y permite editar la consulta. Puedes copiar la respuesta o guardar el resultado completo en JSON. No se envía ninguna consulta automáticamente al cargar ejemplos o seleccionar cartas.

## Cambios de API

- `GET /`: interfaz HTML.
- `GET /static/...`: recursos locales.
- `GET /card-search?q=Blood`: hasta ocho coincidencias por prefijo de nombre o cara, sin distinguir mayúsculas. Exige entre 2 y 200 caracteres; consulta SQLite en modo lectura. No utiliza AWS.
- `GET /cards`, `GET /health` y `POST /rulings`: conservan sus contratos anteriores.

**API conectada** solo indica que `/health` responde. No confirma que la sesión AWS siga vigente. Si ha caducado, inicia sesión de nuevo con `aws login --profile commander-rag` y reinicia Uvicorn.

La búsqueda requiere `data/indexes/cards.sqlite` o la ruta configurada en `MTG_CARDS_DB`, igual que el endpoint de cartas que ya utilizabas. El catálogo se consulta localmente, sin acceder a Scryfall durante la búsqueda. Los enlaces de las fuentes se abren en una pestaña nueva cuando decides visitarlos.

## Comprobaciones realizadas

- **70 pruebas de Python correctas** sobre API, catálogo, contratos del motor y evaluación, incluidas cinco nuevas para la web y la búsqueda.
- **8 comprobaciones del cliente correctas** ejecutando el JavaScript en un DOM simulado: ejemplos sin inferencia, selección de cartas, contrato de envío, cero disparos, aclaración sin recuento inventado, fuentes desplegables, texto seguro y recuperación tras un error de sesión.
- Sintaxis de JavaScript verificada con Node.
- Sin llamadas reales a AWS durante estas comprobaciones.

No se ha podido ejecutar la revisión visual en un navegador en este entorno, que impide iniciarlo o abrir los archivos locales. Queda pendiente comprobar la apariencia y el flujo completo contra tu sesión AWS en Windows.

Puedes ejecutar las pruebas nuevas de Python sin llamar a AWS:

```powershell
python -m unittest discover -s tests -p test_web.py -v
```

La comprobación de cliente es opcional y necesita Node únicamente para pruebas:

```powershell
node tests/test_web_client.cjs
```

`tools/check_web_browser.py` contiene una prueba opcional con Playwright: usa tu catálogo local y simula exclusivamente las respuestas a `/rulings`, sin ejecutar Bedrock. Necesita Playwright y su Chromium instalados; no son dependencias de la aplicación. No se ha ejecutado satisfactoriamente en este entorno. La ruta del navegador puede indicarse con `MTG_CHROMIUM_PATH`.
