// Sonos Sync: hold each <video>'s picture back by the Sonos delay so it lines up
// with the sound coming out of the Sonos.
//
// The audio can't be made earlier, so the picture gets later. Every decoded frame
// is copied (createImageBitmap, which stays on the GPU) into a short queue, and an
// overlay canvas laid exactly over the video shows each frame `delay` ms after
// Chrome presented it. With ~150 ms of delay that is ~9 frames, so the cost is small.
// The real <video> keeps playing underneath (not hidden, so Chrome keeps decoding
// and presenting it), and the site's own controls stay on top and clickable.
//
// Cost when not needed is ~zero: no frame loop runs unless we're casting and some
// video is actually audible (muted previews / autoplay feeds send nothing to the
// Sonos, so they're left alone), and the tray app is only asked for the delay on
// pages that have a video at all.
//
// Stays out of the way (plain video) when not casting, for DRM video (Netflix etc.
// can't be copied), and when the <video> element itself is fullscreen.
(() => {
  if (window.__sonosSync) return;
  window.__sonosSync = true;

  let delay = 0;            // ms, from the tray app via background.js
  const players = new Map(); // <video> -> Delayer
  let loopRunning = false;
  let lastReport = "";

  // ---- delay from the tray app ------------------------------------------
  function poll() {
    if (!players.size) return; // no video on this page: don't bother anyone
    try {
      chrome.runtime.sendMessage({ type: "sync" }, (r) => {
        if (chrome.runtime.lastError || !r) return;
        const was = delay;
        delay = r.delay;
        if (delay > 0 && !was) players.forEach((p) => p.arm());
        startLoop();
      });
    } catch (e) {
      delay = 0; // extension was reloaded; this page's script is orphaned
    }
  }
  setInterval(poll, 1500);

  // ---- badge: tell background.js what this frame is doing ---------------
  function report() {
    let syncing = false, drm = false;
    for (const p of players.values()) {
      if (p.shown) syncing = true;
      if (p.v.mediaKeys && p.audible()) drm = true;
    }
    const key = `${syncing}|${drm}|${syncing ? delay : 0}`;
    if (key === lastReport) return;
    lastReport = key;
    try {
      chrome.runtime.sendMessage({ type: "report", syncing, drm, delay });
    } catch (e) { /* orphaned */ }
  }
  setInterval(report, 1000);

  // ---- one frame loop for all videos, only while needed -----------------
  function startLoop() {
    if (loopRunning || delay <= 0) return;
    loopRunning = true;
    document.documentElement.dataset.sonosSyncLoop = "on"; // for tests / debugging
    requestAnimationFrame(tick);
  }

  function tick(now) {
    let busy = false;
    for (const p of players.values()) busy = p.draw(now) || busy;
    if (busy) {
      requestAnimationFrame(tick);
    } else {
      loopRunning = false; // all paused / inactive: stop until a video plays again
      document.documentElement.dataset.sonosSyncLoop = "off";
    }
  }

  // Self-healing: re-arm any video that should be synced but isn't getting frames
  // (no event to hook after some seeks / source swaps). And while the loop is
  // stopped (everything paused), keep a paused overlay in place through resizes /
  // fullscreen, and drop it if syncing turns off.
  setInterval(() => {
    const now = performance.now();
    for (const p of players.values()) {
      p.arm();
      if (!loopRunning && p.shown) {
        if (p.active()) p.layout(now); else p.reset();
      }
    }
    if (players.size) startLoop();
  }, 500);

  class Delayer {
    constructor(video) {
      this.v = video;
      this.q = [];         // {at, mediaTime, bmp}: frames waiting for their delayed time
      this.shown = null;   // frame currently painted on the overlay
      this.canvas = null;
      this.broken = false; // copying frames failed (DRM / unsupported): leave it alone
      this.armed = false;
      this.layoutAt = 0;
      this.target = null;   // copy size {w, h} when the video is shown smaller than its source
      this.interval = 16.7; // display refresh interval (ms), measured
      this.lastRaf = 0;
      this.onFrame = this.onFrame.bind(this);
      for (const ev of ["play", "volumechange", "loadeddata", "seeked", "canplay"]) {
        video.addEventListener(ev, () => { this.arm(); startLoop(); });
      }
      // Seeking: frames from before the jump are stale, don't play them out.
      video.addEventListener("seeking", () => this.flush());
      // New source (next video, ad, quality switch on some sites): start clean.
      video.addEventListener("emptied", () => { this.broken = false; this.flush(); });
      this.arm();
    }

    flush() {
      for (const f of this.q) f.bmp.close();
      this.q = [];
    }

    audible() {
      return !this.v.muted && this.v.volume > 0;
    }

    active() {
      const v = this.v;
      return delay > 0 && !this.broken && v.isConnected && !v.mediaKeys && this.audible() &&
        v.videoWidth > 0 && v.offsetWidth > 0 &&
        document.fullscreenElement !== v && document.pictureInPictureElement !== v;
    }

    arm() { // per-frame callbacks only while this video needs them
      if (!this.active()) return;
      // Watchdog: playing but no frame for a while (callback lost across a source
      // swap / seek): ask again instead of freezing on the last frame.
      if (this.armed && (this.v.paused || this.v.readyState < 3 || performance.now() - this.armedAt < 1000)) return;
      if (this.armed) this.v.cancelVideoFrameCallback(this.cb);
      this.armed = true;
      this.armedAt = performance.now();
      this.cb = this.v.requestVideoFrameCallback(this.onFrame);
    }

    onFrame(now, meta) {
      this.armed = false;
      if (!this.v.isConnected) return this.destroy();
      if (!this.active()) return;
      this.arm();
      const at = meta.expectedDisplayTime;
      const mediaTime = meta.mediaTime;
      // Copy at the size it's drawn, not the source size: a 4K frame shown in a
      // 1280x720 player is 33 MB of GPU memory vs ~4 MB, times ~9 queued frames.
      const opts = this.target ? { resizeWidth: this.target.w, resizeHeight: this.target.h, resizeQuality: "medium" } : undefined;
      createImageBitmap(this.v, opts).then((bmp) => {
        this.q.push({ at, mediaTime, bmp });
        while (this.q.length > 240) this.q.shift().bmp.close(); // safety cap (~4 s at 60 fps)
      }, (e) => {
        // Only a protected (DRM / cross-origin) video is a lost cause. Mid-seek into
        // unbuffered video the copy fails too (no current frame yet): just skip it.
        if (e.name === "SecurityError") this.broken = true;
      });
    }

    // returns true while it still has frames to show
    draw(now) {
      if (!this.v.isConnected) { this.destroy(); return false; }
      this.v.dataset.sonosSyncDelay = String(delay);
      if (!this.active()) { this.reset(); return false; }

      // What we paint in this rAF reaches the screen at the next refresh, ~1 interval
      // from `now`; pick the newest frame that is due by then.
      if (this.lastRaf && now - this.lastRaf < 100) this.interval += ((now - this.lastRaf) - this.interval) * 0.1;
      this.lastRaf = now;
      const shownAt = now + Math.min(this.interval, 50);
      let pick = -1;
      for (let i = 0; i < this.q.length && this.q[i].at + delay <= shownAt; i++) pick = i;
      if (pick >= 0) {
        for (let i = 0; i < pick; i++) this.q[i].bmp.close();
        if (this.shown) this.shown.bmp.close();
        this.shown = this.q[pick];
        this.q.splice(0, pick + 1);
        this.layout(now);
        this.paint();
        this.paints = (this.paints || 0) + 1;
        this.v.dataset.sonosSyncPaints = String(this.paints);
        this.v.dataset.sonosSyncLag = (this.v.currentTime - this.shown.mediaTime).toFixed(3);
      } else if (this.shown) {
        this.layout(now); // keep tracking resizes / player mode changes
      }
      return this.q.length > 0 || !this.v.paused;
    }

    layout(now) {
      const v = this.v;
      if (!this.canvas) {
        const c = document.createElement("canvas");
        c.setAttribute("aria-hidden", "true");
        c.style.cssText = "position:absolute;pointer-events:none;margin:0;padding:0;border:0;background:#000;";
        this.canvas = c;
        this.ctx = c.getContext("2d", { alpha: false, desynchronized: true });
      }
      const c = this.canvas;
      if (c.previousSibling !== v) v.after(c);
      if (now - this.layoutAt < 250 && c.width) return;
      this.layoutAt = now;
      const cs = getComputedStyle(v);
      const dpr = window.devicePixelRatio || 1;
      Object.assign(c.style, {
        left: v.offsetLeft + "px", top: v.offsetTop + "px",
        width: v.offsetWidth + "px", height: v.offsetHeight + "px",
        zIndex: cs.zIndex, transform: cs.transform, borderRadius: cs.borderRadius, display: "",
      });
      this.fit = cs.objectFit;
      // never more pixels than the video itself has: no point, and it costs fill rate
      const scale = Math.min(dpr, Math.max(v.videoWidth / v.offsetWidth, v.videoHeight / v.offsetHeight, 1));
      const w = Math.round(v.offsetWidth * scale), h = Math.round(v.offsetHeight * scale);
      if (c.width !== w || c.height !== h) {
        c.width = w;
        c.height = h;
        if (this.shown) this.paint();
      }
      const fs = this.fit === "cover" ? Math.max(w / v.videoWidth, h / v.videoHeight)
                                      : Math.min(w / v.videoWidth, h / v.videoHeight);
      this.target = fs < 0.95 ? { w: Math.round(v.videoWidth * fs), h: Math.round(v.videoHeight * fs) } : null;
    }

    paint() {
      const { ctx, canvas: c } = this;
      const b = this.shown.bmp;
      let dw = c.width, dh = c.height;
      if (this.fit !== "fill") {
        const s = this.fit === "cover" ? Math.max(c.width / b.width, c.height / b.height)
                                       : Math.min(c.width / b.width, c.height / b.height);
        dw = b.width * s;
        dh = b.height * s;
      }
      if (dw < c.width || dh < c.height) {
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, c.width, c.height);
      }
      ctx.drawImage(b, (c.width - dw) / 2, (c.height - dh) / 2, dw, dh);
    }

    reset() {
      this.flush();
      if (this.shown) this.shown.bmp.close();
      this.shown = null;
      if (this.canvas) this.canvas.style.display = "none";
      delete this.v.dataset.sonosSyncLag;
    }

    destroy() {
      this.reset();
      if (this.canvas) this.canvas.remove();
      players.delete(this.v);
    }
  }

  function scan() {
    const fresh = !players.size;
    for (const v of document.querySelectorAll("video")) {
      if (!players.has(v)) players.set(v, new Delayer(v));
    }
    if (fresh && players.size) poll(); // first video on the page: get the delay right away
  }
  let pending = false; // pages like YouTube mutate constantly: scan at most every 300 ms
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; scan(); }, 300);
  }).observe(document.documentElement, { childList: true, subtree: true });
  scan();
})();
