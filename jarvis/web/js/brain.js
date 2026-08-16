/*
 * The digital brain.
 *
 * A volumetric neuron cloud shaped like a cerebrum (two hemispheres split by
 * a longitudinal fissure, plus a cerebellum and a brainstem), wired together
 * into a synapse graph, with signal pulses travelling along the edges.
 *
 * Nothing here is decorative-only: every visual parameter is driven by the
 * cognitive state the server reports. Thinking pulls the network inward and
 * ignites deep-layer cascades; speaking pushes waves outward from the core;
 * tool use switches the palette and marches pulses in lockstep; listening
 * makes the surface breathe with the microphone.
 *
 * Written against WebGL 1.0 shaders so it runs on a WebGL2 context or an
 * old WebGL1 one without changes.
 */

const NODE_COUNT = 2600;
const MAX_EDGES = 7000;
const PULSE_COUNT = 2600;

/* ---------------------------------------------------------------- math -- */

function mulberry32(seed) {
  return function () {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function mat4Identity() {
  return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
}

function mat4Perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  const nf = 1 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

function mat4Multiply(a, b) {
  const out = new Float32Array(16);
  for (let i = 0; i < 4; i++) {
    for (let j = 0; j < 4; j++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) sum += a[k * 4 + j] * b[i * 4 + k];
      out[i * 4 + j] = sum;
    }
  }
  return out;
}

function mat4Translate(x, y, z) {
  const m = mat4Identity();
  m[12] = x; m[13] = y; m[14] = z;
  return m;
}

function mat4RotateY(rad) {
  const c = Math.cos(rad), s = Math.sin(rad);
  const m = mat4Identity();
  m[0] = c; m[2] = -s; m[8] = s; m[10] = c;
  return m;
}

function mat4RotateX(rad) {
  const c = Math.cos(rad), s = Math.sin(rad);
  const m = mat4Identity();
  m[5] = c; m[6] = s; m[9] = -s; m[10] = c;
  return m;
}

const lerp = (a, b, t) => a + (b - a) * t;
const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);

/* -------------------------------------------------------------- shaders -- */

const NODE_VS = `
precision highp float;
attribute vec3 aPos;
attribute vec3 aNormal;
attribute float aSeed;
attribute float aDepth;   // 0 = deep core, 1 = cortical surface
attribute float aRegion;  // 0 cerebrum, 1 cerebellum, 2 stem

uniform mat4 uMVP;
uniform float uTime;
uniform float uEnergy;   // overall activation 0..1
uniform float uFocus;    // inward contraction 0..1
uniform float uChaos;    // jitter 0..1
uniform float uWave;     // seconds since last outward wave
uniform float uAudio;    // live mic / voice amplitude 0..1
uniform float uScale;
uniform vec3 uDeepColor;
uniform vec3 uSurfaceColor;
uniform vec3 uHotColor;

varying vec4 vColor;
varying float vGlow;

float hash(float n) { return fract(sin(n) * 43758.5453123); }

void main() {
  vec3 p = aPos;

  // Slow idle respiration, phase-offset per neuron.
  float breathe = sin(uTime * 0.55 + aSeed * 6.283) * 0.012;

  // Focus pulls the cloud toward the core and tightens the shell —
  // this is what "concentrating" looks like.
  p *= 1.0 - uFocus * 0.13 * (0.35 + aDepth);

  // Chaotic micro-jitter during heavy reasoning.
  float j = uChaos * 0.035;
  p += vec3(
    sin(uTime * 3.1 + aSeed * 21.0),
    cos(uTime * 2.7 + aSeed * 13.0),
    sin(uTime * 3.9 + aSeed * 31.0)
  ) * j;

  // Outward shockwave on speech onset: a shell of displacement that
  // expands through the volume and fades.
  float r = length(aPos);
  float waveFront = uWave * 1.9;
  float band = exp(-pow((r - waveFront) * 5.5, 2.0));
  float waveAmp = band * exp(-uWave * 1.6) * 0.16;

  // Microphone drives a surface bulge while listening.
  float mic = uAudio * aDepth * 0.10 * (0.6 + 0.4 * sin(aSeed * 12.0 + uTime * 4.0));

  p += aNormal * (breathe + waveAmp + mic);

  gl_Position = uMVP * vec4(p, 1.0);

  // Firing: each neuron has its own rhythm, gated by global energy.
  float rhythm = hash(aSeed * 91.7) * 4.0 + 0.6;
  float fire = pow(max(0.0, sin(uTime * rhythm + aSeed * 40.0)), 22.0);
  float active = fire * (0.25 + uEnergy) + band * 0.9;

  // Depth-graded base colour, blown out toward the hot colour when firing.
  vec3 base = mix(uDeepColor, uSurfaceColor, aDepth);
  if (aRegion > 0.5) base = mix(base, uDeepColor, 0.45);
  vColor = vec4(mix(base, uHotColor, clamp(active, 0.0, 1.0)), 1.0);

  // Surface neurons read brighter than deep ones, so the cortex stays
  // legible as a shell instead of dissolving into the interior.
  float depthGain = 0.55 + 0.45 * aDepth;
  vGlow = (0.46 + 0.55 * uEnergy + active * 1.15) * depthGain;

  float size = (2.6 + 5.4 * active + 1.8 * uEnergy) * uScale;
  gl_PointSize = clamp(size / max(0.35, gl_Position.w) * 4.3, 1.6, 42.0);
}
`;

const NODE_FS = `
precision highp float;
varying vec4 vColor;
varying float vGlow;

void main() {
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d) * 2.0;
  if (r > 1.0) discard;
  // Tight core plus a wide falloff halo — reads as bloom under additive blend.
  float core = pow(1.0 - r, 2.2);
  float halo = pow(1.0 - r, 0.9) * 0.35;
  gl_FragColor = vec4(vColor.rgb * (core + halo) * vGlow, 1.0);
}
`;

const EDGE_VS = `
precision highp float;
attribute vec3 aPos;
attribute float aSeed;
attribute float aEnd;

uniform mat4 uMVP;
uniform float uTime;
uniform float uEnergy;
uniform float uFocus;
uniform vec3 uEdgeColor;
uniform vec3 uHotColor;

varying vec4 vColor;

void main() {
  vec3 p = aPos * (1.0 - uFocus * 0.13 * 0.9);
  gl_Position = uMVP * vec4(p, 1.0);

  // A charge sweeping along the fibre; only a fraction are lit at once.
  float phase = fract(uTime * (0.25 + aSeed * 0.5) + aSeed);
  float lit = smoothstep(0.0, 0.15, phase) * smoothstep(0.45, 0.2, phase);
  float a = (0.020 + 0.12 * uEnergy) + lit * 0.45 * (0.3 + uEnergy);
  vColor = vec4(mix(uEdgeColor, uHotColor, lit * 0.8) * a, a);
}
`;

const EDGE_FS = `
precision highp float;
varying vec4 vColor;
void main() { gl_FragColor = vec4(vColor.rgb, 1.0); }
`;

const PULSE_VS = `
precision highp float;
attribute vec3 aFrom;
attribute vec3 aTo;
attribute float aSeed;
attribute float aSpeed;

uniform mat4 uMVP;
uniform float uTime;
uniform float uEnergy;
uniform float uFocus;
uniform float uRate;    // global signal velocity multiplier
uniform float uScale;
uniform vec3 uHotColor;
uniform vec3 uSurfaceColor;

varying vec4 vColor;

void main() {
  float t = fract(uTime * aSpeed * uRate + aSeed);
  vec3 p = mix(aFrom, aTo, t) * (1.0 - uFocus * 0.13 * 0.9);
  gl_Position = uMVP * vec4(p, 1.0);

  // Fade in and out at the ends so signals appear to be absorbed, not cut.
  float env = sin(t * 3.14159);
  float energy = 0.3 + 0.7 * uEnergy;
  vColor = vec4(mix(uSurfaceColor, uHotColor, env) * env * energy * 1.5, 1.0);
  gl_PointSize = clamp((1.2 + 2.4 * env * energy) * uScale / max(0.35, gl_Position.w) * 3.0, 1.0, 14.0);
}
`;

const PULSE_FS = `
precision highp float;
varying vec4 vColor;
void main() {
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d) * 2.0;
  if (r > 1.0) discard;
  gl_FragColor = vec4(vColor.rgb * pow(1.0 - r, 2.0), 1.0);
}
`;

/* ------------------------------------------------------------- geometry -- */

/**
 * Signed-ish membership test for the brain volume.
 * Returns the region id (0 cerebrum, 1 cerebellum, 2 brainstem) or -1 outside.
 */
function brainRegion(x, y, z) {
  // Cerebrum: narrow across, long front-to-back, flat underneath — the
  // proportions are what make the silhouette read as a brain in profile
  // rather than as a generic sphere.
  const ex = x / 0.88;
  const ey = y / 0.76;
  const ez = z / 1.36;
  if (ex * ex + ey * ey + ez * ez <= 1.0) {
    // Flat base, angled up slightly toward the frontal lobe.
    if (y < -0.30 + 0.10 * Math.max(0, z)) return -1;
    // Longitudinal fissure, deepest on top and closing toward the base.
    const fissure = 0.085 * clamp01((y + 0.16) / 0.5);
    if (Math.abs(x) < fissure) return -1;
    return 0;
  }
  // Cerebellum: a dense lobe tucked below and behind the occipital pole.
  const cx = x / 0.58;
  const cy = (y + 0.46) / 0.30;
  const cz = (z + 0.98) / 0.42;
  if (cx * cx + cy * cy + cz * cz <= 1.0) return 1;
  // Brainstem: a tapering column dropping from the underside.
  if (y < -0.26 && y > -1.02) {
    const radius = 0.17 - 0.06 * (-0.26 - y);
    const dx = x, dz = z + 0.30;
    if (dx * dx + dz * dz <= radius * radius) return 2;
  }
  return -1;
}

/** Cortical folding — ridges and sulci pushed into the outer shell. */
function gyri(x, y, z) {
  return (
    Math.sin(x * 9.5 + z * 4.0) * 0.5 +
    Math.sin(y * 11.0 - x * 5.0) * 0.35 +
    Math.sin(z * 13.0 + y * 6.0) * 0.3
  );
}

function buildGeometry() {
  const rand = mulberry32(20260816);
  const positions = new Float32Array(NODE_COUNT * 3);
  const normals = new Float32Array(NODE_COUNT * 3);
  const seeds = new Float32Array(NODE_COUNT);
  const depths = new Float32Array(NODE_COUNT);
  const regions = new Float32Array(NODE_COUNT);

  let placed = 0;

  // A dense deep core first — the bright centre the cortex wraps around.
  const CORE = 150;
  while (placed < CORE) {
    const u = rand() * 2 - 1;
    const theta = rand() * Math.PI * 2;
    const r = 0.30 * Math.cbrt(rand());
    const s = Math.sqrt(1 - u * u);
    const x = r * s * Math.cos(theta);
    const y = r * u * 0.8;
    const z = r * s * Math.sin(theta) * 1.2;
    const len = Math.hypot(x, y, z) || 1;
    const i3 = placed * 3;
    positions[i3] = x; positions[i3 + 1] = y; positions[i3 + 2] = z;
    normals[i3] = x / len; normals[i3 + 1] = y / len; normals[i3 + 2] = z / len;
    seeds[placed] = rand();
    depths[placed] = 0.0;
    regions[placed] = 0;
    placed++;
  }

  let guard = 0;
  while (placed < NODE_COUNT && guard < NODE_COUNT * 400) {
    guard++;
    const x = (rand() * 2 - 1) * 1.0;
    const y = (rand() * 2 - 1) * 1.0;
    const z = (rand() * 2 - 1) * 1.45;
    const region = brainRegion(x, y, z);
    if (region < 0) continue;

    // Bias the population toward the cortex: sample the radial coordinate,
    // then reject interior points most of the time.
    const r = Math.sqrt((x / 0.88) ** 2 + (y / 0.76) ** 2 + (z / 1.36) ** 2);
    const shell = region === 0 ? clamp01(r) : 0.8;
    if (region === 0 && rand() > 0.08 + Math.pow(shell, 3.6) * 0.98) continue;

    const len = Math.hypot(x, y, z) || 1;
    const nx = x / len, ny = y / len, nz = z / len;

    // Displace the shell along its normal to carve gyri and sulci.
    const fold = region === 0 ? gyri(x, y, z) * 0.038 * Math.pow(shell, 2.0) : 0;
    // Push the hemispheres apart a touch so the fissure stays legible.
    const split = region === 0 ? Math.sign(x) * 0.022 * clamp01((y + 0.16) / 0.5) : 0;

    const i3 = placed * 3;
    positions[i3] = x + nx * fold + split;
    positions[i3 + 1] = y + ny * fold;
    positions[i3 + 2] = z + nz * fold;
    normals[i3] = nx;
    normals[i3 + 1] = ny;
    normals[i3 + 2] = nz;
    seeds[placed] = rand();
    depths[placed] = shell;
    regions[placed] = region;
    placed++;
  }

  // Wire the network with a spatial hash so this stays linear-ish.
  const cell = 0.16;
  const grid = new Map();
  const key = (a, b, c) => `${a},${b},${c}`;
  for (let i = 0; i < placed; i++) {
    const a = Math.floor(positions[i * 3] / cell);
    const b = Math.floor(positions[i * 3 + 1] / cell);
    const c = Math.floor(positions[i * 3 + 2] / cell);
    const k = key(a, b, c);
    if (!grid.has(k)) grid.set(k, []);
    grid.get(k).push(i);
  }

  const edges = [];
  const seen = new Set();
  const maxDist = 0.19;
  for (let i = 0; i < placed && edges.length < MAX_EDGES; i++) {
    const ax = positions[i * 3], ay = positions[i * 3 + 1], az = positions[i * 3 + 2];
    const ga = Math.floor(ax / cell), gb = Math.floor(ay / cell), gc = Math.floor(az / cell);
    const candidates = [];
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (let dz = -1; dz <= 1; dz++) {
          const bucket = grid.get(key(ga + dx, gb + dy, gc + dz));
          if (bucket) candidates.push(...bucket);
        }
      }
    }
    let linked = 0;
    for (const j of candidates) {
      if (j <= i || linked >= 3) continue;
      const dx = positions[j * 3] - ax;
      const dy = positions[j * 3 + 1] - ay;
      const dz = positions[j * 3 + 2] - az;
      const d = Math.hypot(dx, dy, dz);
      if (d > maxDist || d < 1e-4) continue;
      const pair = i * NODE_COUNT + j;
      if (seen.has(pair)) continue;
      seen.add(pair);
      edges.push(i, j);
      linked++;
    }
  }

  // A few long-range association fibres, the way real cortex has shortcuts
  // between distant regions. Kept sparse and length-limited: unconstrained
  // random pairs read as noise crossing the silhouette rather than as wiring.
  for (let n = 0, added = 0; n < 4000 && added < 70; n++) {
    const i = Math.floor(rand() * placed);
    const j = Math.floor(rand() * placed);
    if (i === j) continue;
    const d = Math.hypot(
      positions[i * 3] - positions[j * 3],
      positions[i * 3 + 1] - positions[j * 3 + 1],
      positions[i * 3 + 2] - positions[j * 3 + 2]
    );
    if (d < 0.5 || d > 1.3) continue;
    edges.push(i, j);
    added++;
  }

  return { positions, normals, seeds, depths, regions, count: placed, edges };
}

/* --------------------------------------------------------------- states -- */

const hex = (h) => [
  parseInt(h.slice(1, 3), 16) / 255,
  parseInt(h.slice(3, 5), 16) / 255,
  parseInt(h.slice(5, 7), 16) / 255,
];

const STATES = {
  idle: {
    energy: 0.30, focus: 0.0, chaos: 0.05, rate: 0.5, spin: 0.045,
    deep: hex("#0a2f47"), surface: hex("#1f8ab5"), hot: hex("#7fe6ff"), edge: hex("#0d5a7a"),
  },
  listening: {
    energy: 0.42, focus: 0.1, chaos: 0.1, rate: 0.9, spin: 0.09,
    deep: hex("#0b3a52"), surface: hex("#2fc6e8"), hot: hex("#c8f7ff"), edge: hex("#11758f"),
  },
  thinking: {
    energy: 0.95, focus: 0.85, chaos: 0.55, rate: 2.6, spin: 0.30,
    deep: hex("#241a5c"), surface: hex("#6a5cff"), hot: hex("#eaf1ff"), edge: hex("#3b3a9e"),
  },
  speaking: {
    energy: 0.72, focus: 0.18, chaos: 0.16, rate: 1.5, spin: 0.11,
    deep: hex("#0d3a4a"), surface: hex("#39e1ff"), hot: hex("#ffffff"), edge: hex("#1a7f9c"),
  },
  tool: {
    energy: 0.80, focus: 0.45, chaos: 0.25, rate: 2.0, spin: 0.20,
    deep: hex("#4a2a05"), surface: hex("#ffab2e"), hot: hex("#fff2cf"), edge: hex("#a86a12"),
  },
  dreaming: {
    energy: 0.30, focus: 0.30, chaos: 0.18, rate: 0.65, spin: 0.035,
    deep: hex("#1b1246"), surface: hex("#7b5cd6"), hot: hex("#d9c4ff"), edge: hex("#3a2a7a"),
  },
  alert: {
    energy: 1.0, focus: 0.55, chaos: 0.85, rate: 3.0, spin: 0.42,
    deep: hex("#4a0d20"), surface: hex("#ff3e6b"), hot: hex("#ffd9e2"), edge: hex("#a01c3c"),
  },
};

/* ---------------------------------------------------------------- class -- */

export class Brain {
  constructor(canvas) {
    this.canvas = canvas;
    const opts = { antialias: true, alpha: true, premultipliedAlpha: false };
    this.gl = canvas.getContext("webgl2", opts) || canvas.getContext("webgl", opts);
    if (!this.gl) throw new Error("WebGL is not available in this browser.");

    this.geometry = buildGeometry();
    this.state = "idle";
    this.current = { ...STATES.idle, deep: [...STATES.idle.deep], surface: [...STATES.idle.surface], hot: [...STATES.idle.hot], edge: [...STATES.idle.edge] };
    this.target = STATES.idle;
    this.intensity = 0.2;
    this.audio = 0;
    this.waveAge = 99;
    // Start in three-quarter profile: the long front-to-back axis is what
    // makes the silhouette read as a brain, and it's lost head-on.
    this.spin = Math.PI * 0.42;
    this.tilt = -0.16;
    this.dragging = false;
    this.userSpin = 0;
    this.userTilt = 0;
    this.velocity = { x: 0, y: 0 };
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.lastFrame = performance.now();
    this.fps = 60;

    this._initGL();
    this._initInput();
    this.resize();
  }

  /* -- setup -- */

  _compile(vsSource, fsSource) {
    const gl = this.gl;
    const make = (type, source) => {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error("Shader failed: " + gl.getShaderInfoLog(shader));
      }
      return shader;
    };
    const program = gl.createProgram();
    gl.attachShader(program, make(gl.VERTEX_SHADER, vsSource));
    gl.attachShader(program, make(gl.FRAGMENT_SHADER, fsSource));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error("Link failed: " + gl.getProgramInfoLog(program));
    }
    return program;
  }

  _buffer(data) {
    const gl = this.gl;
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return buffer;
  }

  _initGL() {
    const gl = this.gl;
    const g = this.geometry;

    this.programs = {
      node: this._compile(NODE_VS, NODE_FS),
      edge: this._compile(EDGE_VS, EDGE_FS),
      pulse: this._compile(PULSE_VS, PULSE_FS),
    };

    // --- nodes
    this.nodeBuffers = {
      pos: this._buffer(g.positions.subarray(0, g.count * 3)),
      normal: this._buffer(g.normals.subarray(0, g.count * 3)),
      seed: this._buffer(g.seeds.subarray(0, g.count)),
      depth: this._buffer(g.depths.subarray(0, g.count)),
      region: this._buffer(g.regions.subarray(0, g.count)),
    };

    // --- edges: two vertices per edge, sharing a seed
    const edgeCount = g.edges.length / 2;
    const edgePos = new Float32Array(edgeCount * 6);
    const edgeSeed = new Float32Array(edgeCount * 2);
    const edgeEnd = new Float32Array(edgeCount * 2);
    const rand = mulberry32(777);
    for (let e = 0; e < edgeCount; e++) {
      const a = g.edges[e * 2], b = g.edges[e * 2 + 1];
      const seed = rand();
      for (let k = 0; k < 3; k++) {
        edgePos[e * 6 + k] = g.positions[a * 3 + k];
        edgePos[e * 6 + 3 + k] = g.positions[b * 3 + k];
      }
      edgeSeed[e * 2] = seed;
      edgeSeed[e * 2 + 1] = seed;
      edgeEnd[e * 2] = 0;
      edgeEnd[e * 2 + 1] = 1;
    }
    this.edgeCount = edgeCount;
    this.edgeBuffers = {
      pos: this._buffer(edgePos),
      seed: this._buffer(edgeSeed),
      end: this._buffer(edgeEnd),
    };

    // --- pulses: one point riding each sampled edge
    const pulses = Math.min(PULSE_COUNT, edgeCount);
    const from = new Float32Array(pulses * 3);
    const to = new Float32Array(pulses * 3);
    const pseed = new Float32Array(pulses);
    const pspeed = new Float32Array(pulses);
    for (let p = 0; p < pulses; p++) {
      const e = Math.floor(rand() * edgeCount);
      const a = g.edges[e * 2], b = g.edges[e * 2 + 1];
      const flip = rand() > 0.5;
      const src = flip ? b : a;
      const dst = flip ? a : b;
      for (let k = 0; k < 3; k++) {
        from[p * 3 + k] = g.positions[src * 3 + k];
        to[p * 3 + k] = g.positions[dst * 3 + k];
      }
      pseed[p] = rand();
      pspeed[p] = 0.25 + rand() * 0.75;
    }
    this.pulseCount = pulses;
    this.pulseBuffers = {
      from: this._buffer(from),
      to: this._buffer(to),
      seed: this._buffer(pseed),
      speed: this._buffer(pspeed),
    };

    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE); // additive: overlapping signals bloom
  }

  _initInput() {
    const canvas = this.canvas;
    let lastX = 0, lastY = 0;

    const down = (e) => {
      this.dragging = true;
      lastX = e.clientX ?? e.touches[0].clientX;
      lastY = e.clientY ?? e.touches[0].clientY;
    };
    const move = (e) => {
      const x = e.clientX ?? (e.touches && e.touches[0].clientX);
      const y = e.clientY ?? (e.touches && e.touches[0].clientY);
      if (x == null) return;
      if (this.dragging) {
        this.velocity.x = (x - lastX) * 0.006;
        this.velocity.y = (y - lastY) * 0.005;
        this.userSpin += this.velocity.x;
        this.userTilt = Math.max(-1.1, Math.min(1.1, this.userTilt + this.velocity.y));
        lastX = x;
        lastY = y;
      }
      // Gentle parallax even when not dragging.
      const nx = x / window.innerWidth - 0.5;
      this.parallax = nx * 0.18;
    };
    const up = () => { this.dragging = false; };

    canvas.addEventListener("pointerdown", down);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    canvas.addEventListener("touchstart", down, { passive: true });
    window.addEventListener("touchmove", move, { passive: true });
    window.addEventListener("touchend", up);
    window.addEventListener("resize", () => this.resize());
    this.parallax = 0;
  }

  resize() {
    const canvas = this.canvas;
    const width = Math.floor(canvas.clientWidth * this.dpr);
    const height = Math.floor(canvas.clientHeight * this.dpr);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    this.gl.viewport(0, 0, canvas.width, canvas.height);
  }

  /* -- public control -- */

  setState(state, intensity = 0.5) {
    const target = STATES[state] || STATES.idle;
    if (state !== this.state) {
      this.state = state;
      // Speech and alerts announce themselves with an outward shockwave.
      if (state === "speaking" || state === "alert") this.waveAge = 0;
    }
    this.target = target;
    this.intensity = clamp01(intensity);
  }

  /** Live amplitude 0..1 from the microphone or the speech synthesizer. */
  setAudio(level) {
    this.audio = clamp01(level);
  }

  /** Kick a visible ripple — used for tokens, tool calls, memory writes. */
  ripple(strength = 1) {
    this.waveAge = Math.max(0, 1 - strength) * 0.2;
  }

  /* -- frame -- */

  _bindAttrib(program, name, buffer, size) {
    const gl = this.gl;
    const loc = gl.getAttribLocation(program, name);
    if (loc < 0) return;
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
  }

  render(nowMs) {
    const gl = this.gl;
    const dt = Math.min(0.1, (nowMs - this.lastFrame) / 1000);
    this.lastFrame = nowMs;
    this.fps = lerp(this.fps, 1 / Math.max(dt, 1e-3), 0.05);
    const time = nowMs / 1000;

    // Ease every visual parameter toward the target state so transitions
    // read as the network changing mode, not as a hard cut.
    const k = 1 - Math.pow(0.0015, dt);
    const t = this.target;
    const c = this.current;
    // Intensity modulates but never fully extinguishes a state — the core
    // should always look alive, just quieter.
    const boost = 0.72 + 0.6 * this.intensity;
    c.energy = lerp(c.energy, t.energy * boost, k);
    c.focus = lerp(c.focus, t.focus, k);
    c.chaos = lerp(c.chaos, t.chaos, k);
    c.rate = lerp(c.rate, t.rate, k);
    c.spin = lerp(c.spin, t.spin, k);
    for (let i = 0; i < 3; i++) {
      c.deep[i] = lerp(c.deep[i], t.deep[i], k);
      c.surface[i] = lerp(c.surface[i], t.surface[i], k);
      c.hot[i] = lerp(c.hot[i], t.hot[i], k);
      c.edge[i] = lerp(c.edge[i], t.edge[i], k);
    }

    this.waveAge += dt;
    if (!this.dragging) {
      this.userSpin += this.velocity.x;
      this.velocity.x *= 0.94;
      this.velocity.y *= 0.94;
    }
    this.spin += c.spin * dt;

    const aspect = this.canvas.width / Math.max(1, this.canvas.height);
    const proj = mat4Perspective(Math.PI / 4.2, aspect, 0.1, 60);
    const view = mat4Translate(0, 0, -3.75 + c.focus * 0.25);
    const rotY = mat4RotateY(this.spin + this.userSpin + this.parallax);
    const rotX = mat4RotateX(this.tilt + this.userTilt + Math.sin(time * 0.21) * 0.05);
    const mvp = mat4Multiply(proj, mat4Multiply(view, mat4Multiply(rotX, rotY)));

    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    const scale = this.dpr * (this.canvas.height / 900);

    // --- edges first: the substrate everything else sits on top of
    let p = this.programs.edge;
    gl.useProgram(p);
    gl.uniformMatrix4fv(gl.getUniformLocation(p, "uMVP"), false, mvp);
    gl.uniform1f(gl.getUniformLocation(p, "uTime"), time);
    gl.uniform1f(gl.getUniformLocation(p, "uEnergy"), c.energy);
    gl.uniform1f(gl.getUniformLocation(p, "uFocus"), c.focus);
    gl.uniform3fv(gl.getUniformLocation(p, "uEdgeColor"), c.edge);
    gl.uniform3fv(gl.getUniformLocation(p, "uHotColor"), c.hot);
    this._bindAttrib(p, "aPos", this.edgeBuffers.pos, 3);
    this._bindAttrib(p, "aSeed", this.edgeBuffers.seed, 1);
    this._bindAttrib(p, "aEnd", this.edgeBuffers.end, 1);
    gl.drawArrays(gl.LINES, 0, this.edgeCount * 2);

    // --- travelling signals
    p = this.programs.pulse;
    gl.useProgram(p);
    gl.uniformMatrix4fv(gl.getUniformLocation(p, "uMVP"), false, mvp);
    gl.uniform1f(gl.getUniformLocation(p, "uTime"), time);
    gl.uniform1f(gl.getUniformLocation(p, "uEnergy"), c.energy);
    gl.uniform1f(gl.getUniformLocation(p, "uFocus"), c.focus);
    gl.uniform1f(gl.getUniformLocation(p, "uRate"), c.rate);
    gl.uniform1f(gl.getUniformLocation(p, "uScale"), scale);
    gl.uniform3fv(gl.getUniformLocation(p, "uHotColor"), c.hot);
    gl.uniform3fv(gl.getUniformLocation(p, "uSurfaceColor"), c.surface);
    this._bindAttrib(p, "aFrom", this.pulseBuffers.from, 3);
    this._bindAttrib(p, "aTo", this.pulseBuffers.to, 3);
    this._bindAttrib(p, "aSeed", this.pulseBuffers.seed, 1);
    this._bindAttrib(p, "aSpeed", this.pulseBuffers.speed, 1);
    gl.drawArrays(gl.POINTS, 0, this.pulseCount);

    // --- neurons on top
    p = this.programs.node;
    gl.useProgram(p);
    gl.uniformMatrix4fv(gl.getUniformLocation(p, "uMVP"), false, mvp);
    gl.uniform1f(gl.getUniformLocation(p, "uTime"), time);
    gl.uniform1f(gl.getUniformLocation(p, "uEnergy"), c.energy);
    gl.uniform1f(gl.getUniformLocation(p, "uFocus"), c.focus);
    gl.uniform1f(gl.getUniformLocation(p, "uChaos"), c.chaos);
    gl.uniform1f(gl.getUniformLocation(p, "uWave"), this.waveAge);
    gl.uniform1f(gl.getUniformLocation(p, "uAudio"), this.audio);
    gl.uniform1f(gl.getUniformLocation(p, "uScale"), scale);
    gl.uniform3fv(gl.getUniformLocation(p, "uDeepColor"), c.deep);
    gl.uniform3fv(gl.getUniformLocation(p, "uSurfaceColor"), c.surface);
    gl.uniform3fv(gl.getUniformLocation(p, "uHotColor"), c.hot);
    this._bindAttrib(p, "aPos", this.nodeBuffers.pos, 3);
    this._bindAttrib(p, "aNormal", this.nodeBuffers.normal, 3);
    this._bindAttrib(p, "aSeed", this.nodeBuffers.seed, 1);
    this._bindAttrib(p, "aDepth", this.nodeBuffers.depth, 1);
    this._bindAttrib(p, "aRegion", this.nodeBuffers.region, 1);
    gl.drawArrays(gl.POINTS, 0, this.geometry.count);
  }

  stats() {
    return {
      neurons: this.geometry.count,
      synapses: this.edgeCount,
      signals: this.pulseCount,
      fps: Math.round(this.fps),
    };
  }
}
