"""Optional browser integration check. Real local catalog; POST /rulings simulated.
Requires playwright and Chromium. Never contacts Bedrock.
Run from project root: python tools/check_web_browser.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT.parent / 'mtg_ui_preview'
OUTPUT.mkdir(exist_ok=True)
sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]; sock.close()
server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'api:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = f'http://127.0.0.1:{port}'
result = {
    'status': 'answered', 'answer': 'No creatures die. Rest in Peace exiles them instead of letting them enter the graveyard.',
    'trigger_count': 0, 'clarifying_question': None, 'citations': ['C02', 'R01'],
    'sources': [
        {'citation_id': 'C02', 'title': 'Rest in Peace — Oracle text', 'kind': 'oracle_card', 'excerpt': 'If a card or token would be put into a graveyard from anywhere, exile it instead.', 'source_url': 'https://scryfall.com/card/big/4/rest-in-peace'},
        {'citation_id': 'R01', 'title': 'Comprehensive Rules 700.4', 'kind': 'rule', 'excerpt': 'The term dies means “is put into a graveyard from the battlefield.”', 'source_url': 'https://magic.wizards.com/en/rules'}
    ],
    'provenance': {'pipeline_version': '1.5.4', 'rules_effective_date': '2026-11-06', 'llm_model_id': 'eu.amazon.nova-2-lite-v1:0'},
    'usage': {'new_llm_calls': 3, 'llm_cache_hits': 0, 'new_llm_input_tokens': 12000, 'new_llm_output_tokens': 600, 'new_embedding_calls': 2}, 'elapsed_seconds': 4.2
}
mode = {'error': False}
posts = []
errors = []
checks = []
try:
    for _ in range(100):
        try:
            urllib.request.urlopen(base + '/health', timeout=.2); break
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError('Local server did not start')
    with sync_playwright() as p:
        launch_options = {'headless': True}
        if os.environ.get('MTG_CHROMIUM_PATH'):
            launch_options['executable_path'] = os.environ['MTG_CHROMIUM_PATH']
        browser = p.chromium.launch(**launch_options)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def respond(route):
            posts.append(route.request.post_data_json)
            body = {'error': {'code': 'aws_login_expired', 'message': 'Session expired'}} if mode['error'] else result
            route.fulfill(status=503 if mode['error'] else 200, content_type='application/json', body=json.dumps(body))
        page.route('**/rulings', respond)
        page.goto(base)
        page.wait_for_function("document.getElementById('connection-label').textContent === 'API conectada'")
        page.screenshot(path=str(OUTPUT / 'commander-rag-desktop.png'), full_page=True)
        assert not posts
        checks.append('Initial page and local health; no inference')
        page.locator('#card-input').fill('Blood Art')
        page.locator('[role=option]').first.wait_for()
        page.locator('#card-input').press('ArrowDown')
        page.locator('#card-input').press('Enter')
        page.locator('#selected-cards summary').filter(has_text='Blood Artist').wait_for()
        assert page.locator('#card-count').inner_text() == '1 / 12'
        page.locator('#card-input').fill('Blood Artist')
        page.locator('#card-input').press('Enter')
        page.wait_for_function("document.getElementById('form-feedback').textContent.includes('ya está')")
        assert page.locator('#card-count').inner_text() == '1 / 12'
        checks.append('Real SQLite autocomplete, keyboard selection and duplicate prevention')
        page.locator('[data-example=death]').click()
        page.wait_for_function("document.getElementById('card-count').textContent === '3 / 12' && !document.getElementById('query-fields').disabled")
        assert not posts
        checks.append('Example loads real card text without inference')
        page.locator('#submit-query').click()
        page.locator('#answer-result').wait_for(state='visible')
        assert page.locator('#trigger-value').inner_text() == '0'
        assert set(posts[-1]) == {'question', 'cards'}
        assert posts[-1]['cards'] == ['Blood Artist', 'Rest in Peace', 'Wrath of God']
        page.locator('.citation-chip').first.click()
        assert page.locator('.source').first.get_attribute('open') is not None
        page.screenshot(path=str(OUTPUT / 'commander-rag-answer.png'), full_page=True)
        with page.expect_download() as download_info:
            page.locator('#download-result').click()
        download = download_info.value
        export_path = OUTPUT / 'simulated-browser-result.json'
        download.save_as(str(export_path))
        assert json.loads(export_path.read_text())['response']['trigger_count'] == 0
        checks.append('POST contract, zero triggers, expandable citations and JSON export')
        result.update(status='needs_clarification', trigger_count=None, clarifying_question='How many other creatures are on the battlefield?', answer='The number of creatures is required.')
        page.locator('#submit-query').click()
        page.locator('#clarification-block').wait_for(state='visible')
        assert page.locator('#trigger-block').is_hidden()
        page.locator('#edit-question').click()
        assert page.locator('#question').evaluate('(e) => e === document.activeElement')
        checks.append('Clarification shown and unknown count not displayed as zero')
        result.update(status='answered', trigger_count=None, clarifying_question=None, answer='<img src=x onerror="window.uiXss=1">This remains plain text.')
        result['sources'][0]['source_url'] = 'javascript:window.uiXss=2'
        page.locator('#submit-query').click()
        page.wait_for_function("document.getElementById('answer-text').textContent.startsWith('<img')")
        assert page.evaluate('window.uiXss') is None
        assert page.locator('#answer-text img').count() == 0
        assert page.locator('.source').first.locator('a').count() == 0
        checks.append('Model text is escaped and non-HTTPS source URLs are omitted')
        mode['error'] = True
        page.locator('#submit-query').click()
        page.locator('#error-result').wait_for(state='visible')
        assert 'aws login --profile commander-rag' in page.locator('#error-message').inner_text()
        assert not page.locator('#query-fields').is_disabled()
        checks.append('Expired AWS session message and form recovery')
        mode['error'] = False
        page.locator('#new-query').click()
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(OUTPUT / 'commander-rag-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('[data-example=blocking]').click()
        page.wait_for_function("document.getElementById('card-count').textContent === '2 / 12' && !document.getElementById('query-fields').disabled")
        result.update(status='answered', trigger_count=None, answer='Yes, Giant Spider can block Wind Drake because reach allows it to block creatures with flying.')
        page.locator('#submit-query').click()
        page.wait_for_function("document.getElementById('answer-text').textContent.startsWith('Yes,')")
        assert page.locator('#trigger-block').is_hidden()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        checks.append('Mobile layout and non-count answer')
        page.locator('[data-example=commander]').click()
        page.wait_for_function("document.getElementById('card-count').textContent === '3 / 12' && !document.getElementById('query-fields').disabled")
        assert 'Yargle' in page.locator('#selected-cards').inner_text()
        checks.append('Commander example resolves all three real card names')
        assert not errors, errors
        browser.close()
    print(json.dumps({'browser_checks': checks, 'page_errors': errors, 'inference': 'Simulated POST only; no AWS calls', 'screenshots': str(OUTPUT)}, indent=2, ensure_ascii=False))
finally:
    server.terminate()
    server.wait(timeout=10)
