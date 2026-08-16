/*
 * The 2D overlay drawn on top of the brain: reticle rings, a radial
 * amplitude ring, sweep arcs, corner brackets and scan lines.
 *
 * Kept on its own canvas so it can run at full resolution with normal
 * alpha blending while the WebGL layer underneath runs additive.
 */

const TAU = Math.PI * 2;

const PALETTE = {
  idle:      { ring: "#2fa8d8", accent: "#7fe6ff", dim: "#12455f" },
  listening: { ring: "#38e1ff", accent: "#c8f7ff", dim: "#125d75" },
  thinking:  { ring: "#8b7cff", accent: "#eaf1ff", dim: "#2f2a72" },
  speaking:  { ring: "#39e1ff", accent: "#ffffff", dim: "#12586d" },
  tool:      { ring: "#ffab2e", accent: "#fff2cf", dim: "#6b4410" },
  dreaming:  { ring: "#9b7cf6", accent: "#d9c4ff", dim: "#2c2059" },
  alert:     { ring: "#ff3e6b", accent: "#ffd9e2", dim: "#5e1226" },
};

export class Hud {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.state = "idle";
    this.intensity = 0.2;
    this.level = 0;
    this.history = new Array(128).fill(0);
    this.historyIndex = 0;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.rings = [
      { r: 0.30, speed: 0.22, arc: 0.62, width: 1.4, gaps: 3 },
      { r: 0.355, speed: -0.14, arc: 0.34, width: 2.2, gaps: 2 },
      { r: 0.415, speed: 0.09, arc: 0.85, width: 1.0, gaps: 5 },
      { r: 0.46, speed: -0.05, arc: 0.20, width: 3.0, gaps: 1 },
    ];
    window.addEventListener("resize", () => this.resize());
    this.resize();
  }

  resize() {
    const { canvas } = this;
    canvas.width = Math.floor(canvas.clientWidth * this.dpr);
    canvas.height = Math.floor(canvas.clientHeight * this.dpr);
  }

  setState(state, intensity = 0.5) {
    this.state = PALETTE[state] ? state : "idle";
    this.intensity = intensity;
  }

  setLevel(level) {
    this.level = level;
    this.history[this.historyIndex] = level;
    this.historyIndex = (this.historyIndex + 1) % this.history.length;
  }

  render(nowMs) {
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const t = nowMs / 1000;
    const cx = w / 2;
    const cy = h / 2;
    const unit = Math.min(w, h);
    const palette = PALETTE[this.state] || PALETTE.idle;

    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = "round";

    // --- rotating segmented rings
    for (const ring of this.rings) {
      const radius = unit * ring.r;
      ctx.strokeStyle = palette.ring;
      ctx.globalAlpha = 0.18 + 0.35 * this.intensity;
      ctx.lineWidth = ring.width * this.dpr;
      for (let g = 0; g < ring.gaps; g++) {
        const start = t * ring.speed * TAU + (g / ring.gaps) * TAU;
        ctx.beginPath();
        ctx.arc(cx, cy, radius, start, start + (ring.arc / ring.gaps) * TAU);
        ctx.stroke();
      }
    }

    // --- tick marks on the outer ring, brighter where the signal is loud
    const tickRadius = unit * 0.487;
    ctx.globalAlpha = 0.5;
    for (let i = 0; i < 96; i++) {
      const angle = (i / 96) * TAU + t * 0.02;
      const sample = this.history[(this.historyIndex + i) % this.history.length];
      const long = i % 8 === 0;
      const len = unit * (long ? 0.018 : 0.008) + sample * unit * 0.05;
      ctx.strokeStyle = sample > 0.12 ? palette.accent : palette.dim;
      ctx.globalAlpha = sample > 0.12 ? 0.85 : 0.28;
      ctx.lineWidth = (long ? 1.6 : 1.0) * this.dpr;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(angle) * tickRadius, cy + Math.sin(angle) * tickRadius);
      ctx.lineTo(
        cx + Math.cos(angle) * (tickRadius + len),
        cy + Math.sin(angle) * (tickRadius + len)
      );
      ctx.stroke();
    }

    // --- sweeping radar line
    const sweep = (t * 0.35) % TAU;
    const grad = ctx.createLinearGradient(
      cx, cy,
      cx + Math.cos(sweep) * unit * 0.5,
      cy + Math.sin(sweep) * unit * 0.5
    );
    grad.addColorStop(0, "rgba(0,0,0,0)");
    grad.addColorStop(1, palette.ring);
    ctx.globalAlpha = 0.22;
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2 * this.dpr;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(sweep) * unit * 0.47, cy + Math.sin(sweep) * unit * 0.47);
    ctx.stroke();

    // --- live amplitude ring
    ctx.globalAlpha = 0.9;
    ctx.strokeStyle = palette.accent;
    ctx.lineWidth = 1.4 * this.dpr;
    ctx.beginPath();
    const base = unit * 0.255;
    for (let i = 0; i <= 180; i++) {
      const angle = (i / 180) * TAU - Math.PI / 2;
      const sample = this.history[(this.historyIndex + i) % this.history.length];
      const r = base + sample * unit * 0.035 + Math.sin(angle * 6 + t * 2) * unit * 0.002;
      const x = cx + Math.cos(angle) * r;
      const y = cy + Math.sin(angle) * r;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.stroke();

    // --- corner brackets
    ctx.globalAlpha = 0.55;
    ctx.strokeStyle = palette.ring;
    ctx.lineWidth = 2 * this.dpr;
    const inset = unit * 0.045;
    const arm = unit * 0.05;
    const corners = [
      [inset, inset, 1, 1],
      [w - inset, inset, -1, 1],
      [inset, h - inset, 1, -1],
      [w - inset, h - inset, -1, -1],
    ];
    for (const [x, y, sx, sy] of corners) {
      ctx.beginPath();
      ctx.moveTo(x + arm * sx, y);
      ctx.lineTo(x, y);
      ctx.lineTo(x, y + arm * sy);
      ctx.stroke();
    }

    // --- scan lines: subtle, and they drift so they never look static
    ctx.globalAlpha = 0.035;
    ctx.fillStyle = palette.ring;
    const offset = (t * 26) % 4;
    for (let y = offset; y < h; y += 4) ctx.fillRect(0, y, w, 1);

    ctx.globalAlpha = 1;
  }
}
