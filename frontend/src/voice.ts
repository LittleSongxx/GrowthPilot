import {encodeWav, PcmDecoder, SpeechSegmenter} from './audio-processing';

interface VoiceEvents {
  onSpeechStart: () => void;
  onRecorded: (audio: Blob) => void;
  onPlayback: (event: 'started' | 'completed' | 'stopped') => void;
  onError: (error: Error) => void;
}

/** 麦克风持续监听，回复按 PCM 分片排队播放；两条音频路径互不等待。 */
export class BrowserVoice {
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private capture: AudioWorkletNode | null = null;
  private segmenter: SpeechSegmenter | null = null;
  private captureGeneration = 0;
  private playbackGeneration = 0;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  private sources = new Set<AudioBufferSourceNode>();
  private queueEnd = 0;
  private receiving = false;
  private replayAudio: {chunks: Float32Array[]; sampleRate: number} | null = null;
  listening = false;
  muted = false;
  isPlaying = false;

  constructor(private events: VoiceEvents) {}
  get recording() { return this.segmenter?.speaking ?? false; }
  get hasReplay() { return this.replayAudio !== null; }

  async open() {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext || !window.AudioWorkletNode) {
      throw new Error('自动通话需要 localhost 或 HTTPS，以及支持 AudioWorklet 的浏览器，建议使用 Chrome。');
    }
    const generation = this.captureGeneration;
    // 在点击“开始通话”的同一次用户操作中启用音频，后续回复不再需要点击播放。
    // 浏览器负责重采样；16 kHz 的 60 秒 WAV 小于后端 2 MB 上传限制。
    const context = new AudioContext({sampleRate: 16000});
    this.context = context;
    const resumed = context.resume();
    void resumed.catch(() => {});
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1},
      });
      if (generation !== this.captureGeneration) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      for (const track of stream.getTracks()) track.onended = () => {
        if (generation === this.captureGeneration && this.listening) {
          this.releaseMicrophone();
          this.events.onError(new Error('麦克风连接已断开，请检查设备后重新开始通话。'));
        }
      };
      await Promise.all([resumed, context.audioWorklet.addModule('/audio-capture-worklet.js')]);
      if (generation !== this.captureGeneration) return;
      this.segmenter = new SpeechSegmenter(context.sampleRate, () => {
        this.events.onSpeechStart();
      }, samples => this.events.onRecorded(encodeWav(samples, context.sampleRate)));
      this.source = context.createMediaStreamSource(stream);
      this.capture = new AudioWorkletNode(context, 'audio-capture');
      this.capture.port.onmessage = event => {
        if (generation === this.captureGeneration && this.listening && !this.muted) this.segmenter?.push(event.data);
      };
      this.capture.onprocessorerror = () => {
        this.releaseMicrophone();
        this.events.onError(new Error('麦克风处理已中断，请重新开始通话。'));
      };
      this.source.connect(this.capture);
      this.capture.connect(context.destination);
    } catch (error) { this.close(); throw error; }
  }

  listen() { if (this.stream) this.listening = true; }
  setMuted(muted: boolean) { this.muted = muted; this.segmenter?.reset(); }
  clearReplay() { this.replayAudio = null; }

  private schedule(samples: Float32Array, sampleRate: number, generation: number) {
    const context = this.context;
    if (!context || generation !== this.playbackGeneration || !samples.length) return;
    const buffer = context.createBuffer(1, samples.length, sampleRate);
    buffer.getChannelData(0).set(samples);
    const source = context.createBufferSource();
    source.buffer = buffer; source.connect(context.destination);
    this.sources.add(source);
    source.onended = () => {
      source.disconnect(); this.sources.delete(source);
      if (generation === this.playbackGeneration) this.finishPlayback();
    };
    const start = Math.max(this.queueEnd, context.currentTime + 0.02);
    source.start(start); this.queueEnd = start + samples.length / sampleRate;
    if (!this.isPlaying) { this.isPlaying = true; this.events.onPlayback('started'); }
  }

  private finishPlayback() {
    if (!this.receiving && !this.sources.size && this.isPlaying) {
      this.isPlaying = false; this.events.onPlayback('completed');
    }
  }

  async playPcm(response: Response, signal?: AbortSignal) {
    this.stopPlayback();
    this.replayAudio = null;
    const generation = this.playbackGeneration;
    if (!this.context || signal?.aborted) return;
    const sampleRate = Number(response.headers.get('X-Audio-Sample-Rate'));
    if (!response.body || !Number.isFinite(sampleRate) || sampleRate < 8000 || sampleRate > 96000) {
      throw new Error('语音响应格式无效，请直接说话重试。');
    }
    const reader = response.body.getReader(), decoder = new PcmDecoder(), chunks: Float32Array[] = [];
    this.reader = reader; this.receiving = true;
    const cancel = () => { if (generation === this.playbackGeneration) this.stopPlayback(); };
    signal?.addEventListener('abort', cancel, {once: true});
    try {
      while (generation === this.playbackGeneration && !signal?.aborted) {
        const {value, done} = await reader.read();
        if (generation !== this.playbackGeneration || signal?.aborted) return;
        if (done) break;
        const samples = decoder.push(value);
        if (samples.length) { chunks.push(samples); this.schedule(samples, sampleRate, generation); }
      }
      if (generation !== this.playbackGeneration || signal?.aborted) return;
      decoder.finish();
      if (!chunks.length) throw new Error('本次回复没有收到语音，请直接说话重试。');
      this.replayAudio = {chunks, sampleRate};
    } catch (error) {
      if (generation !== this.playbackGeneration || signal?.aborted) return;
      this.stopPlayback();
      throw error;
    } finally {
      signal?.removeEventListener('abort', cancel);
      reader.releaseLock();
      if (generation === this.playbackGeneration) { this.reader = null; this.receiving = false; this.finishPlayback(); }
    }
  }

  replay() {
    const cached = this.replayAudio;
    if (!cached) return;
    this.stopPlayback();
    for (const chunk of cached.chunks) this.schedule(chunk, cached.sampleRate, this.playbackGeneration);
  }

  stopPlayback() {
    this.playbackGeneration++;
    void this.reader?.cancel().catch(() => {}); this.reader = null;
    this.receiving = false;
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); }
    this.sources.clear(); this.queueEnd = 0;
    if (this.isPlaying) { this.isPlaying = false; this.events.onPlayback('stopped'); }
  }

  releaseMicrophone() {
    this.captureGeneration++; this.listening = false; this.segmenter?.reset();
    if (this.capture) { this.capture.port.onmessage = null; this.capture.disconnect(); }
    this.capture = null; this.source?.disconnect(); this.source = null;
    this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); }); this.stream = null;
  }

  close() {
    this.releaseMicrophone(); this.stopPlayback(); this.replayAudio = null;
    void this.context?.close().catch(() => {}); this.context = null;
  }
}
