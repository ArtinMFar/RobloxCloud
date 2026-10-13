/* Plays vm/streamer.py's /video264 stream: H.264 from the VM's GPU encoder
 * (NVENC), decoded by this device's hardware video decoder through WebCodecs
 * and drawn on a canvas as soon as each frame is out.
 *
 * The stream (see H264Stream in vm/streamer.py) is a text message with the
 * decoder's configuration, then one binary message per frame:
 *   1 byte flags (1 = keyframe) | 4 bytes timestamp in ms | the frame (AVCC).
 *
 * The page uses JSMpeg's MPEG-1 stream instead when H264Player.supported()
 * says no, and switches to it when onFail is called. */
"use strict";
(() => {
  const HW = "prefer-hardware";
  // Asked before connecting, so the page can pick a stream: High profile, level
  // 4.0, which covers 720p at 60 frames a second. The stream's own codec string
  // comes in its configuration.
  const PROBE_CODEC = "avc1.640028";

  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

  class H264Player {
    /** Whether this browser has a hardware H.264 decoder for the stream. */
    static async supported(codec = PROBE_CODEC) {
      if (typeof VideoDecoder !== "function" || typeof EncodedVideoChunk !== "function") return false;
      try {
        const r = await VideoDecoder.isConfigSupported({ codec, hardwareAcceleration: HW, optimizeForLatency: true });
        return !!(r && r.supported);
      } catch {
        return false;
      }
    }

    /**
     * @param {string} url the /video264 websocket
     * @param {HTMLCanvasElement} canvas where the frames are drawn
     * @param {{onFrame?: () => void, onFail?: (why: string) => void}} opts
     */
    constructor(url, canvas, opts = {}) {
      this.url = url;
      this.canvas = canvas;
      this.opts = opts;
      // desynchronized: frames reach the screen without waiting for the page's next paint.
      this.ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
      this.config = null;
      this.decoder = null;
      this.needKey = true;
      this.frames = 0; // frames drawn
      this.fed = 0; // frames given to the decoder since the last one came out
      this.lastOut = performance.now();
      this.errors = [];
      this.dead = false;
      this.ws = null;
      this.retry = null;
      if (!this.ctx) {
        setTimeout(() => this.fail("no 2D canvas"), 0);
        return;
      }
      // A decoder that is fed and gives nothing back is no use to anyone.
      this.watchdog = setInterval(() => {
        if (this.fed >= 30 && performance.now() - this.lastOut > 5000) this.fail("the decoder gives no frames");
      }, 1000);
      this.connect();
    }

    connect() {
      if (this.dead) return;
      const ws = new WebSocket(this.url);
      ws.binaryType = "arraybuffer";
      this.ws = ws;
      ws.onmessage = (e) => {
        if (typeof e.data === "string") {
          let m = null;
          try {
            m = JSON.parse(e.data);
          } catch {}
          if (m && m.t === "config") this.configure(m);
        } else this.decode(e.data);
      };
      ws.onclose = () => {
        if (this.ws !== ws || this.dead) return;
        this.ws = null;
        this.needKey = true;
        this.retry = setTimeout(() => this.connect(), 2000);
      };
    }

    /** A new configuration: the stream (re)started or this page (re)connected. */
    configure(m) {
      this.config = {
        codec: m.codec,
        description: unb64(m.description),
        codedWidth: m.w,
        codedHeight: m.h,
        hardwareAcceleration: HW,
        optimizeForLatency: true,
      };
      this.fps = m.fps;
      this.needKey = true;
      if (!this.decoder || this.decoder.state === "closed") {
        this.decoder = new VideoDecoder({ output: (f) => this.draw(f), error: (e) => this.onError(e) });
      } else {
        this.decoder.reset();
      }
      try {
        this.decoder.configure(this.config);
      } catch (e) {
        this.fail(`cannot configure ${m.codec}: ${e.message || e}`);
      }
    }

    decode(buf) {
      const d = this.decoder;
      if (!d || d.state !== "configured" || buf.byteLength < 6) return;
      const head = new DataView(buf, 0, 5);
      const key = (head.getUint8(0) & 1) === 1;
      if (!key) {
        if (this.needKey) return;
        // A device that decodes slower than the stream: skip to the next keyframe
        // rather than show an ever older picture.
        if (d.decodeQueueSize > 30) {
          this.needKey = true;
          return;
        }
      }
      this.needKey = false;
      try {
        d.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: head.getUint32(1) * 1000, data: new Uint8Array(buf, 5) }));
        this.fed++;
      } catch (e) {
        this.onError(e);
      }
    }

    draw(frame) {
      if (this.dead) {
        frame.close();
        return;
      }
      const w = frame.displayWidth;
      const h = frame.displayHeight;
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
      this.ctx.drawImage(frame, 0, 0, w, h);
      frame.close();
      this.frames++;
      this.fed = 0;
      this.lastOut = performance.now();
      if (this.opts.onFrame) this.opts.onFrame();
    }

    onError(e) {
      if (this.dead) return;
      const why = (e && (e.message || e.name)) || String(e);
      const now = performance.now();
      this.errors = this.errors.filter((t) => now - t < 60000);
      this.errors.push(now);
      // One that never drew a frame cannot do this stream; one that did hit a
      // bad frame, so it starts again at the next keyframe, unless it keeps failing.
      if (!this.frames || this.errors.length > 3 || !this.config) {
        this.fail(why);
        return;
      }
      console.warn("H.264 decoder error, starting again at the next keyframe:", why);
      try {
        if (this.decoder && this.decoder.state !== "closed") this.decoder.close();
      } catch {}
      this.decoder = new VideoDecoder({ output: (f) => this.draw(f), error: (err) => this.onError(err) });
      this.needKey = true;
      try {
        this.decoder.configure(this.config);
      } catch (err) {
        this.fail(err.message || String(err));
      }
    }

    fail(why) {
      if (this.dead) return;
      const cb = this.opts.onFail;
      this.destroy();
      if (cb) cb(why);
    }

    destroy() {
      this.dead = true;
      clearInterval(this.watchdog);
      clearTimeout(this.retry);
      if (this.ws) {
        const ws = this.ws;
        this.ws = null;
        ws.onclose = ws.onmessage = null;
        try {
          ws.close();
        } catch {}
      }
      try {
        if (this.decoder && this.decoder.state !== "closed") this.decoder.close();
      } catch {}
      this.decoder = null;
    }
  }

  window.H264Player = H264Player;
})();
