import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../src/acceptance.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {installAcceptance} = await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));

function setupAcceptance(requestedCase) {
  const selection = {value:'dialogue-growth-v2-D01'};
  const output = {textContent:''};
  const form = {onsubmit:null, querySelector:()=>selection};
  const target = {innerHTML:'', querySelector:selector=>selector === 'form' ? form : output};
  globalThis.window = {location:{search:'?voice_case='+encodeURIComponent(requestedCase)}};
  Object.defineProperty(globalThis, 'navigator', {configurable:true, value:{userAgent:'test-browser', platform:'test-machine'}});
  return {selection, output, form, target};
}

test('CLI case link selects the current case and saves the versioned observation payload', async () => {
  const f = setupAcceptance('dialogue-growth-v2-R01');
  let saved;
  installAcceptance(f.target, ()=>({session:'voice-rider', scenario:'meituan_rider'}), async data=>{saved=data;});
  assert.equal(f.selection.value, 'dialogue-growth-v2-R01');
  const ids = [...f.target.innerHTML.matchAll(/value="(dialogue-growth-v2-[DRMF]\d{2})"/g)].map(match=>match[1]);
  assert.equal(new Set(ids).size, 19);
  globalThis.FormData = class {
    entries() { return Object.entries({case_id:f.selection.value, reviewer:'tester', machine:'test-machine', network:'local', audio_device:'test-headset', result:'pass', human_attestation:'on', notes:'test observation only', timing:''}); }
  };
  await f.form.onsubmit({preventDefault(){}});
  assert.equal(saved.case_id, 'dialogue-growth-v2-R01');
  assert.equal(saved.review_version, 'dialogue-growth-v2');
  assert.equal(saved.scenario_id, 'meituan_rider');
  assert.equal(saved.pipeline, 'asr-agent-tts');
  assert.equal(saved.origin, 'human_self_report');
});

test('retired or unknown URL cases cannot be selected as current observations', () => {
  for (const requestedCase of ['D01', 'dialogue-growth-v1-F01', 'unknown']) {
    const f = setupAcceptance(requestedCase);
    installAcceptance(f.target, ()=>({session:'voice', scenario:'didi_driver'}), async()=>{});
    assert.equal(f.selection.value, 'dialogue-growth-v2-D01');
  }
});
