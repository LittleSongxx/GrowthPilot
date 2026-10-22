import './style.css';
import {request, response, setToken, type Scenario, type SessionState, type Trace, type Turn} from './api';
import {BrowserVoice} from './voice';
import {installAcceptance} from './acceptance';
import {escapeHTML as escape, publicTraceEvents, renderToolCalls} from './tool-results';

const app = document.querySelector<HTMLDivElement>('#app')!;
app.innerHTML = `
<div class="layout">
 <aside class="sidebar"><div class="brand"><span class="brandmark">↗</span> Growth Agent</div>
  <nav aria-label="业务场景"><div class="eyebrow">SCENARIO WORKSPACE</div><div id="scenarios" class="scenarios"></div></nav>
  <div class="side-note"><strong>客户增长工作台</strong>了解客户需求，解答业务问题，引导下载、注册和使用 App。<br><br>01 / 需求 → 解惑 → 引导</div>
 </aside>
 <main class="workspace">
  <header class="topbar"><span class="breadcrumb">客户工作台 / 通用外呼 Agent</span><span class="badge">用增对话工作台</span></header>
  <section class="hero"><div><div class="eyebrow">CONVERSATION TO ACTION</div><h1 id="title">从一个问题，走向下一步。</h1><p id="subtitle">了解需求、解答顾虑，帮助客户下载、注册和使用 App。</p></div>
   <div class="session-control"><label>客户标识<input id="customer" value="customer-001" maxlength="80" autocomplete="off" aria-label="客户标识"/></label><button id="start" class="primary">开始通话 ↗</button></div>
  </section>
  <div id="notice" class="notice" role="alert"></div>
  <div class="grid"><section class="panel conversation" aria-label="Agent 对话">
   <div class="panel-title"><span>对话空间 <span id="session-label" class="label"></span></span><span class="label">语音交流</span></div>
   <div id="voice" class="voice"><div id="voice-orb" class="voice-orb">◉</div><div id="voice-status" role="status">点击开始通话，允许使用麦克风</div><p>开始通话后直接说话，停顿后自动发送 · 回复时也可以开口打断 · 建议使用耳机</p><p>转写、回复与工具查询会同步显示在下方。</p><div class="voice-actions"><button id="mute" class="secondary" disabled>静音</button><button id="replay" class="secondary" disabled>重播回复</button><button id="voice-stop" class="primary" disabled>结束通话</button></div></div>
   <div id="messages" class="messages" aria-live="polite"><div class="empty"><div class="empty-icon">✳</div><h2>从一个真实问题开始</h2><p>选择场景并开始会话。Agent 会按需要查询政策、当地业务资料和沟通技能。</p></div></div>
  </section>
  <aside class="right-column">
   <section class="panel"><div class="panel-title">业务查询 <span class="label">TOOL RESULTS</span></div><p class="data-note">按客户问题查询政策、服务和费用信息，请结合结果中的适用条件理解。</p><div id="queries" class="panel-content"><div class="muted">Agent 会根据客户的问题查询所需资料。</div></div></section>
   <section class="panel"><div class="panel-title">客户画像 <span class="label">WITH SOURCE</span></div><p id="profile-status" class="data-note">通话前自动载入，通话结束后更新。</p><div id="memory" class="panel-content"><div class="muted">有来源的自述会跨会话保留。</div></div></section>
  </aside></div>
  <details class="panel evidence"><summary>查看证据与调用轨迹 <span class="label">仅展示当前会话公开状态</span></summary><pre id="evidence">尚无轨迹</pre></details>
  <details id="acceptance" class="panel evidence"></details>
  <footer class="footer"><span id="connection" class="statusline"><span class="pulse"></span> 业务工作空间</span><span>AgentScope · ASR → Agent → TTS · 用增对话</span></footer>
 </main>
</div>`;

const el = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id)! as T;
let scenarios: Scenario[] = [], selected = '', session = '', sessionToken = '';
let state: SessionState | null = null, transport: BrowserVoice | null = null;
let starting = false, processing = false, lastMessages = '', lastQueries = '', replyMessageId = '';
const refreshing = new Set<string>();
let turnGeneration = 0, serverGeneration = 0;
let interrupts: Promise<void> = Promise.resolve();
let pendingRecordings: Blob[] = [], sendingVoice: BrowserVoice | null = null;
let endAfterPlayback: {replyId: string; generation: number; serverGeneration: number} | null = null;
let traceEvents: unknown[] = [], traceSeq = 0, poll: ReturnType<typeof setInterval> | undefined;
let turnAbort: AbortController | null = null;
const launch = new URLSearchParams(window.location.search);
let preparedRun = launch.get('run') ?? undefined;
const isClosed = () => state?.session.state !== 'active';
const endpoint = (suffix: string) => `/api/sessions/${session}${suffix}`;

function notice(message: string) {
  el('notice').textContent = message;
  el('notice').classList.toggle('visible', !!message);
}
function guarded(action: () => Promise<void>) {
  return async () => {
    try { notice(''); await action(); }
    catch (error) { if ((error as Error).name !== 'AbortError') notice((error as Error).message); }
  };
}
function controls() {
  el<HTMLButtonElement>('start').disabled = starting || !!transport;
  el('scenarios').querySelectorAll<HTMLButtonElement>('button').forEach(button => button.disabled = starting);
  el<HTMLInputElement>('customer').disabled = starting || !!transport;
  el<HTMLButtonElement>('voice-stop').disabled = !transport && (!session || isClosed());
  el<HTMLButtonElement>('mute').disabled = !transport?.listening || isClosed();
  el('mute').textContent = transport?.muted ? '取消静音' : '静音';
  el<HTMLButtonElement>('replay').disabled = !transport || !replyMessageId || processing || transport.recording;
  el('voice-orb').classList.toggle('active', !!transport?.recording || !!transport?.isPlaying);
}
function voiceStatus(text: string) { el('voice-status').textContent = text; controls(); }
function listeningStatus() {
  voiceStatus(isClosed() || !transport ? '通话已结束' : transport.muted ? '麦克风已静音' : '正在听，可以直接说话');
}
function closeLocalCall() {
  ++turnGeneration;
  turnAbort?.abort(); turnAbort = null; processing = false; starting = false;
  pendingRecordings = []; sendingVoice = null;
  endAfterPlayback = null;
  transport?.close(); transport = null;
  voiceStatus('通话已结束');
}
function telemetry(event: string) {
  if (session) void request(endpoint('/telemetry'), {event, client_ms: performance.now(), data: {message_id: replyMessageId, pipeline: 'asr-agent-tts'}}).catch(() => {});
}
function refreshInBackground() {
  const sid = session;
  void refresh().catch(error => { if (sid && sid === session) notice((error as Error).message); });
}

function scenarioList() {
  el('scenarios').innerHTML = scenarios.map(s => `<button class="scenario ${selected === s.id ? 'active' : ''}" data-scenario="${escape(s.id)}">${escape(s.title)}<small>${escape(s.industry)}</small></button>`).join('');
  el('scenarios').querySelectorAll<HTMLButtonElement>('[data-scenario]').forEach(button => button.onclick = guarded(async () => {
    await endSession('scenario_switch');
    selected = button.dataset.scenario!;
    session = ''; replyMessageId = ''; lastMessages = ''; traceEvents = []; traceSeq = 0;
    scenarioList(); render(null);
    voiceStatus('点击开始通话，允许使用麦克风');
  }));
  el('title').textContent = scenarios.find(s => s.id === selected)?.title ?? '';
}
function render(value: SessionState | null) {
  state = value;
  if (value && isClosed() && transport) closeLocalCall();
  const messages = value?.messages?.filter(m => m.origin !== 'host_control') ?? [];
  const fingerprint = JSON.stringify(messages);
  if (fingerprint !== lastMessages) {
    lastMessages = fingerprint;
    el('messages').innerHTML = messages.map(m => {
      const user = m.origin !== 'agent';
      return `<div class="message ${user ? 'user' : ''}"><span class="avatar">${user ? '你' : '✳'}</span><div><div class="bubble">${escape(m.text)}</div><div class="message-note">${user ? '客户' : 'Agent'}</div></div></div>`;
    }).join('') || '<div class="empty"><div class="empty-icon">✳</div><h2>从一个真实问题开始</h2><p>开始通话，了解业务并解答下载、注册和使用中的问题。</p></div>';
    el('messages').scrollTop = el('messages').scrollHeight;
  }
  const profileStatus: Record<string, string> = {collecting: '已载入历史画像，本次内容将在通话结束后整理。', pending: '通话已结束，正在准备更新画像。', running: '正在整理本次通话中的客户资料…', retrying: '画像更新暂未完成，稍后自动重试。', completed: '已完成本次通话的画像整理。', skipped: '本次没有客户发言，画像未变更。', failed: '本次画像更新失败，历史画像仍保留。', cancelled: '本次会话已清理，画像更新已取消。'};
  el('profile-status').textContent = profileStatus[value?.profile_update?.status ?? ''] ?? '通话前自动载入，通话结束后更新。';
  const fieldStatus: Record<string, string> = {expired: '已过期，待确认', unknown: '尚不明确', declined: '客户拒答', not_asked: '尚未询问'};
  el('memory').innerHTML = value?.memory?.length ? value.memory.map(m => `<div class="fact-row"><span>${escape(value.field_definitions?.find(f => f.id === m.field)?.description ?? m.field)}</span><span>${escape(m.status === 'known' ? m.value : fieldStatus[m.status] ?? m.status)}${m.version !== undefined ? `<br><span class="mini">版本 ${m.version}</span>` : ''}</span></div>`).join('') : '<div class="muted">尚无客户画像。</div>';
  const queries = renderToolCalls(traceEvents);
  if (queries !== lastQueries) { el('queries').innerHTML = queries; lastQueries = queries; }
  el('evidence').textContent = JSON.stringify({session: value?.session, audit: value?.audit ?? [], trace: publicTraceEvents(traceEvents)}, null, 2);
  el('session-label').textContent = session ? `· ${session.slice(-8)}` : '';
  el('connection').textContent = value ? `语音 · ${value.session.state}` : '业务工作空间';
  controls();
}
async function refresh() {
  if (!session || refreshing.has(session)) return;
  const sid = session;
  refreshing.add(sid);
  try {
    const [value, trace] = await Promise.all([
      request<SessionState>(`/api/sessions/${sid}/state`),
      request<Trace>(`/api/sessions/${sid}/events?after=${traceSeq}`),
    ]);
    if (sid !== session) return;
    traceEvents.push(...trace.events); traceSeq = trace.last_seq;
    render(value);
    if (value.session.state !== 'active' && !['pending', 'running', 'retrying'].includes(value.profile_update?.status ?? '')) clearInterval(poll);
  } finally { refreshing.delete(sid); }
}
/** 先停止本地声音，再串行通知服务端取消旧轮；新录音必须等待这道屏障。 */
function interruptForSpeech(voice: BrowserVoice) {
  if (voice !== transport || !session || isClosed()) return;
  const interruptedMessage = voice.isPlaying ? replyMessageId : '';
  const generation = ++turnGeneration, sid = session;
  endAfterPlayback = null;
  turnAbort?.abort(); turnAbort = null; processing = false;
  voice.stopPlayback();
  voiceStatus('正在听，说完停顿即可发送');
  interrupts = interrupts.catch(() => {}).then(async () => {
    if (sid !== session || voice !== transport) return;
    const result = await request<{generation: number}>(`/api/sessions/${sid}/voice/interrupt`, interruptedMessage ? {message_id: interruptedMessage} : {});
    if (sid === session && voice === transport) serverGeneration = Math.max(serverGeneration, result.generation);
  });
  // 保留失败状态给新录音读取，同时避免等待用户说完期间产生未处理的 Promise 拒绝。
  void interrupts.catch(error => {
    if (generation === turnGeneration && voice === transport) notice(`取消上一轮失败：${(error as Error).message}。请稍后重新说话。`);
  });
}

async function playReply(id: string, generation: number, signal: AbortSignal) {
  replyMessageId = id;
  transport?.clearReplay();
  voiceStatus('正在准备语音，仍可直接说话');
  const voice = transport, sid = session;
  const audio = await response(endpoint(`/voice/audio/${encodeURIComponent(id)}?stream=true&generation=${serverGeneration}`), undefined, signal);
  if (generation === turnGeneration && sid === session && voice === transport && !signal.aborted) await voice?.playPcm(audio, signal);
  else void audio.body?.cancel();
}

/** 只排队尚未上传的分段；旧回复可以丢弃，已说出的用户输入不能因插话而丢弃。 */
async function sendRecordings(voice: BrowserVoice) {
  if (sendingVoice === voice) return;
  sendingVoice = voice;
  try {
    while (voice === transport && pendingRecordings.length) {
      const barrier = interrupts;
      try { await barrier; }
      catch (error) {
        if (voice === transport) notice(`暂时无法提交语音：${(error as Error).message}。录音已保留，请稍后继续说话。`);
        return;
      }
      if (barrier !== interrupts) continue;
      if (voice !== transport || isClosed() || voice.recording) return;
      const blob = pendingRecordings.shift()!;
      // 等待取消期间积累的前几段只转写入上下文，最后一段才生成回复；每段只上传一次。
      await runTurn('/voice/turn', blob, turnGeneration, pendingRecordings.length > 0);
    }
  } finally { if (sendingVoice === voice) sendingVoice = null; }
}

async function runTurn(path: string, body: unknown, generation = turnGeneration, inputOnly = false) {
  const sid = session, voice = transport;
  let controller: AbortController | null = null;
  try {
    if (generation !== turnGeneration || sid !== session || voice !== transport) return;
    endAfterPlayback = null;
    controller = new AbortController(); turnAbort = controller; processing = true;
    notice('');
    voiceStatus(path === '/voice/turn' ? '正在转写并处理，仍可直接说话' : 'Agent 正在接通，您可以直接说话');
    const result = await request<Turn>(`/api/sessions/${sid}${path}?generation=${serverGeneration}${inputOnly ? '&input_only=true' : ''}`, body, controller.signal);
    if (generation !== turnGeneration || sid !== session || controller.signal.aborted) return;
    // 必须先登记再开始播放，极短语音的完成回调可能早于 playPcm 返回。
    endAfterPlayback = result.end_after_playback && result.reply_message_id
      ? {replyId: result.reply_message_id, generation, serverGeneration} : null;
    // 页面消息和工具轨迹不阻塞语音首包。
    refreshInBackground();
    if (result.reply_message_id) await playReply(result.reply_message_id, generation, controller.signal);
  } catch (error) {
    if (generation === turnGeneration && voice === transport && !controller?.signal.aborted) {
      voice?.stopPlayback();
      notice((error as Error).name === 'TimeoutError' ? '本次处理超时，可以直接说话重试。' : `${(error as Error).message}；可以直接说话重试。`);
    }
  } finally {
    if (generation === turnGeneration && voice === transport) {
      if (turnAbort === controller) turnAbort = null;
      processing = false;
      if (!voice?.recording && !voice?.isPlaying) listeningStatus();
      controls(); refreshInBackground();
    }
  }
}

async function newSession() {
  if (transport || starting) return;
  const customer = el<HTMLInputElement>('customer').value.trim();
  if (!customer) throw new Error('请输入客户标识');
  const generation = ++turnGeneration;
  starting = true; processing = false; replyMessageId = ''; interrupts = Promise.resolve();
  pendingRecordings = []; sendingVoice = null;
  endAfterPlayback = null;
  clearInterval(poll);
  session = ''; state = null;
  const voice = new BrowserVoice({
    onSpeechStart: () => interruptForSpeech(voice),
    onRecorded: blob => {
      if (voice === transport && !isClosed()) {
        pendingRecordings.push(blob);
        void sendRecordings(voice);
      }
    },
    onPlayback: event => {
      if (voice !== transport) return;
      telemetry(`playback_${event}`);
      if (event === 'started') voiceStatus(voice.muted ? '正在回复；麦克风已静音' : '正在回复，随时可以直接说话打断');
      if (event === 'completed') {
        const ending = endAfterPlayback;
        if (ending && ending.generation === turnGeneration && ending.replyId === replyMessageId) {
          const closing = endSession('agent_completed', ending.serverGeneration), generation = turnGeneration;
          void closing.catch(error => { if (generation === turnGeneration) notice((error as Error).message); });
        } else if (!voice.recording) listeningStatus();
      }
      controls();
    },
    onError: error => {
      if (voice !== transport) return;
      const closing = endSession('microphone_error'), generation = turnGeneration;
      void closing.catch(() => {}).finally(() => { if (generation === turnGeneration) notice(error.message); });
    },
  });
  transport = voice;
  voiceStatus('正在开启麦克风…');
  let created = false;
  try {
    // open 在首次 await 前创建并启用 AudioContext，保留按钮点击的自动播放授权。
    await voice.open();
    if (transport !== voice) return;
    const matchesPrepared = preparedRun && selected === launch.get('scenario') && customer === launch.get('customer');
    const data = await request<{session_id: string; token: string; generation: number}>('/api/sessions', {scenario_id: selected, customer_id: customer, ...(matchesPrepared ? {run_id: preparedRun} : {})});
    if (transport !== voice) {
      await fetch(`/api/sessions/${data.session_id}/stop`, {method: 'POST', headers: {'Content-Type': 'application/json', Authorization: `Bearer ${data.token}`}, body: JSON.stringify({reason: 'closed_during_start'})});
      return;
    }
    created = true; preparedRun = undefined; session = data.session_id; setToken(data.token); sessionToken = data.token;
    serverGeneration = data.generation;
    state = {session: {session_id: session, state: 'active', reason: null}};
    lastMessages = ''; traceEvents = []; traceSeq = 0; starting = false;
    // 会话可用后才开放语音分段，避免授权或创建会话期间发送无归属的录音。
    voice.listen(); controls(); refreshInBackground();
    poll = setInterval(refreshInBackground, 800);
    await runTurn('/voice/start', {}, generation);
  } catch (error) {
    if (transport === voice && generation === turnGeneration) {
      if (!created) { voice.close(); transport = null; }
      voiceStatus('无法开始通话，请重试');
      throw error;
    }
  } finally {
    if (transport === voice || !transport && generation === turnGeneration) { starting = false; controls(); }
  }
}

async function endSession(reason: string, generation?: number) {
  closeLocalCall();
  clearInterval(poll);
  if (session) {
    const sid = session;
    await request(`/api/sessions/${sid}/stop`, {reason, ...(generation === undefined ? {} : {generation})});
    if (sid === session) {
      refreshInBackground();
      if (reason === 'voice_stop_button' || reason === 'agent_completed') poll = setInterval(refreshInBackground, 1000);
    }
  }
}
el('start').onclick = guarded(newSession);
el('voice-stop').onclick = guarded(() => endSession('voice_stop_button'));
el('mute').onclick = () => {
  if (!transport) return;
  transport.setMuted(!transport.muted);
  if (transport.muted && pendingRecordings.length) void sendRecordings(transport);
  if (!transport.isPlaying) listeningStatus();
  else voiceStatus(transport.muted ? '正在回复；麦克风已静音' : '正在回复，随时可以直接说话打断');
};
el('replay').onclick = async () => {
  const voice = transport;
  if (!voice || processing || voice.recording || !replyMessageId) return;
  if (voice.hasReplay) { voice.replay(); return; }
  // 合成失败时只重试这条回复的语音，不重复提交客户发言或执行 Agent 工具。
  const generation = turnGeneration, controller = new AbortController();
  turnAbort = controller; processing = true; controls(); notice('');
  try { await playReply(replyMessageId, generation, controller.signal); }
  catch (error) { if (generation === turnGeneration && voice === transport && !controller.signal.aborted) notice((error as Error).message); }
  finally {
    if (generation === turnGeneration && voice === transport) {
      turnAbort = null; processing = false;
      if (!voice.isPlaying && !voice.recording) listeningStatus();
      controls();
    }
  }
};
window.addEventListener('pagehide', () => {
  closeLocalCall();
  if (session) void fetch(endpoint('/stop'), {method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', Authorization: `Bearer ${sessionToken}`}, body: JSON.stringify({reason: 'browser_closed'})}).catch(() => {});
});
installAcceptance(el('acceptance'), () => ({session, scenario: selected}), data => request(endpoint('/telemetry'), {event: 'human_voice_review', client_ms: performance.now(), data}));
void guarded(async () => {
  const data = await request<{scenarios: Scenario[]; default_scenario: string}>('/api/scenarios');
  scenarios = data.scenarios;
  selected = scenarios.find(s => s.id === launch.get('scenario'))?.id ?? data.default_scenario;
  if (launch.get('customer')) el<HTMLInputElement>('customer').value = launch.get('customer')!;
  scenarioList(); controls();
  if (!scenarios.length) notice('没有可用场景，请先运行 growth validate。');
})();
