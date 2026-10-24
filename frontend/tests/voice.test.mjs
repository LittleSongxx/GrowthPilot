import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const compile = source => ts.transpileModule(source, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const moduleUrl = source => 'data:text/javascript;base64,' + Buffer.from(compile(source)).toString('base64');
const processingUrl = moduleUrl(readFileSync(new URL('../src/audio-processing.ts', import.meta.url), 'utf8'));
const {SpeechSegmenter, encodeWav, PcmDecoder} = await import(processingUrl);
const {BrowserVoice} = await import(moduleUrl(readFileSync(new URL('../src/voice.ts', import.meta.url), 'utf8').replace("'./audio-processing'", JSON.stringify(processingUrl))));
const tick = () => new Promise(resolve => setImmediate(resolve));
const frame = (value = 0, rate = 16000) => new Float32Array(rate * 0.02).fill(value);

function setupVoice() {
  const tracks = [{stopped:false,stop(){this.stopped=true;}}], stream = {getTracks:()=>tracks};
  const starts=[], recorded=[], playback=[], errors=[], contexts=[], captures=[], buffers=[];
  class Context {
    constructor(options) { this.sampleRate=options.sampleRate; this.currentTime=0; this.destination={}; this.audioWorklet={addModule:async()=>{}}; contexts.push(this); }
    resume(){this.resumed=true;return Promise.resolve();}
    close(){this.closed=true;return Promise.resolve();}
    createMediaStreamSource(){return {connect(){},disconnect(){this.disconnected=true;}};}
    createBuffer(channels,length,sampleRate){const data=new Float32Array(length);return {sampleRate,data,getChannelData:()=>data};}
    createBufferSource(){const source={connect(){},disconnect(){this.disconnected=true;},start(time){this.time=time;},stop(){this.stopped=true;},finish(){this.onended?.();}};buffers.push(source);return source;}
  }
  class Capture {
    constructor(){this.port={onmessage:null};captures.push(this);}
    connect(){} disconnect(){this.disconnected=true;}
  }
  globalThis.AudioContext=Context;globalThis.AudioWorkletNode=Capture;
  globalThis.window={AudioContext:Context,AudioWorkletNode:Capture};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{mediaDevices:{getUserMedia:async()=>stream}}});
  const voice = new BrowserVoice({
    onSpeechStart(){starts.push(true);voice.stopPlayback();},onRecorded:blob=>recorded.push(blob),
    onPlayback:event=>playback.push(event),onError:error=>errors.push(error),
  });
  const feed = (value, count) => {for(let i=0;i<count;i++) captures.at(-1).port.onmessage?.({data:frame(value)});};
  return {voice,tracks,stream,starts,recorded,playback,errors,contexts,captures,buffers,feed};
}
function pcmResponse() {
  let controller;
  const body=new ReadableStream({start(value){controller=value;}});
  return {controller,response:new Response(body,{headers:{'X-Audio-Sample-Rate':'24000'}})};
}

test('VAD ignores silence and brief noise, preserves the sentence start and submits after silence',()=>{
  const starts=[],sent=[];
  const vad=new SpeechSegmenter(16000,()=>starts.push(true),samples=>sent.push(samples));
  for(let i=0;i<20;i++)vad.push(frame());
  for(let i=0;i<3;i++)vad.push(frame(0.1));
  for(let i=0;i<20;i++)vad.push(frame());
  assert.equal(starts.length,0);
  for(let i=0;i<6;i++)vad.push(frame(0.2));
  assert.equal(starts.length,1);assert.equal(vad.speaking,true);
  for(let i=0;i<34;i++)vad.push(frame());
  assert.equal(sent.length,0);
  vad.push(frame());
  assert.equal(sent.length,1);assert.equal(vad.speaking,false);
  assert.equal(sent[0].length,16000*0.94);
  assert.ok(sent[0].slice(16000*0.12,16000*0.24).every(value=>Math.abs(value-0.2)<0.00001));
});

test('60 seconds of continuous speech yields a WAV below the 2 MB upload limit',async()=>{
  const sent=[];const vad=new SpeechSegmenter(16000,()=>{},samples=>sent.push(samples));
  for(let i=0;i<3000;i++)vad.push(frame(0.2));
  assert.equal(sent.length,1);
  const wav=encodeWav(sent[0],16000),view=new DataView(await wav.arrayBuffer());
  assert.equal(wav.type,'audio/wav');assert.equal(wav.size,1920044);assert.ok(wav.size<2000000);
  assert.equal(view.getUint32(24,true),16000);assert.equal(view.getUint16(22,true),1);
});

test('PCM decoder preserves signed samples split across odd byte boundaries',()=>{
  const decoder=new PcmDecoder();
  assert.deepEqual([...decoder.push(new Uint8Array([0]))],[]);
  assert.deepEqual([...decoder.push(new Uint8Array([128,255,127,0]))],[-1,32767/32768]);
  assert.deepEqual([...decoder.push(new Uint8Array([0]))],[0]);decoder.finish();
  decoder.push(new Uint8Array([1]));assert.throws(()=>decoder.finish(),/不完整/);
});

test('a call listens only after activation, submits automatically and continues listening',async()=>{
  const f=setupVoice();const opening=f.voice.open();assert.equal(f.contexts[0].resumed,true);await opening;
  f.feed(0.2,20);assert.equal(f.starts.length,0);
  f.voice.listen();f.feed(0.2,10);f.feed(0,35);
  assert.equal(f.recorded.length,1);assert.equal(f.recorded[0].type,'audio/wav');assert.equal(f.voice.listening,true);
  f.feed(0.2,10);f.feed(0,35);assert.equal(f.recorded.length,2);
  f.voice.setMuted(true);f.feed(0.2,10);assert.equal(f.starts.length,2);
  f.voice.setMuted(false);f.feed(0.2,6);assert.equal(f.starts.length,3);f.voice.close();
});

test('PCM starts playing before the stream completes and later chunks are scheduled in order',async()=>{
  const f=setupVoice();await f.voice.open();f.voice.listen();
  const input=pcmResponse();const pending=f.voice.playPcm(input.response);
  input.controller.enqueue(new Uint8Array(4800));await tick();
  assert.equal(f.buffers.length,1);assert.equal(f.voice.isPlaying,true);assert.equal(f.voice.hasReplay,false);
  input.controller.enqueue(new Uint8Array(4800));await tick();
  assert.equal(f.buffers.length,2);assert.ok(f.buffers[1].time>=f.buffers[0].time+0.1-0.00001);
  input.controller.close();await pending;assert.equal(f.voice.hasReplay,true);
  f.buffers.forEach(source=>source.finish());assert.equal(f.voice.isPlaying,false);
  const before=f.buffers.length;f.voice.replay();assert.equal(f.buffers.length,before+2);
  assert.deepEqual(f.playback,['started','completed','started']);f.voice.close();
});

test('valid speech immediately stops queued playback while microphone stays active',async()=>{
  const f=setupVoice();await f.voice.open();f.voice.listen();
  const input=pcmResponse();const pending=f.voice.playPcm(input.response);
  input.controller.enqueue(new Uint8Array(9600));await tick();
  f.feed(0.2,6);await pending;
  assert.equal(f.voice.isPlaying,false);assert.equal(f.buffers[0].stopped,true);
  assert.equal(f.voice.listening,true);assert.equal(f.tracks[0].stopped,false);assert.equal(f.voice.hasReplay,false);
  f.feed(0,35);assert.equal(f.recorded.length,1);f.voice.close();
});

test('a late chunk from a cancelled stream cannot enter the next playback queue',async()=>{
  const f=setupVoice();await f.voice.open();let resolveRead;
  const old={headers:new Headers({'X-Audio-Sample-Rate':'24000'}),body:{getReader:()=>({read:()=>new Promise(resolve=>resolveRead=resolve),cancel:async()=>{},releaseLock(){}})}};
  const pending=f.voice.playPcm(old);f.voice.stopPlayback();
  const next=pcmResponse();const nextPending=f.voice.playPcm(next.response);
  next.controller.enqueue(new Uint8Array(4800));await tick();
  resolveRead({value:new Uint8Array(9600),done:false});await pending;
  assert.equal(f.buffers.length,1);assert.equal(f.voice.isPlaying,true);
  next.controller.close();await nextPending;f.voice.close();
});

test('stream failure discards incomplete replay and leaves the microphone available',async()=>{
  const f=setupVoice();await f.voice.open();f.voice.listen();const input=pcmResponse();
  const pending=f.voice.playPcm(input.response);input.controller.enqueue(new Uint8Array(4800));await tick();
  input.controller.error(new Error('connection lost'));await assert.rejects(pending,/connection lost/);
  assert.equal(f.voice.hasReplay,false);assert.equal(f.voice.isPlaying,false);assert.equal(f.voice.listening,true);f.voice.close();
});

test('closing releases microphone, worklet, context and queued audio',async()=>{
  const f=setupVoice();await f.voice.open();f.voice.listen();f.feed(0.2,6);
  const input=pcmResponse();const pending=f.voice.playPcm(input.response);input.controller.enqueue(new Uint8Array(4800));await tick();
  f.voice.close();await pending;
  assert.equal(f.tracks[0].stopped,true);assert.equal(f.captures[0].disconnected,true);assert.equal(f.contexts[0].closed,true);
  assert.equal(f.buffers[0].stopped,true);assert.equal(f.recorded.length,0);assert.equal(f.voice.listening,false);
});

test('late microphone permission cannot reopen a closed call',async()=>{
  const f=setupVoice();let resolve;
  navigator.mediaDevices.getUserMedia=()=>new Promise(done=>resolve=done);
  const pending=f.voice.open();f.voice.close();resolve(f.stream);await pending;
  assert.equal(f.tracks[0].stopped,true);assert.equal(f.contexts[0].closed,true);assert.equal(f.captures.length,0);
});

test('disconnecting a microphone reports an actionable error and stops listening',async()=>{
  const f=setupVoice();await f.voice.open();f.voice.listen();f.tracks[0].onended();
  assert.equal(f.errors.length,1);assert.match(f.errors[0].message,/麦克风连接已断开/);
  assert.equal(f.voice.listening,false);assert.equal(f.tracks[0].onended,null);f.voice.close();
});
