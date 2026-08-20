/*
 * The helix that appears beside a nudge.
 *
 * Two counter-rotating strands of points with rungs between them, drawn on a
 * small canvas. It only animates while a nudge is on screen — an idle
 * requestAnimationFrame loop behind a hidden element is wasted battery, which
 * matters on a laptop where this thing runs all day.
 */

const TWO_PI = Math.PI * 2;

export class Helix {
  constructor(canvas, { points = 42, turns = 2.4 } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.points = points;
    this.turns = turns;
    this.phase = 0;
    this.speed = 0.0016;
    this.hue = 190;
    this.running = false;
    this.frame = null;
    this.resize();
  }

  resize() {
    // Draw at device resolution so the strands stay crisp on a retina panel.
    const ratio = window.devicePixelRatio || 1;
    const { width, height } = this.canvas.getBoundingClientRect();
    if (!width || !height) return;
    this.canvas.width = Math.round(width * ratio);
    this.canvas.height = Math.round(height * ratio);
    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.w = width;
    this.h = height;
  }

  setKind(kind) {
    // Colour carries the same meaning as the HUD state palette: cyan for a
    // suggestion, amber for time, red for something that actually matters.
    this.hue = { coach: 190, playtime: 38, reminder: 265, alert: 4 }[kind] ?? 190;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.resize();
    const tick = (now) => {
      if (!this.running) return;
      this.render(now);
      this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  stop() {
    this.running = false;
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = null;
  }

  render(now) {
    const { ctx, w, h } = this;
    if (!w || !h) return this.resize();

    ctx.clearRect(0, 0, w, h);
    this.phase = now * this.speed;

    const midX = w / 2;
    const amplitude = w * 0.32;
    const top = h * 0.08;
    const span = h * 0.84;

    const strandA = [];
    const strandB = [];
    for (let i = 0; i < this.points; i++) {
      const t = i / (this.points - 1);
      const angle = t * this.turns * TWO_PI + this.phase;
      const y = top + t * span;
      // sin gives the x offset, cos the depth — depth drives size and alpha,
      // which is what reads as rotation rather than a flat wave.
      strandA.push({ x: midX + Math.sin(angle) * amplitude, y, z: Math.cos(angle) });
      strandB.push({
        x: midX + Math.sin(angle + Math.PI) * amplitude,
        y,
        z: Math.cos(angle + Math.PI),
      });
    }

    // Rungs first, so the nodes sit on top of them.
    ctx.lineWidth = 1;
    for (let i = 0; i < this.points; i += 2) {
      const a = strandA[i];
      const b = strandB[i];
      const depth = (a.z + 1) / 2;
      ctx.strokeStyle = `hsla(${this.hue}, 90%, ${45 + depth * 20}%, ${0.1 + depth * 0.35})`;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    ctx.globalCompositeOperation = "lighter";
    for (const strand of [strandA, strandB]) {
      for (const point of strand) {
        const depth = (point.z + 1) / 2;
        const radius = 0.9 + depth * 2.1;
        ctx.fillStyle = `hsla(${this.hue}, 95%, ${55 + depth * 25}%, ${0.25 + depth * 0.7})`;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, TWO_PI);
        ctx.fill();
      }
    }
    ctx.globalCompositeOperation = "source-over";
  }
}
