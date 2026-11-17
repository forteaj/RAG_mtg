'use strict';
const $ = id => document.getElementById(id);
const state = {cards: [], suggestions: [], active: -1, busy: false, editing: false, searchSeq: 0, searchAbort: null, result: null};
const examples = {
  death: {cards: ['Blood Artist', 'Rest in Peace', 'Wrath of God'], question: 'I control Blood Artist and two other creatures. My opponent controls one creature and Rest in Peace. Wrath of God resolves, destroying all four creatures simultaneously. None are commanders or have indestructible. There are no other relevant effects. How many times does my Blood Artist ability trigger, and why?'},
  blocking: {cards: ['Giant Spider', 'Wind Drake'], question: 'My opponent attacks with Wind Drake. I control an untapped Giant Spider that is otherwise able to block. Assuming no other blocking restrictions, can Giant Spider block Wind Drake, and why?'},
  commander: {cards: ['Blood Artist', 'Yargle, Glutton of Urborg', 'Murder'], question: 'I control Blood Artist and my commander Yargle, Glutton of Urborg. Murder resolves targeting Yargle and destroys it. There are no replacement effects or other relevant effects. I then choose to move Yargle from my graveyard to the command zone as a state-based action. How many times does Blood Artist trigger, and why?'}
};
function node(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function announce(message) { $('announcement').textContent = message; }
function feedback(message = '') { $('form-feedback').textContent = message; }
function safeLink(url, label, cls) {
  try { const parsed = new URL(url); if (parsed.protocol !== 'https:') return null; const a = node('a', cls, label); a.href = parsed.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; } catch { return null; }
}
async function request(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'Accept': 'application/json', ...options.headers}});
  let data; try { data = await response.json(); } catch { data = {error: {message: `Respuesta no válida del servidor (HTTP ${response.status}).`}}; }
  if (!response.ok) { const error = new Error(data.error?.message || `HTTP ${response.status}`); error.data = data; error.status = response.status; throw error; }
  return data;
}
function updateCount() { $('char-count').textContent = `${$('question').value.length} / 4000`; }
function closeSuggestions() { $('card-suggestions').hidden = true; $('card-input').setAttribute('aria-expanded', 'false'); $('card-input').removeAttribute('aria-activedescendant'); state.active = -1; }
function clearSearch() { state.searchSeq++; clearTimeout(searchTimer); state.searchAbort?.abort(); state.suggestions = []; closeSuggestions(); }
function setActive(index) {
  state.active = index;
  [...$('card-suggestions').querySelectorAll('[role=option]')].forEach((option, i) => option.setAttribute('aria-selected', String(i === index)));
  if (index >= 0) { $('card-input').setAttribute('aria-activedescendant', `card-option-${index}`); $(`card-option-${index}`)?.scrollIntoView({block: 'nearest'}); }
  else $('card-input').removeAttribute('aria-activedescendant');
}
function renderSuggestions(message) {
  const list = $('card-suggestions'); list.replaceChildren();
  if (message) list.append(node('div', 'search-message', message));
  else state.suggestions.forEach((card, i) => {
    const option = node('div', 'suggestion'); option.id = `card-option-${i}`; option.setAttribute('role', 'option'); option.setAttribute('aria-selected', 'false');
    option.append(node('strong', '', card.name), node('small', '', card.type_line || 'Carta del catálogo'));
    option.addEventListener('mousedown', event => event.preventDefault());
    option.addEventListener('click', () => addCard(card.name)); list.append(option);
  });
  list.hidden = false; $('card-input').setAttribute('aria-expanded', 'true'); state.active = -1;
}
let searchTimer;
async function searchCards() {
  const query = $('card-input').value.trim(); clearSearch(); if (query.length < 2 || state.busy) return;
  const seq = state.searchSeq; state.searchAbort = new AbortController();
  try {
    const data = await request(`/card-search?q=${encodeURIComponent(query)}`, {signal: state.searchAbort.signal});
    if (seq !== state.searchSeq) return;
    state.suggestions = data.results.filter(card => !state.cards.some(selected => selected.oracle_id === card.oracle_id));
    renderSuggestions(state.suggestions.length ? null : 'No hay coincidencias nuevas. Prueba otro nombre en inglés.');
  } catch (error) { if (error.name !== 'AbortError' && seq === state.searchSeq) renderSuggestions('No se puede consultar el catálogo. Comprueba el servidor y cards.sqlite.'); }
}
function renderCards() {
  const container = $('selected-cards'); container.replaceChildren();
  for (const card of state.cards) {
    const item = node('div', 'selected-card'); const details = node('details');
    details.append(node('summary', '', card.name)); const preview = node('div', 'card-preview');
    const faces = card.card_faces?.length ? card.card_faces : [card];
    preview.append(node('div', '', faces.map(face => [face.name, [face.mana_cost, face.type_line].filter(Boolean).join(' · '), face.oracle_text || '(Sin texto de reglas.)', face.power !== undefined && face.toughness !== undefined ? `${face.power}/${face.toughness}` : ''].filter(Boolean).join('\n')).join('\n\n')));
    const link = safeLink(card.source_url, 'Ver carta en Scryfall ↗', ''); if (link) preview.append(link);
    details.append(preview); const remove = node('button', 'remove-card', '×'); remove.type = 'button'; remove.disabled = state.busy || state.editing; remove.setAttribute('aria-label', `Quitar ${card.name}`);
    remove.addEventListener('click', () => { state.cards = state.cards.filter(c => c.oracle_id !== card.oracle_id); renderCards(); feedback(); });
    item.append(details, remove); container.append(item);
  }
  $('card-count').textContent = `${state.cards.length} / 12`; $('reference-note').hidden = state.cards.length === 0;
}
async function addCard(name = $('card-input').value.trim()) {
  if (!name || state.busy || state.editing) return;
  if (state.cards.length >= 12) { feedback('Puedes añadir hasta 12 cartas.'); return; }
  clearSearch(); state.editing = true; $('add-card').disabled = true;
  try {
    const card = await request(`/cards?name=${encodeURIComponent(name)}`);
    if (state.cards.some(c => c.oracle_id === card.oracle_id)) { feedback('Esta carta ya está añadida.'); return; }
    state.cards.push(card); $('card-input').value = ''; feedback(); announce(`${card.name} añadida.`);
  } catch (error) { feedback(error.status === 404 ? 'No encuentro esa carta. Usa su nombre completo en inglés o selecciona una sugerencia.' : 'No se puede leer el catálogo local. Comprueba cards.sqlite y el servidor.'); }
  finally { state.editing = false; $('add-card').disabled = false; renderCards(); $('card-input').focus(); }
}
function showPanel(id, badge, warning = false) {
  for (const panel of ['empty-result', 'loading-result', 'error-result', 'answer-result']) $(panel).hidden = panel !== id;
  $('result-badge').textContent = badge; $('result-badge').classList.toggle('warning', warning);
}
function setBusy(value) {
  state.busy = value; $('query-fields').disabled = value; $('result-panel').setAttribute('aria-busy', String(value));
  $('new-query').disabled = value;
  document.querySelectorAll('.example-card').forEach(button => button.disabled = value);
  renderCards();
}
function renderAnswer(data, submitted) {
  const labels = {answered: 'RESPUESTA DISPONIBLE', needs_clarification: 'NECESITA UN DATO', insufficient_evidence: 'EVIDENCIA INSUFICIENTE'};
  showPanel('answer-result', labels[data.status] || 'RESULTADO', data.status !== 'answered');
  const hasCount = Number.isInteger(data.trigger_count) && data.trigger_count >= 0;
  $('trigger-block').hidden = !hasCount; $('trigger-value').textContent = hasCount ? String(data.trigger_count) : '';
  $('clarification-block').hidden = data.status !== 'needs_clarification'; $('clarification-question').textContent = data.clarifying_question || 'Completa los datos de la situación en tu pregunta.';
  $('answer-text').textContent = data.answer || '';
  $('source-list').replaceChildren(); $('citation-chips').replaceChildren();
  const sources = Array.isArray(data.sources) ? data.sources : [];
  $('source-count').textContent = `${sources.length} ${sources.length === 1 ? 'fuente' : 'fuentes'}`;
  const sourceElements = new Map();
  sources.forEach(source => {
    const details = node('details', 'source'); const summary = node('summary');
    summary.append(node('span', 'source-id', `[${source.citation_id}]`), node('span', 'source-title', source.title || 'Fuente'));
    const body = node('div', 'source-body'); const kinds = {rule: 'COMPREHENSIVE RULES', oracle_card: 'TEXTO ORACLE', card_ruling: 'RULING DE CARTA'};
    body.append(node('p', 'source-meta', [kinds[source.kind] || 'FUENTE', source.publisher, source.published_at].filter(Boolean).join(' · ')), node('blockquote', '', source.excerpt || ''));
    const link = safeLink(source.source_url, 'Abrir fuente original ↗', 'source-link'); if (link) body.append(link);
    details.append(summary, body); $('source-list').append(details); sourceElements.set(source.citation_id, details);
  });
  for (const citation of data.citations || []) {
    const button = node('button', 'citation-chip', `[${citation}]`); button.type = 'button';
    button.addEventListener('click', () => { const source = sourceElements.get(citation); if (source) { source.open = true; source.scrollIntoView({block: 'nearest', behavior: 'smooth'}); source.querySelector('summary').focus(); } });
    $('citation-chips').append(button);
  }
  const usage = data.usage || {}; const provenance = data.provenance || {};
  const info = [['Tiempo del motor', typeof data.elapsed_seconds === 'number' ? `${data.elapsed_seconds.toFixed(2)} s` : '—'], ['Motor', provenance.pipeline_version || '—'], ['Reglas vigentes', provenance.rules_effective_date || '—'], ['Modelo', provenance.llm_model_id || '—'], ['Nuevas llamadas al modelo', usage.new_llm_calls ?? '—'], ['Respuestas desde caché', usage.llm_cache_hits ?? '—'], ['Tokens nuevos de entrada / salida', `${usage.new_llm_input_tokens ?? '—'} / ${usage.new_llm_output_tokens ?? '—'}`], ['Nuevas llamadas de embeddings', usage.new_embedding_calls ?? '—']];
  $('query-details').replaceChildren(); for (const [label, value] of info) $('query-details').append(node('dt', '', label), node('dd', '', String(value)));
  $('submitted-question').textContent = submitted.question;
  state.result = {request: submitted, response: data, received_at: new Date().toISOString()};
  announce(data.status === 'needs_clarification' ? 'El motor necesita una aclaración.' : 'Consulta completada. La respuesta y las fuentes están disponibles.');
}
function renderError(error) {
  const code = error.data?.error?.code;
  const errors = {
    aws_login_expired: ['Tu sesión de AWS ha caducado', 'En PowerShell ejecuta: aws login --profile commander-rag\nDespués reinicia el servidor y vuelve a consultar.'],
    aws_credentials: ['Revisa el acceso a AWS', 'No se han podido usar las credenciales. Comprueba el perfil commander-rag y reinicia el servidor.'],
    busy: ['Hay otra consulta en marcha', 'Espera a que termine antes de volver a intentarlo.'],
    aws_throttled: ['AWS ha limitado las solicitudes', 'Espera un momento y vuelve a intentarlo.'],
    engine_rejected_result: ['El motor no ha devuelto una respuesta válida', 'Puedes consultar el detalle técnico o el diagnóstico en la consola del servidor.'],
    invalid_engine_response: ['El motor no ha devuelto una respuesta válida', 'Revisa el registro del servidor para identificar qué validación ha fallado.'],
    engine_failure: ['No se ha podido completar la consulta', 'Revisa el registro del servidor. Allí aparecerá la causa del fallo.'],
    catalog_unavailable: ['El catálogo no está disponible', 'Comprueba que existe data/indexes/cards.sqlite.'],
    invalid_card_context: ['Revisa las cartas de referencia', 'Alguna carta no se ha podido usar. Revisa los nombres y vuelve a añadirla.'],
    unknown_or_duplicate_card: ['Revisa las cartas de referencia', 'Selecciona nombres válidos del catálogo y evita añadir una misma carta dos veces.']
  };
  let [title, message] = errors[code] || ['No se ha podido completar la consulta', error.status ? `El servidor ha respondido con HTTP ${error.status}. Revisa el detalle y el registro del servidor.` : 'No se puede conectar con la API. Comprueba que Uvicorn sigue ejecutándose.'];
  if (error.name === 'AbortError') { title = 'La consulta está tardando demasiado'; message = 'Se ha agotado la espera de diez minutos. El servidor podría seguir trabajando; revisa su consola antes de iniciar otra consulta.'; }
  $('error-title').textContent = title; $('error-message').textContent = message;
  $('error-raw').textContent = JSON.stringify(error.data || {message: error.message}, null, 2); $('error-details').open = false;
  showPanel('error-result', 'CONSULTA NO COMPLETADA', true); announce(title);
}
$('question').addEventListener('input', updateCount);
$('card-input').addEventListener('input', () => { clearSearch(); searchTimer = setTimeout(searchCards, 180); });
$('card-input').addEventListener('keydown', event => {
  if (event.key === 'Escape') closeSuggestions();
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { if ($('card-suggestions').hidden || !state.suggestions.length) return; event.preventDefault(); const delta = event.key === 'ArrowDown' ? 1 : -1; setActive((state.active + delta + state.suggestions.length) % state.suggestions.length); }
  if (event.key === 'Enter') { event.preventDefault(); if (!$('card-suggestions').hidden && state.active >= 0) addCard(state.suggestions[state.active].name); else addCard(); }
});
$('card-input').addEventListener('blur', closeSuggestions);
$('add-card').addEventListener('click', () => addCard());
document.addEventListener('click', event => { if (!event.target.closest('.card-search')) closeSuggestions(); });
$('ruling-form').addEventListener('submit', async event => {
  event.preventDefault(); if (state.busy || state.editing) return;
  const question = $('question').value.trim();
  if (!question) { feedback('Describe la situación en inglés.'); $('question').focus(); return; }
  if (!state.cards.length) { feedback('Añade al menos una carta de referencia.'); $('card-input').focus(); return; }
  const submitted = {question, cards: state.cards.map(card => card.name)};
  feedback(); clearSearch(); state.result = null; setBusy(true); showPanel('loading-result', 'CONSULTANDO');
  const start = performance.now(); $('loading-time').textContent = '0 s';
  const timer = setInterval(() => $('loading-time').textContent = `${Math.floor((performance.now() - start) / 1000)} s`, 1000);
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 600000);
  try { renderAnswer(await request('/rulings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(submitted), signal: controller.signal}), submitted); }
  catch (error) { renderError(error); }
  finally { clearInterval(timer); clearTimeout(timeout); setBusy(false); }
});
$('edit-question').addEventListener('click', () => { $('question').focus(); $('question').scrollIntoView({block: 'center', behavior: 'smooth'}); });
$('new-query').addEventListener('click', () => {
  if (state.busy || state.editing) return;
  clearSearch(); state.cards = []; state.result = null; $('question').value = ''; $('card-input').value = ''; updateCount(); renderCards(); feedback(); showPanel('empty-result', 'LISTO PARA CONSULTAR'); $('question').focus();
});
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', async () => {
  if (state.busy || state.editing) return; const example = examples[button.dataset.example]; state.editing = true; clearSearch(); feedback('Cargando cartas del ejemplo…');
  document.querySelectorAll('.example-card').forEach(b => b.disabled = true); $('query-fields').disabled = true; $('new-query').disabled = true;
  try {
    const cards = await Promise.all(example.cards.map(name => request(`/cards?name=${encodeURIComponent(name)}`)));
    state.cards = cards; $('question').value = example.question; $('card-input').value = ''; state.result = null;
    showPanel('empty-result', 'LISTO PARA CONSULTAR'); feedback(); updateCount(); announce('Ejemplo cargado. Pulsa Consultar las reglas para enviarlo.');
  } catch { feedback('No se han podido cargar las cartas del ejemplo. Comprueba el catálogo y el servidor.'); }
  finally { state.editing = false; $('query-fields').disabled = false; $('new-query').disabled = false; document.querySelectorAll('.example-card').forEach(b => b.disabled = false); renderCards(); $('question').focus(); $('question').scrollIntoView({block: 'center', behavior: 'smooth'}); }
}));
$('copy-answer').addEventListener('click', async () => {
  if (!state.result) return; const data = state.result.response;
  const text = [data.answer, data.clarifying_question, ...(data.sources || []).map(source => `[${source.citation_id}] ${source.title}\n${source.source_url || ''}`)].filter(Boolean).join('\n\n');
  try { await navigator.clipboard.writeText(text); $('copy-answer').textContent = 'Respuesta copiada'; announce('Respuesta copiada.'); setTimeout(() => $('copy-answer').textContent = 'Copiar respuesta', 2000); }
  catch { announce('No se ha podido copiar. Puedes seleccionar el texto o guardar el JSON.'); }
});
$('download-result').addEventListener('click', () => {
  if (!state.result) return; const url = URL.createObjectURL(new Blob([JSON.stringify(state.result, null, 2)], {type: 'application/json'}));
  const link = node('a'); link.href = url; link.download = `commander-rag-${new Date().toISOString().replace(/[:.]/g, '-')}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
request('/health').then(data => { if (data.status !== 'ok') throw new Error('health'); $('connection-dot').classList.add('online'); $('connection-label').textContent = 'API conectada'; }).catch(() => { $('connection-dot').classList.add('offline'); $('connection-label').textContent = 'API no disponible'; });
