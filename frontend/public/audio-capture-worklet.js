/** 每 20 毫秒把单声道采样交给主线程；输出保持静音，避免把麦克风声音送回扬声器。 */
class AudioCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(Math.round(sampleRate * 0.02));
    this.offset = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const value of input) {
      this.frame[this.offset++] = value;
      if (this.offset === this.frame.length) {
        this.port.postMessage(this.frame, [this.frame.buffer]);
        this.frame = new Float32Array(Math.round(sampleRate * 0.02));
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('audio-capture', AudioCaptureProcessor);
