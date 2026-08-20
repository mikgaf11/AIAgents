/*
 * Voice: speech in, speech out, and an amplitude signal for the brain.
 *
 * Recognition uses the browser's Web Speech API in continuous mode. A
 * separate microphone stream feeds an AnalyserNode purely so the
 * visualization can react to the room — recognition and metering are kept
 * independent so losing one doesn't take out the other.
 *
 * Synthesis has no audio-graph output we can tap, so speaking amplitude is
 * reconstructed from `boundary` events (which fire per word) shaped into a
 * plausible envelope. It tracks real speech timing rather than being pure
 * decoration.
 */

const SpeechRecognitionImpl =
  window.SpeechRecognition || window.webkitSpeechRecognition || null;

export class Voice {
  constructor(handlers = {}) {
    this.handlers = handlers;
    this.supported = Boolean(SpeechRecognitionImpl);
    this.listening = false;
    this.speaking = false;
    this.requireWakeWord = false;
    this.wakeWord = "jarvis";
    this.muted = false;
    this.level = 0;

    this._recognition = null;
    this._analyser = null;
    this._audioData = null;
    this._stream = null;
    this._wantsListening = false;
    this._restartTimer = null;
    this._speakQueue = [];
    this._envelope = { target: 0, value: 0, wordAt: 0 };
    this._voice = null;

    // Voice, speed and pitch are per-person taste, so they persist across
    // restarts rather than resetting to whatever the browser prefers.
    this.voiceName = localStorage.getItem("jarvis.voice.name") || "";
    this.rate = Number(localStorage.getItem("jarvis.voice.rate")) || 1.04;
    this.pitch = Number(localStorage.getItem("jarvis.voice.pitch")) || 0.92;
    this.onVoicesChanged = null;

    this._setupSynthesis();
    this._tick();
  }

  /* ------------------------------------------------------------ output -- */

  _setupSynthesis() {
    if (!("speechSynthesis" in window)) return;
    const pick = () => {
      const voices = window.speechSynthesis.getVoices();
      if (!voices.length) return;

      // An explicit choice always wins.
      if (this.voiceName) {
        const chosen = voices.find((v) => v.name === this.voiceName);
        if (chosen) {
          this._voice = chosen;
          this.onVoicesChanged?.(voices);
          return;
        }
      }

      // Otherwise prefer the natural-sounding English voices. Matching is by
      // substring because Windows names them things like "Microsoft Ryan
      // Online (Natural) - English (United Kingdom)".
      const preferred = [
        "Ryan Online", "Guy Online", "Sonia Online", "Aria Online",
        "Google UK English Male", "Daniel", "Arthur", "Oliver",
        "Google UK English Female", "Samantha", "Google US English",
      ];
      for (const fragment of preferred) {
        const match = voices.find((v) => v.name.includes(fragment));
        if (match) {
          this._voice = match;
          this.onVoicesChanged?.(voices);
          return;
        }
      }
      this._voice = voices.find((v) => v.lang.startsWith("en")) || voices[0];
      this.onVoicesChanged?.(voices);
    };
    pick();
    window.speechSynthesis.onvoiceschanged = pick;
  }

  /** Every installed voice, best-sounding and English first. */
  listVoices() {
    if (!("speechSynthesis" in window)) return [];
    const voices = window.speechSynthesis.getVoices();
    const score = (v) => {
      let points = 0;
      if (/Natural|Online|Enhanced|Premium|Siri/i.test(v.name)) points -= 4;
      if (/^Google/.test(v.name)) points -= 2;
      if (v.lang.startsWith("en")) points -= 3;
      if (v.localService) points += 1;
      return points;
    };
    return [...voices]
      .sort((a, b) => score(a) - score(b) || a.name.localeCompare(b.name))
      .map((v) => ({
        name: v.name,
        lang: v.lang,
        local: v.localService,
        selected: this._voice ? v.name === this._voice.name : false,
      }));
  }

  setVoice(name) {
    const match = window.speechSynthesis.getVoices().find((v) => v.name === name);
    if (!match) return false;
    this._voice = match;
    this.voiceName = name;
    localStorage.setItem("jarvis.voice.name", name);
    return true;
  }

  setRate(rate) {
    this.rate = Math.max(0.5, Math.min(2.0, Number(rate) || 1));
    localStorage.setItem("jarvis.voice.rate", String(this.rate));
  }

  setPitch(pitch) {
    this.pitch = Math.max(0.3, Math.min(2.0, Number(pitch) || 1));
    localStorage.setItem("jarvis.voice.pitch", String(this.pitch));
  }

  /** Speak a sample, ignoring mute — this is the user asking to hear it. */
  preview(text = "Systems nominal. All subsystems are responding normally.") {
    const wasMuted = this.muted;
    this.muted = false;
    this.stopSpeaking();
    this.speak(text);
    this.muted = wasMuted;
  }

  speak(text) {
    if (this.muted || !text || !("speechSynthesis" in window)) return;

    // Strip things that sound wrong read aloud: code fences, markdown
    // emphasis, bullet glyphs, bare URLs.
    const spoken = text
      .replace(/```[\s\S]*?```/g, " (code omitted) ")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/\*\*?([^*]+)\*\*?/g, "$1")
      .replace(/^[\s>*-]+/gm, "")
      .replace(/https?:\/\/\S+/g, "a link")
      .trim();
    if (!spoken) return;

    const utterance = new SpeechSynthesisUtterance(spoken);
    if (this._voice) utterance.voice = this._voice;
    utterance.rate = this.rate;
    utterance.pitch = this.pitch;
    utterance.volume = 1.0;

    utterance.onstart = () => {
      this.speaking = true;
      this._envelope.target = 0.7;
      this.handlers.onSpeakStart?.();
    };
    utterance.onboundary = () => {
      // Each word gives the envelope a fresh kick.
      this._envelope.wordAt = performance.now();
      this._envelope.target = 0.55 + Math.random() * 0.45;
    };
    utterance.onend = utterance.onerror = () => {
      this.speaking = false;
      this._envelope.target = 0;
      this.handlers.onSpeakEnd?.();
    };

    window.speechSynthesis.speak(utterance);
  }

  stopSpeaking() {
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    this.speaking = false;
    this._envelope.target = 0;
  }

  setMuted(muted) {
    this.muted = muted;
    if (muted) this.stopSpeaking();
  }

  /* ------------------------------------------------------------- input -- */

  async startListening() {
    if (!this.supported) {
      this.handlers.onError?.("Speech recognition is not supported in this browser.");
      return false;
    }
    this._wantsListening = true;
    await this._startMeter();

    const recognition = new SpeechRecognitionImpl();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      this.listening = true;
      this.handlers.onListenStart?.();
    };

    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const transcript = result[0].transcript.trim();
        if (result.isFinal) {
          this._handleFinal(transcript, result[0].confidence);
        } else {
          interim += transcript + " ";
        }
      }
      if (interim.trim()) {
        // Barge-in: the moment the user starts talking, stop talking over them.
        if (this.speaking) this.stopSpeaking();
        this.handlers.onInterim?.(interim.trim());
      }
    };

    recognition.onerror = (event) => {
      if (event.error === "no-speech" || event.error === "aborted") return;
      this.handlers.onError?.(`Recognition error: ${event.error}`);
      if (event.error === "not-allowed") this._wantsListening = false;
    };

    recognition.onend = () => {
      this.listening = false;
      this.handlers.onListenEnd?.();
      // Chrome ends the session periodically; restart if we still want it.
      if (this._wantsListening) {
        clearTimeout(this._restartTimer);
        this._restartTimer = setTimeout(() => {
          try { recognition.start(); } catch { /* already starting */ }
        }, 350);
      }
    };

    this._recognition = recognition;
    try {
      recognition.start();
      return true;
    } catch (err) {
      this.handlers.onError?.(String(err));
      return false;
    }
  }

  stopListening() {
    this._wantsListening = false;
    clearTimeout(this._restartTimer);
    if (this._recognition) {
      try { this._recognition.stop(); } catch { /* not running */ }
    }
    this.listening = false;
    this._stopMeter();
  }

  async toggleListening() {
    if (this._wantsListening) {
      this.stopListening();
      return false;
    }
    return this.startListening();
  }

  _handleFinal(transcript, confidence) {
    if (!transcript) return;
    let text = transcript;

    if (this.requireWakeWord) {
      const lower = text.toLowerCase();
      const index = lower.indexOf(this.wakeWord);
      if (index === -1) {
        this.handlers.onIgnored?.(text);
        return;
      }
      // Drop the wake word and any leading comma/filler after it.
      text = text.slice(index + this.wakeWord.length).replace(/^[\s,.:!?-]+/, "");
      if (!text) {
        this.handlers.onWake?.();
        return;
      }
    }

    if (this.speaking) this.stopSpeaking();
    this.handlers.onSpeech?.(text, confidence);
  }

  /* ----------------------------------------------------------- metering -- */

  async _startMeter() {
    if (this._analyser) return;
    try {
      this._stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioCtx();
      const source = ctx.createMediaStreamSource(this._stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.75;
      source.connect(analyser);
      this._audioCtx = ctx;
      this._analyser = analyser;
      this._audioData = new Uint8Array(analyser.frequencyBinCount);
    } catch (err) {
      // Metering is a nice-to-have; recognition may still work without it.
      this.handlers.onError?.(`Microphone metering unavailable: ${err.name}`);
    }
  }

  _stopMeter() {
    if (this._stream) {
      this._stream.getTracks().forEach((track) => track.stop());
      this._stream = null;
    }
    if (this._audioCtx) {
      this._audioCtx.close().catch(() => {});
      this._audioCtx = null;
    }
    this._analyser = null;
  }

  _tick() {
    const step = () => {
      // Live microphone level.
      let mic = 0;
      if (this._analyser && this._audioData) {
        this._analyser.getByteFrequencyData(this._audioData);
        let sum = 0;
        // Weight the speech band rather than the whole spectrum.
        const lo = 2, hi = Math.min(this._audioData.length, 64);
        for (let i = lo; i < hi; i++) sum += this._audioData[i];
        mic = Math.min(1, sum / ((hi - lo) * 160));
      }

      // Reconstructed speaking envelope: decay between word boundaries.
      const env = this._envelope;
      if (this.speaking) {
        const sinceWord = (performance.now() - env.wordAt) / 1000;
        const decay = Math.exp(-sinceWord * 5.0);
        const jitter = 0.5 + 0.5 * Math.sin(performance.now() / 55);
        env.target = Math.max(0.18, env.target * decay * (0.75 + 0.25 * jitter));
      }
      env.value += (env.target - env.value) * 0.25;

      this.level = Math.max(mic, this.speaking ? env.value : 0);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
}
