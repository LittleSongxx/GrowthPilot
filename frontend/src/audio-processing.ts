/** 只处理音频数据，方便学员单独理解和测试自动断句。 */
export class SpeechSegmenter {
  private before: {samples: Float32Array; voiced: boolean}[] = [];
  private beforeSize = 0;
  private chunks: Float32Array[] = [];
  private size = 0;
  private silence = 0;
  speaking = false;

  constructor(
    private sampleRate: number,
    private onStart: () => void,
    private onEnd: (samples: Float32Array) => void,
  ) {}

  push(samples: Float32Array) {
    if (!samples.length) return;
    const rms = Math.sqrt(samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length);
    // 回声消除与降噪由浏览器完成；开始阈值略高于结束阈值，减少短促杂音触发。
    const voiced = rms >= (this.speaking ? 0.01 : 0.015);
    if (!this.speaking) {
      this.before.push({samples, voiced});
      this.beforeSize += samples.length;
      // 保存最近 240 毫秒，确认至少 120 毫秒的语音后仍能保留句首。
      while (this.beforeSize - this.before[0].samples.length >= this.sampleRate * 0.24) {
        this.beforeSize -= this.before.shift()!.samples.length;
      }
      const voicedSize = this.before.reduce((sum, frame) => sum + (frame.voiced ? frame.samples.length : 0), 0);
      if (voicedSize < this.sampleRate * 0.12) return;
      this.speaking = true;
      this.chunks = this.before.map(frame => frame.samples);
      this.size = this.beforeSize;
      this.before = []; this.beforeSize = 0; this.silence = 0;
      this.onStart();
      return;
    }
    this.chunks.push(samples); this.size += samples.length;
    this.silence = voiced ? 0 : this.silence + samples.length;
    // 停顿 700 毫秒自动发送；长发言按 60 秒切分，避免超过接口的音频大小限制。
    if (this.silence >= this.sampleRate * 0.7 || this.size >= this.sampleRate * 60) {
      const result = new Float32Array(this.size);
      let offset = 0;
      for (const chunk of this.chunks) { result.set(chunk, offset); offset += chunk.length; }
      this.reset();
      this.onEnd(result);
    }
  }

  reset() {
    this.before = []; this.beforeSize = 0; this.chunks = []; this.size = 0;
    this.silence = 0; this.speaking = false;
  }
}

/** 按麦克风实际采样率生成单声道 PCM WAV，继续使用已有 ASR 上传接口。 */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2), view = new DataView(buffer);
  const text = (offset: number, value: string) => [...value].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); text(8, 'WAVE');
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, i) => {
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(44 + i * 2, value * (value < 0 ? 32768 : 32767), true);
  });
  return new Blob([buffer], {type: 'audio/wav'});
}

/** HTTP 分片不一定落在采样点边界，需要把最后一个字节留给下一片。 */
export class PcmDecoder {
  private tail: number | undefined;
  push(bytes: Uint8Array): Float32Array {
    const data = new Uint8Array(bytes.length + (this.tail === undefined ? 0 : 1));
    if (this.tail !== undefined) data[0] = this.tail;
    data.set(bytes, this.tail === undefined ? 0 : 1);
    this.tail = data.length % 2 ? data[data.length - 1] : undefined;
    const view = new DataView(data.buffer), result = new Float32Array(Math.floor(data.length / 2));
    for (let i = 0; i < result.length; i++) result[i] = view.getInt16(i * 2, true) / 32768;
    return result;
  }
  finish() { if (this.tail !== undefined) throw new Error('语音数据不完整，请直接说话重试。'); }
}
