/* Client contract checks in a simulated DOM. No browser layout or AWS calls.
 * Run: node tests/test_web_client.cjs
 * Uses only Node built-ins. Real browser checks are in tools/check_web_browser.py.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.listeners={}; this.attributes={}; this.hidden=false; this.disabled=false; this.value=''; this.dataset={}; this.open=false; this._text=''; this.className=''; this.classList={add:()=>{}, toggle:()=>{}}; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text + this.children.map(e=>e.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this._text=''; this.children=items; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(name, value) { this.attributes[name]=String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { document.activeElement=this; }
  scrollIntoView() {}
  remove() {}
  click() { return Promise.all((this.listeners.click||[]).map(fn=>fn({target:this,preventDefault(){}}))); }
  querySelectorAll(selector) { return this.children.flatMap(e=>[...(selector==='[role=option]' && e.attributes.role==='option' ? [e] : []), ...e.querySelectorAll(selector)]); }
  querySelector(selector) { if (selector==='summary') return this.children.find(e=>e.tagName==='summary'); return null; }
}
const elements={};
const html=fs.readFileSync(path.join(root,'web/index.html'),'utf8');
for (const [,id] of html.matchAll(/\bid="([^"]+)"/g)) elements[id]=new Element();
const exampleButtons=['death','blocking','commander'].map(key=>{const e=new Element('button');e.dataset.example=key;return e});
const document={activeElement:null,getElementById:id=>elements[id],createElement:tag=>new Element(tag),body:new Element('body'),addEventListener(){},querySelectorAll:()=>exampleButtons};
const calls=[]; let response={}; let fail=false;
const sandbox={document,URL,Blob,AbortController,console,performance,setInterval,clearInterval,setTimeout:(fn,ms)=>ms===2000||ms===1000?0:setTimeout(fn,ms),clearTimeout,navigator:{clipboard:{writeText:async()=>{}}},fetch:async(url,options={})=>{
  calls.push({url,options});
  if(url==='/health') return {ok:true,json:async()=>({status:'ok'})};
  if(url.startsWith('/cards?')) {const name=new URL('http://local'+url).searchParams.get('name');return{ok:true,json:async()=>({oracle_id:name,name,oracle_text:'Fixture text',source_url:'https://scryfall.com/'})};}
  return {ok:!fail,status:fail?503:200,json:async()=>response};
}};
vm.createContext(sandbox); vm.runInContext(fs.readFileSync(path.join(root,'web/app.js'),'utf8'),sandbox);
const run=(code)=>vm.runInContext(code,sandbox);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await tick();
  assert.equal(calls.filter(c=>c.url==='/rulings').length,0);
  assert.equal(elements['connection-label'].textContent,'API conectada');
  await exampleButtons[0].click();
  assert.equal(elements['card-count'].textContent,'3 / 12');
  assert.equal(calls.filter(c=>c.url==='/rulings').length,0);
  response={status:'answered',answer:'No creatures die.',trigger_count:0,citations:['R01'],sources:[{citation_id:'R01',kind:'rule',title:'Rule 700.4',excerpt:'Dies definition',source_url:'https://magic.wizards.com/en/rules'}],usage:{},provenance:{},elapsed_seconds:1};
  const submit=()=>elements['ruling-form'].listeners.submit[0]({preventDefault(){}});
  await submit();
  assert.equal(elements['trigger-value'].textContent,'0');assert.equal(elements['trigger-block'].hidden,false);
  const payload=JSON.parse(calls.find(c=>c.url==='/rulings').options.body);
  assert.deepEqual(Object.keys(payload).sort(),['cards','question']);assert.equal(payload.cards.length,3);
  await elements['citation-chips'].children[0].click();
  assert.equal(elements['source-list'].children[0].open,true);
  response={...response,status:'needs_clarification',trigger_count:null,clarifying_question:'How many creatures?'};
  await submit(); assert.equal(elements['trigger-block'].hidden,true);assert.equal(elements['clarification-block'].hidden,false);
  await elements['edit-question'].click();assert.equal(document.activeElement,elements.question);
  response={...response,status:'answered',answer:'<img src=x onerror=alert(1)>',sources:[{citation_id:'R01',title:'<script>unsafe</script>',excerpt:'<b>text</b>',source_url:'javascript:alert(1)'}]};
  await submit();assert.equal(elements['answer-text'].textContent,response.answer);assert.equal(elements['answer-text'].children.length,0);
  const body=elements['source-list'].children[0].children[1];assert.equal(body.children.some(e=>e.tagName==='a'),false);
  fail=true;response={error:{code:'aws_login_expired',message:'Expired'}};
  await submit();assert.match(elements['error-message'].textContent,/aws login --profile commander-rag/);assert.equal(elements['query-fields'].disabled,false);
  await elements['new-query'].click();assert.equal(elements['card-count'].textContent,'0 / 12');assert.equal(elements.question.value,'');assert.equal(elements['empty-result'].hidden,false);
  console.log('PASS: 8 client contracts (simulated DOM, no layout/browser or AWS).');
})().catch(error=>{console.error(error);process.exitCode=1});
