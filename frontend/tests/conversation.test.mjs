import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

function setup(options={}) {
  const nodes=new Map(),calls=[],voices=[];
  let generation=0;
  const node=id=>{
    if(!nodes.has(id))nodes.set(id,{value:'customer-001',textContent:'',innerHTML:'',disabled:false,scrollHeight:0,classList:{toggle(){}},querySelectorAll:()=>[]});
    return nodes.get(id);
  };
  class Voice {
    constructor(events){this.events=events;this.recording=false;this.listening=false;this.muted=false;this.isPlaying=false;this.hasReplay=false;this.played=[];voices.push(this);}
    async open(){await options.open?.();}
    listen(){this.listening=true;}
    setMuted(value){this.muted=value;this.recording=false;}
    clearReplay(){this.hasReplay=false;}
    stopPlayback(){this.isPlaying=false;this.stops=(this.stops??0)+1;}
    async playPcm(audio){this.played.push(audio);this.isPlaying=true;this.hasReplay=true;this.events.onPlayback('started');if(options.completeDuringPlay)this.complete();}
    complete(){this.isPlaying=false;this.events.onPlayback('completed');}
    replay(){this.replays=(this.replays??0)+1;this.isPlaying=true;}
    releaseMicrophone(){this.listening=false;}
    close(){this.closed=true;this.listening=false;this.isPlaying=false;}
  }
  const context={
    document:{querySelector:()=>node('app'),getElementById:node},window:{location:{search:''},addEventListener(){}},
    URLSearchParams,AbortController,performance,console,setInterval:()=>1,clearInterval(){},BrowserVoice:Voice,
    escape:value=>String(value),renderToolCalls:()=>'',publicTraceEvents:value=>value,installAcceptance(){},setToken(){},
    request:async(path,body,signal)=>{
      calls.push({path,body,signal});const custom=options.request?.(path,body,signal);if(custom!==undefined)return await custom;
      if(path==='/api/scenarios')return {scenarios:[{id:'didi_driver',title:'司机',industry:'出行'}],default_scenario:'didi_driver'};
      if(path==='/api/sessions')return {session_id:'session-1',token:'test',generation:0};
      if(path.endsWith('/state'))return options.state??{session:{session_id:'session-1',state:'active',reason:null},messages:[]};
      if(path.includes('/events'))return {events:[],last_seq:0};
      if(path.endsWith('/voice/interrupt'))return {generation:++generation};
      if(path.includes('/voice/'))return {reply_message_id:null};
      return {};
    },
    response:async(path,body,signal)=>{calls.push({path,body,signal,audio:true});return await options.response?.(path,body,signal)??{body:{cancel:async()=>{}}};},
    fetch:async()=>({}),
  };
  const source=readFileSync(new URL('../src/main.ts',import.meta.url),'utf8').replace(/^import[^\n]*\n/gm,'')+'\nglobalThis.hooks={refresh,render, getProcessing:()=>processing};';
  vm.createContext(context);vm.runInContext(ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText,context);
  const speech=()=>{const voice=voices.at(-1);voice.recording=true;voice.events.onSpeechStart();};
  const sent=blob=>{const voice=voices.at(-1);voice.recording=false;voice.events.onRecorded(blob);};
  return {nodes,node,calls,voices,context,speech,sent,start:()=>node('start').onclick()};
}

test('microphone callbacks activate only after session creation completes',async()=>{
  const create=deferred();const f=setup({request:path=>path==='/api/sessions'?create.promise:undefined});
  await tick();const pending=f.start();await tick();assert.equal(f.voices[0].listening,false);
  create.resolve({session_id:'session-1',token:'test',generation:0});await pending;
  assert.equal(f.voices[0].listening,true);assert.equal(f.node('mute').disabled,false);
});

test('three interruptions preserve every queued recording once and only the last generates a reply',async()=>{
  const cancellations=[deferred(),deferred(),deferred()];let index=0;
  const f=setup({request:path=>path.endsWith('/voice/interrupt')?cancellations[index++].promise:undefined});
  await tick();await f.start();f.calls.length=0;
  const recordings=[new Blob(['Beijing']),new Blob(['part time']),new Blob(['evenings'])];
  for(const blob of recordings){f.speech();await tick();f.sent(blob);}
  assert.equal(index,1);assert.equal(f.calls.filter(call=>call.path.includes('/voice/turn')).length,0);
  cancellations[0].resolve({generation:1});await tick();assert.equal(index,2);
  cancellations[1].resolve({generation:2});await tick();assert.equal(index,3);
  cancellations[2].resolve({generation:3});await tick();
  const turns=f.calls.filter(call=>call.path.includes('/voice/turn'));
  assert.equal(turns.length,3);assert.deepEqual(turns.map(call=>call.body),recordings);
  assert.match(turns[0].path,/generation=3&input_only=true$/);
  assert.match(turns[1].path,/generation=3&input_only=true$/);
  assert.match(turns[2].path,/generation=3$/);
});

test('speech cancels the opening and its late reply cannot play or replace current state',async()=>{
  const opening=deferred(),turn=deferred();
  const f=setup({request:path=>path.includes('/voice/start')?opening.promise:path.includes('/voice/turn')?turn.promise:undefined});
  await tick();const started=f.start();await tick();
  const old=f.calls.find(call=>call.path.includes('/voice/start'));
  f.speech();await tick();f.sent(new Blob(['next']));await tick();
  assert.equal(old.signal.aborted,true);assert.equal(f.context.hooks.getProcessing(),true);
  opening.resolve({reply_message_id:'old'});await started;await tick();
  assert.equal(f.calls.filter(call=>call.audio).length,0);assert.equal(f.context.hooks.getProcessing(),true);
  turn.resolve({reply_message_id:null});await tick();
  assert.equal(f.context.hooks.getProcessing(),false);assert.match(f.node('voice-status').textContent,/正在听/);
});

test('a submitted recording is never resent when later speech interrupts its ASR',async()=>{
  const firstTurn=deferred(),cancel=deferred();let interruptions=0;
  const first=new Blob(['already uploaded']),second=new Blob(['new phrase']);
  const f=setup({request:(path,body)=>path.includes('/voice/turn')&&body===first?firstTurn.promise:path.endsWith('/voice/interrupt')&&++interruptions===2?cancel.promise:undefined});
  await tick();await f.start();f.speech();await tick();f.sent(first);await tick();
  f.speech();await tick();f.sent(second);await tick();
  firstTurn.reject(new DOMException('cancelled','AbortError'));await tick();
  assert.equal(f.calls.filter(call=>call.path.includes('/voice/turn')).length,1);
  cancel.resolve({generation:2});await tick();
  const uploads=f.calls.filter(call=>call.path.includes('/voice/turn')).map(call=>call.body);
  assert.deepEqual(uploads,[first,second]);
});

test('ASR failure restores listening state while the microphone remains active',async()=>{
  const f=setup({request:path=>path.includes('/voice/turn')?Promise.reject(new Error('ASR unavailable')):undefined});
  await tick();await f.start();f.speech();await tick();f.sent(new Blob(['speech']));await tick();
  assert.match(f.node('notice').textContent,/ASR unavailable/);assert.match(f.node('voice-status').textContent,/正在听/);
  assert.equal(f.voices[0].listening,true);assert.equal(f.context.hooks.getProcessing(),false);
});

test('TTS failure can retry the same reply without running Agent and completed replay uses the cache',async()=>{
  let attempts=0;
  const f=setup({request:path=>path.includes('/voice/start')?{reply_message_id:'reply-1'}:undefined,response:()=>{if(++attempts===1)throw new Error('TTS unavailable');return {};}});
  await tick();await f.start();
  assert.equal(f.node('replay').disabled,false);assert.match(f.node('voice-status').textContent,/正在听/);
  await f.node('replay').onclick();assert.equal(attempts,2);
  const turns=f.calls.filter(call=>/\/voice\/(start|turn)/.test(call.path)).length;
  await f.node('replay').onclick();assert.equal(attempts,2);assert.equal(f.voices[0].replays,1);
  assert.equal(f.calls.filter(call=>/\/voice\/(start|turn)/.test(call.path)).length,turns);
});

test('state and trace refresh failure does not block the reply audio',async()=>{
  const f=setup({request:path=>path.endsWith('/state')?Promise.reject(new Error('display unavailable')):path.includes('/voice/start')?{reply_message_id:'reply-1'}:undefined});
  await tick();await f.start();assert.equal(f.voices[0].played.length,1);
});

test('hangup stops local media before the server stop response arrives',async()=>{
  const stop=deferred();const f=setup({request:path=>path.endsWith('/stop')?stop.promise:undefined});
  await tick();await f.start();const pending=f.node('voice-stop').onclick();
  assert.equal(f.voices[0].closed,true);assert.equal(f.node('start').disabled,false);assert.equal(f.node('voice-status').textContent,'通话已结束');
  stop.resolve({});await pending;
});

test('server closure stops media and allows a new call without an extra hangup click',async()=>{
  const f=setup({request:path=>path.includes('/voice/start')?{reply_message_id:'reply-1'}:undefined});
  await tick();await f.start();assert.equal(f.voices[0].isPlaying,true);
  f.context.hooks.render({session:{session_id:'session-1',state:'closed',reason:'timeout'},messages:[]});
  assert.equal(f.voices[0].closed,true);assert.equal(f.node('start').disabled,false);assert.equal(f.node('voice-status').textContent,'通话已结束');
});

test('muting unfinished speech still submits earlier completed recordings after cancellation',async()=>{
  const cancel=deferred();let count=0;
  const f=setup({request:path=>path.endsWith('/voice/interrupt')?++count===1?cancel.promise:{generation:2}:undefined});
  await tick();await f.start();f.speech();await tick();const complete=new Blob(['completed phrase']);f.sent(complete);
  f.speech();await tick();cancel.resolve({generation:1});await tick();
  assert.equal(f.calls.filter(call=>call.path.includes('/voice/turn')).length,0);
  f.node('mute').onclick();await tick();
  const uploads=f.calls.filter(call=>call.path.includes('/voice/turn'));
  assert.equal(uploads.length,1);assert.equal(uploads[0].body,complete);
});
test('Agent farewell waits for actual playback completion before closing the call',async()=>{
  const f=setup({request:path=>path.includes('/voice/start')?{reply_message_id:'farewell',end_after_playback:true}:undefined});
  await tick();await f.start();
  assert.equal(f.voices[0].listening,true);assert.equal(f.voices[0].isPlaying,true);
  assert.equal(f.calls.filter(call=>call.path.endsWith('/stop')).length,0);
  f.voices[0].complete();await tick();
  const stops=f.calls.filter(call=>call.path.endsWith('/stop'));
  assert.equal(stops.length,1);assert.equal(stops[0].body.reason,'agent_completed');assert.equal(stops[0].body.generation,0);
  assert.equal(f.voices[0].closed,true);assert.equal(f.node('start').disabled,false);
});

test('a short farewell may complete before playPcm resolves without losing the ending intent',async()=>{
  const f=setup({completeDuringPlay:true,request:path=>path.includes('/voice/start')?{reply_message_id:'farewell',end_after_playback:true}:undefined});
  await tick();await f.start();await tick();
  assert.equal(f.calls.filter(call=>call.path.endsWith('/stop')).length,1);
  assert.equal(f.voices[0].closed,true);assert.equal(f.node('voice-status').textContent,'通话已结束');
});

test('interrupting a farewell clears its ending intent so a later playback completion cannot hang up',async()=>{
  const f=setup({request:path=>path.includes('/voice/start')?{reply_message_id:'farewell',end_after_playback:true}:path.includes('/voice/turn')?{reply_message_id:'answer',end_after_playback:false}:undefined});
  await tick();await f.start();f.speech();await tick();f.sent(new Blob(['one more question']));await tick();
  f.voices[0].complete();await tick();
  assert.equal(f.calls.filter(call=>call.path.endsWith('/stop')).length,0);
  assert.equal(f.voices[0].listening,true);assert.equal(f.voices[0].closed,undefined);
});

test('failed farewell TTS keeps the call open and retry ends it after the same reply finishes',async()=>{
  let attempts=0;
  const f=setup({request:path=>path.includes('/voice/start')?{reply_message_id:'farewell',end_after_playback:true}:undefined,response:()=>{if(++attempts===1)throw new Error('TTS unavailable');return {};}});
  await tick();await f.start();
  assert.equal(f.voices[0].listening,true);assert.equal(f.calls.filter(call=>call.path.endsWith('/stop')).length,0);
  await f.node('replay').onclick();assert.equal(attempts,2);
  assert.equal(f.calls.filter(call=>call.path.endsWith('/stop')).length,0);
  f.voices[0].complete();await tick();
  assert.equal(f.calls.filter(call=>call.path.endsWith('/stop')).length,1);
  assert.equal(f.calls.filter(call=>call.path.includes('/voice/start')).length,1);
});

test('manual hangup clears a farewell intent and a late completion does not send another stop',async()=>{
  const f=setup({request:path=>path.includes('/voice/start')?{reply_message_id:'farewell',end_after_playback:true}:undefined});
  await tick();await f.start();await f.node('voice-stop').onclick();
  f.voices[0].complete();await tick();
  const stops=f.calls.filter(call=>call.path.endsWith('/stop'));
  assert.equal(stops.length,1);assert.equal(stops[0].body.reason,'voice_stop_button');
});
