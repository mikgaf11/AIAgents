/*
 * The interface: websocket transport, the panel dock, the command palette,
 * voice wiring, and the single animation loop that drives all three canvases.
 *
 * The brain owns the screen; everything here is chrome around it. Panels live
 * in one dock driven by the icon rail, so only the view you asked for renders
 * and the rest of the viewport stays on the core.
 */

import { Brain } from "./brain.js";
import { Helix } from "./helix.js";
import { Hud } from "./hud.js";
import { Voice } from "./voice.js";

const $ = (id) => document.getElementById(id);

const el = {
  brain: $("brain-canvas"),
  hud: $("hud-canvas"),
  stateLabel: $("state-label"),
  stateDot: $("state-dot"),
  model: $("stat-model"),
  latency: $("stat-latency"),
  rate: $("stat-rate"),
  tokens: $("stat-tokens"),
  fps: $("stat-fps"),

  dock: $("dock"),
  dockTitle: $("dock-title"),
  focusToggle: $("focus-toggle"),

  transcript: $("transcript"),
  input: $("composer-input"),
  send: $("composer-send"),
  mic: $("mic-button"),
  micLabel: $("mic-label"),
  speaker: $("speaker-button"),
  wake: $("wake-toggle"),
  interim: $("interim"),
  suggestions: $("suggestions"),
  banner: $("banner"),

  // today
  todayHero: $("today-hero"),
  todayTime: $("today-time"),
  todayAttention: $("today-attention"),
  todayNoticed: $("today-noticed"),

  // mind
  thoughts: $("thought-stream"),
  tools: $("tool-trace"),
  mood: $("mood-label"),
  tickNow: $("tick-now"),

  // inbox
  inboxList: $("inbox-list"),
  emailStatus: $("email-status"),
  draftList: $("draft-list"),

  // work
  workList: $("work-list"),
  workNow: $("work-now"),
  workForm: $("work-form"),
  workInput: $("work-input"),

  // life
  activityNow: $("activity-now"),
  timeBars: $("time-bars"),
  insightList: $("insight-list"),
  mineNow: $("mine-now"),

  ventureList: $("venture-list"),

  // memory
  recallForm: $("recall-form"),
  recallInput: $("recall-input"),
  recallResults: $("recall-results"),
  captureForm: $("capture-form"),
  captureInput: $("capture-input"),
  goalForm: $("goal-form"),
  goalInput: $("goal-input"),
  goals: $("goal-list"),

  // system
  backendCard: $("backend-card"),
  vitals: $("vitals"),
  memoryStream: $("memory-stream"),
  voiceSelect: $("voice-select"),
  voiceRate: $("voice-rate"),
  voiceRateValue: $("voice-rate-value"),
  voicePitch: $("voice-pitch"),
  voicePitchValue: $("voice-pitch-value"),
  voiceTest: $("voice-test"),
  voiceReset: $("voice-reset"),
  voiceHint: $("voice-hint"),

  // palette
  palette: $("palette"),
  paletteOpen: $("palette-open"),
  paletteInput: $("palette-input"),
  paletteList: $("palette-list"),

  // overlays
  briefing: $("briefing"),
  briefingTitle: $("briefing-title"),
  briefingBody: $("briefing-body"),
  briefingClose: $("briefing-close"),
  nudge: $("nudge"),
  nudgeHelix: $("nudge-helix"),
  nudgeKind: $("nudge-kind"),
  nudgeText: $("nudge-text"),
  nudgeClose: $("nudge-close"),
  nudgeOk: $("nudge-ok"),
  nudgeBad: $("nudge-bad"),
  nudgeSnooze: $("nudge-snooze"),

  badges: {
    today: $("badge-today"),
    inbox: $("badge-inbox"),
    work: $("badge-work"),
    life: $("badge-life"),
    system: $("badge-system"),
  },
};

const PRIORITY_LABELS = ["noise", "low", "normal", "high", "critical"];
const CATEGORY_HUE = {
  game: 38, media: 300, code: 190, work: 150, comms: 265, browse: 210, other: 220,
};

/* ---------------------------------------------------------------- state -- */

const brain = new Brain(el.brain);
const hud = new Hud(el.hud);
const helix = new Helix(el.nudgeHelix);

let socket = null;
let reconnectDelay = 500;
let currentState = "idle";
let liveTurn = null;       // the assistant bubble currently being streamed into
let spokenSoFar = "";      // how much of the live reply has been sent to the voice
let pendingThought = "";
let statusCache = {};
let lastNudge = "";

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`jarvis.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`jarvis.${key}`, JSON.stringify(value)); } catch {}
  },
};

const voice = new Voice({
  onSpeech: (text) => {
    el.interim.textContent = "";
    submit(text, true);
  },
  onInterim: (text) => {
    el.interim.textContent = text;
    setState("listening", 0.5);
    send({ action: "activity", t: Date.now() / 1000 });
  },
  onListenStart: () => {
    el.mic.classList.add("active");
    el.micLabel.textContent = "LISTENING";
  },
  onListenEnd: () => {
    if (!voice._wantsListening) {
      el.mic.classList.remove("active");
      el.micLabel.textContent = "MIC OFF";
    }
  },
  onWake: () => banner("Wake word heard — go ahead."),
  onError: (message) => banner(message, true),
  onSpeakStart: () => setState("speaking", 0.7),
  onSpeakEnd: () => { if (currentState === "speaking") setState("idle", 0.15); },
});

/* ------------------------------------------------------------- websocket -- */

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onopen = () => {
    reconnectDelay = 500;
    banner("Link established.");
    setState("idle", 0.15);
  };

  socket.onmessage = (event) => {
    let batch;
    try {
      batch = JSON.parse(event.data);
    } catch {
      return;
    }
    for (const item of Array.isArray(batch) ? batch : [batch]) handleEvent(item);
  };

  socket.onclose = () => {
    setState("alert", 0.6);
    banner("Link lost — reconnecting…", true);
    // Exponential backoff, capped, so a server restart reconnects promptly
    // but a dead server doesn't spin the browser.
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.8, 8000);
  };

  socket.onerror = () => socket.close();
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(payload));
  }
}

async function api(path, options) {
  try {
    const response = await fetch(path, options);
    return await response.json();
  } catch {
    return null;  // the link banner already reports outages
  }
}

const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

/* ---------------------------------------------------------- event router -- */

// Events replayed from history on connect fill the panels but must never
// re-fire anything transient: an old nudge popping up and speaking itself
// every time you open the window is maddening.
const TRANSIENT = new Set(["nudge", "briefing", "news", "proactive"]);

function handleEvent(event) {
  if (event.replay && TRANSIENT.has(event.kind)) {
    // Keep the record, drop the interruption.
    if (event.kind === "proactive") addBubble("assistant", event.text, "proactive");
    else if (event.kind === "nudge") addThought(event.text, "sys");
    return;
  }
  switch (event.kind) {
    case "hello":
    case "status":
      applyStatus(event.status);
      break;

    case "boot":
      banner(`${event.name} online — ${event.model}`
             + (event.local ? " · free, on this machine" : ""));
      break;

    case "backend_status":
      if (event.ok === false) banner(event.detail, true);
      refreshStatus();
      break;

    case "state":
      setState(event.state, event.intensity ?? 0.5, event.tool);
      break;

    case "user_message":
      addBubble("user", event.text, event.spoken ? "spoken" : "");
      startAssistantBubble();
      break;

    case "thinking_token":
      pendingThought += event.text;
      flushThought();
      brain.ripple(0.25);
      break;

    case "token":
      appendToLive(event.text);
      break;

    case "tool_start": addTool(event); break;
    case "tool_end": completeTool(event); break;
    case "tool_args": break;  // streamed fragments; the trace shows the final input
    case "tool_pause": addThought("server-side tool paused — resuming", "sys"); break;

    case "memory": addMemory(event); break;

    case "cognition":
      addThought(event.text, event.mood || "calm");
      el.mood.textContent = (event.mood || "calm").toUpperCase();
      break;

    case "email":
      addThought(`mail [${event.label}] ${event.sender}: ${event.subject}`,
                 event.priority >= 3 ? "concerned" : "reason");
      if (event.priority >= 3) brain.ripple(0.9);
      refreshInbox();
      refreshToday();
      break;

    case "email_status":
      el.emailStatus.textContent = event.detail || "";
      el.emailStatus.classList.toggle("bad", event.ok === false);
      break;

    case "work_queued":
      addThought(`queued: ${event.title}`, "focused");
      refreshWork();
      break;

    case "work_start":
      el.workNow.textContent = `▶ ${event.title}`;
      el.workNow.classList.add("busy");
      addThought(`working: ${event.title}`, "focused");
      refreshWork();
      break;

    case "work_end":
      el.workNow.textContent = "";
      el.workNow.classList.remove("busy");
      addThought(`${event.ok ? "done" : "failed"}: ${event.title}`,
                 event.ok ? "focused" : "error");
      refreshWork();
      break;

    case "briefing": showBriefing(event.text, "BRIEFING"); break;
    case "news": showBriefing(event.text, "MORNING NEWS"); break;

    case "draft":
      refreshInbox();
      banner(event.status === "sent" ? `Sent to ${event.to}`
                                     : `Draft ${event.status}`);
      break;

    case "proactive":
      finishLive();
      addBubble("assistant", event.text, "proactive");
      voice.speak(event.text);
      refreshStatus();
      break;

    case "nudge": showNudge(event); break;

    case "nudge_suppressed":
      // Not shown to the user — the rate limiter doing its job — but worth a
      // trace so "why didn't it say anything" is answerable.
      addThought(`held back a nudge: ${event.text}`, "sys");
      break;

    case "activity": renderActivityNow(event); break;

    case "insight":
      addThought(`learned — ${event.topic}: ${event.text}`, "curious");
      refreshLife();
      break;

    case "goal": refreshMemoryPanel(); break;

    case "usage":
      el.latency.textContent = `${event.latency_ms} ms`;
      el.rate.textContent = `${event.tokens_per_second} tok/s`;
      if (event.totals) el.tokens.textContent = formatTokens(event.totals);
      break;

    case "turn_end":
      finishLive(event.text);
      refreshStatus();
      break;

    case "error":
      banner(event.message, true);
      addThought(event.message, "error");
      break;
  }
}

/* ------------------------------------------------------------ the shell -- */

const VIEWS = ["today", "mind", "inbox", "work", "life", "ventures", "memory", "system"];
let activeView = store.get("view", "today");

function showView(name) {
  if (!VIEWS.includes(name)) return;
  activeView = name;
  store.set("view", name);
  for (const button of document.querySelectorAll(".rail-btn")) {
    button.classList.toggle("active", button.dataset.view === name);
  }
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("active", section.dataset.view === name);
  }
  el.dockTitle.textContent = name.toUpperCase();
  if (document.body.classList.contains("focus")) setFocus(false);
  REFRESHERS[name]?.();
}

for (const button of document.querySelectorAll(".rail-btn")) {
  button.addEventListener("click", () => showView(button.dataset.view));
}

function setFocus(on) {
  document.body.classList.toggle("focus", on);
  store.set("focus", on);
}
el.focusToggle.addEventListener("click", () =>
  setFocus(!document.body.classList.contains("focus"))
);

function setState(state, intensity = 0.5, tool = null) {
  currentState = state;
  brain.setState(state, intensity);
  hud.setState(state, intensity);
  el.stateLabel.textContent = tool ? `${state.toUpperCase()} · ${tool}`
                                   : state.toUpperCase();
  el.stateDot.dataset.state = state;
  document.body.dataset.state = state;
}

function banner(message, isError = false) {
  el.banner.textContent = message;
  el.banner.classList.toggle("error", isError);
  el.banner.classList.add("visible");
  clearTimeout(banner._timer);
  banner._timer = setTimeout(() => el.banner.classList.remove("visible"), 4200);
}

function trimChildren(container, max) {
  while (container.children.length > max) container.removeChild(container.firstChild);
}

function setBadge(name, value, hot = false) {
  const node = el.badges[name];
  if (!node) return;
  node.textContent = value ? String(value) : "";
  node.classList.toggle("hot", Boolean(hot));
}

function empty(container, message) {
  container.innerHTML = "";
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = message;
  container.appendChild(node);
}

/* --------------------------------------------------------- conversation -- */

function addBubble(role, text, extra = "") {
  const node = document.createElement("div");
  node.className = `bubble ${role} ${extra}`.trim();
  node.innerHTML = `<span class="who">${role === "user" ? "YOU" : "JARVIS"}</span>`
    + `<span class="body"></span>`;
  node.querySelector(".body").textContent = text;
  el.transcript.appendChild(node);
  trimChildren(el.transcript, 60);
  el.transcript.scrollTop = el.transcript.scrollHeight;
  el.suggestions.innerHTML = "";
  return node;
}

function startAssistantBubble() {
  finishLive();
  liveTurn = addBubble("assistant", "", "streaming");
  spokenSoFar = "";
}

function appendToLive(text) {
  if (!liveTurn) liveTurn = addBubble("assistant", "", "streaming");
  const body = liveTurn.querySelector(".body");
  body.textContent += text;
  el.transcript.scrollTop = el.transcript.scrollHeight;
  brain.ripple(0.5);

  // Speak sentence by sentence as they complete, so the voice keeps pace with
  // the stream instead of waiting for the whole reply.
  const full = body.textContent;
  const unspoken = full.slice(spokenSoFar.length);
  const boundary = unspoken.search(/[.!?…](\s|$)/);
  if (boundary !== -1) {
    const sentence = unspoken.slice(0, boundary + 1).trim();
    if (sentence.length > 1) {
      voice.speak(sentence);
      spokenSoFar = full.slice(0, spokenSoFar.length + boundary + 1);
    }
  }
}

function finishLive(finalText) {
  if (!liveTurn) return;
  const body = liveTurn.querySelector(".body");
  if (finalText && finalText.length > body.textContent.length) {
    body.textContent = finalText;
  }
  const remainder = body.textContent.slice(spokenSoFar.length).trim();
  if (remainder) voice.speak(remainder);
  liveTurn.classList.remove("streaming");
  liveTurn = null;
  spokenSoFar = "";
}

function flushThought() {
  // Thinking arrives as a token stream; emit it in sentence-ish chunks so the
  // panel reads as thoughts rather than a character firehose.
  const match = pendingThought.match(/^([\s\S]*?[.!?\n])\s/);
  if (match && match[1].trim().length > 12) {
    addThought(match[1].trim(), "reason");
    pendingThought = pendingThought.slice(match[0].length);
  } else if (pendingThought.length > 220) {
    addThought(pendingThought.slice(0, 200).trim() + "…", "reason");
    pendingThought = "";
  }
}

function addThought(text, kind = "reason") {
  if (!text) return;
  const node = document.createElement("div");
  node.className = `thought ${kind}`;
  node.textContent = text;
  el.thoughts.appendChild(node);
  trimChildren(el.thoughts, 60);
  el.thoughts.scrollTop = el.thoughts.scrollHeight;
}

function addTool(event) {
  const node = document.createElement("div");
  node.className = `tool-entry running${event.dangerous ? " dangerous" : ""}`;
  node.dataset.id = event.id;
  node.innerHTML = `<span class="tool-name"></span><span class="tool-arg"></span>`
    + `<span class="tool-time">···</span>`;
  node.querySelector(".tool-name").textContent = event.name;
  node.querySelector(".tool-arg").textContent = event.input || "";
  el.tools.appendChild(node);
  trimChildren(el.tools, 40);
  el.tools.scrollTop = el.tools.scrollHeight;
  brain.ripple(0.8);
}

function completeTool(event) {
  const node = el.tools.querySelector(`[data-id="${event.id}"]`);
  if (!node) return;
  node.classList.remove("running");
  node.classList.add(event.ok ? "ok" : "failed");
  node.querySelector(".tool-time").textContent = `${event.ms} ms`;
  node.title = event.preview || "";
}

function addMemory(event) {
  const node = document.createElement("div");
  node.className = `memory-entry op-${event.op}`;
  node.innerHTML = `<span class="op"></span><span class="detail"></span>`;
  node.querySelector(".op").textContent = event.op;
  node.querySelector(".detail").textContent =
    event.detail || (event.count != null ? `${event.count} hits` : "");
  el.memoryStream.appendChild(node);
  trimChildren(el.memoryStream, 30);
  el.memoryStream.scrollTop = el.memoryStream.scrollHeight;
}

/* --------------------------------------------------------------- today -- */

function tile(label, value, sub = "", tone = "", wide = false) {
  return `<div class="tile ${tone} ${wide ? "wide" : ""}">`
    + `<span class="tile-label">${label}</span>`
    + `<div class="tile-value">${value}</div>`
    + (sub ? `<div class="tile-sub">${sub}</div>` : "")
    + `</div>`;
}

function renderToday(data) {
  if (!data) return;
  const mail = data.mail || {};
  const work = data.work || {};
  const play = data.play || {};
  const goals = data.goals || [];
  const overPlay = play.limit && play.minutes >= play.limit;

  const focusMinutes = Object.entries(data.time || {})
    .filter(([k]) => k === "code" || k === "work")
    .reduce((sum, [, v]) => sum + v, 0);

  el.todayHero.innerHTML = [
    tile("NEEDS A REPLY", mail.unhandled ?? 0,
         mail.urgent ? `${mail.urgent} urgent` : "nothing urgent",
         mail.urgent ? "hot" : ""),
    tile("FOCUSED WORK", formatMinutes(focusMinutes), "code and work apps"),
    tile("PLAY", formatMinutes(play.minutes || 0),
         play.limit ? `limit ${formatMinutes(play.limit)}` : "no limit set",
         overPlay ? "warm" : ""),
    tile("IN PROGRESS", work.pending ?? 0,
         work.running ? String(work.running).slice(0, 40) : "queue is clear"),
  ].join("");

  renderTimeBars(el.todayTime, data.time || {}, null);

  // "Needs you" merges the things that actually want a decision.
  const attention = [];
  for (const item of mail.top || []) {
    attention.push({
      tag: PRIORITY_LABELS[item.priority] || "normal",
      tone: item.priority >= 4 ? "hot" : item.priority >= 3 ? "warm" : "",
      text: `${item.sender} — ${item.subject}`,
    });
  }
  for (const reminder of data.reminders || []) {
    attention.push({ tag: "due", tone: "warm", text: reminder.text });
  }
  for (const goal of goals.slice(0, 3)) {
    attention.push({
      tag: `${Math.round((goal.progress || 0) * 100)}%`,
      tone: "",
      text: goal.title,
    });
  }
  renderRows(el.todayAttention, attention, "nothing waiting on you");

  const noticed = [
    ...(data.insights || []).map((i) => ({ tag: "learned", tone: "", text: i.insight })),
    ...(data.observations || []).map((o) => ({ tag: "noted", tone: "", text: o.text })),
  ];
  renderRows(el.todayNoticed, noticed.slice(0, 8), "nothing noted yet");

  setBadge("today", mail.urgent || 0, mail.urgent > 0);
}

function renderRows(container, rows, emptyMessage) {
  container.innerHTML = "";
  if (!rows.length) return empty(container, emptyMessage);
  for (const row of rows.slice(0, 10)) {
    const node = document.createElement("div");
    node.className = "row-item";
    node.innerHTML = `<span class="row-tag ${row.tone}"></span><span class="row-text"></span>`;
    node.querySelector(".row-tag").textContent = row.tag;
    node.querySelector(".row-text").textContent = row.text;
    container.appendChild(node);
  }
}

const formatMinutes = (m) =>
  m >= 60 ? `${(m / 60).toFixed(1)}h` : `${Math.round(m || 0)}m`;

function renderTimeBars(container, today, playInfo) {
  container.innerHTML = "";
  const entries = Object.entries(today).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, v]) => sum + v, 0);
  if (!entries.length) return empty(container, "nothing recorded today");

  for (const [category, minutes] of entries) {
    const row = document.createElement("div");
    row.className = "time-row";
    const width = total ? Math.round((minutes / total) * 100) : 0;
    const hue = CATEGORY_HUE[category] ?? 220;
    row.innerHTML =
      `<span class="time-label"></span>`
      + `<span class="time-bar"><i style="width:${width}%;background:hsl(${hue} 90% 55% / .8)"></i></span>`
      + `<span class="time-value"></span>`;
    row.querySelector(".time-label").textContent = category;
    row.querySelector(".time-value").textContent = formatMinutes(minutes);
    container.appendChild(row);
  }

  if (playInfo && playInfo.limit) {
    const summary = document.createElement("div");
    summary.className = `play-summary${playInfo.minutes >= playInfo.limit ? " over" : ""}`;
    summary.textContent =
      `play ${Math.round(playInfo.minutes)}m of ${Math.round(playInfo.limit)}m limit`;
    container.appendChild(summary);
  }
}

/* ---------------------------------------------------------------- inbox -- */

function renderInbox(data) {
  if (!data) return;
  const messages = data.messages || [];
  const unhandled = messages.filter((m) => !m.handled);
  const urgent = unhandled.filter((m) => m.priority >= 3);
  setBadge("inbox", unhandled.length, urgent.length > 0);

  const status = data.email || {};
  if (!status.configured) {
    el.emailStatus.innerHTML = 'not connected — run <code>python3 connect.py</code>';
    el.emailStatus.classList.add("bad");
  } else if (status.error) {
    el.emailStatus.textContent = status.error;
    el.emailStatus.classList.add("bad");
  } else {
    const when = status.last_sweep
      ? new Date(status.last_sweep * 1000).toLocaleTimeString()
      : "not yet";
    el.emailStatus.textContent =
      `${status.address} · ${status.triaged} triaged · checked ${when}`;
    el.emailStatus.classList.remove("bad");
  }

  renderDrafts(data.drafts || []);

  el.inboxList.innerHTML = "";
  if (!unhandled.length) {
    return empty(el.inboxList, status.configured ? "inbox clear" : "no mail connected");
  }
  for (const message of unhandled.slice(0, 30)) {
    const node = document.createElement("div");
    node.className = `mail p${message.priority}`;
    node.innerHTML =
      `<div class="mail-head"><span class="mail-priority"></span>`
      + `<span class="mail-from"></span>`
      + `<button class="mail-done" title="Mark handled">✓</button></div>`
      + `<div class="mail-subject"></div><div class="mail-summary"></div>`;
    node.querySelector(".mail-priority").textContent =
      PRIORITY_LABELS[message.priority] || "normal";
    node.querySelector(".mail-from").textContent = message.sender;
    node.querySelector(".mail-subject").textContent = message.subject;
    node.querySelector(".mail-summary").textContent =
      message.summary + (message.action ? ` → ${message.action}` : "");
    node.querySelector(".mail-done").addEventListener("click", async () => {
      await post(`/api/email/handled/${message.id}`);
      refreshInbox();
      refreshToday();
    });
    el.inboxList.appendChild(node);
  }
}

function renderDrafts(drafts) {
  el.draftList.innerHTML = "";
  for (const draft of drafts) {
    const node = document.createElement("div");
    node.className = "draft";
    node.innerHTML =
      `<div class="draft-head">DRAFT → <span class="draft-to"></span></div>`
      + `<div class="draft-subject"></div><div class="draft-body"></div>`
      + `<div class="draft-actions"><button class="approve">APPROVE &amp; SEND</button>`
      + `<button class="discard">DISCARD</button></div>`;
    node.querySelector(".draft-to").textContent = draft.to_addr;
    node.querySelector(".draft-subject").textContent = draft.subject;
    node.querySelector(".draft-body").textContent = draft.body;

    node.querySelector(".approve").addEventListener("click", async () => {
      const result = await api(`/api/draft/${draft.id}/approve`, { method: "POST" });
      if (result && result.error) banner(result.error, true);
      refreshInbox();
    });
    node.querySelector(".discard").addEventListener("click", async () => {
      await post(`/api/draft/${draft.id}/discard`);
      refreshInbox();
    });
    el.draftList.appendChild(node);
  }
}

/* ----------------------------------------------------------------- work -- */

function renderWork(data) {
  if (!data) return;
  const tasks = data.tasks || [];
  const pending = tasks.filter((t) => t.status === "pending" || t.status === "running");
  setBadge("work", pending.length, pending.some((t) => t.status === "running"));

  const working = (data.autonomy || {}).working_on;
  if (working) {
    el.workNow.textContent = `▶ ${working}`;
    el.workNow.classList.add("busy");
  } else if (!el.workNow.classList.contains("busy")) {
    el.workNow.textContent = "";
  }

  el.workList.innerHTML = "";
  if (!tasks.length) return empty(el.workList, "nothing queued");
  for (const task of tasks.slice(0, 25)) {
    const node = document.createElement("div");
    node.className = `task ${task.status}`;
    node.innerHTML =
      `<div class="task-head"><span class="task-status"></span>`
      + `<span class="task-title"></span></div>`
      + (task.result ? `<div class="task-result"></div>` : "");
    node.querySelector(".task-status").textContent = task.status;
    node.querySelector(".task-title").textContent = task.title;
    if (task.result) node.querySelector(".task-result").textContent = task.result;
    el.workList.appendChild(node);
  }

  renderVentures(data.ventures || []);
}

function renderVentures(ventures) {
  el.ventureList.innerHTML = "";
  if (!ventures.length) {
    return empty(el.ventureList, "no opportunities tracked yet");
  }
  for (const venture of ventures.slice(0, 20)) {
    const node = document.createElement("div");
    node.className = `venture ${venture.status}`;
    node.innerHTML =
      `<div class="venture-head"><span class="venture-title"></span>`
      + `<span class="venture-conf"></span></div>`
      + `<div class="venture-meta"></div><div class="venture-thesis"></div>`
      + `<div class="venture-next"><b>next</b> <span></span></div>`;
    node.querySelector(".venture-title").textContent = venture.title;
    node.querySelector(".venture-conf").textContent =
      `${Math.round((venture.confidence || 0) * 100)}%`;
    node.querySelector(".venture-meta").textContent =
      `${venture.status} · ${venture.effort} effort · ${venture.horizon}`;
    node.querySelector(".venture-thesis").textContent = venture.thesis || "";
    node.querySelector(".venture-next span").textContent = venture.next_step || "—";
    el.ventureList.appendChild(node);
  }
}

el.workForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = el.workInput.value.trim();
  if (!title) return;
  el.workInput.value = "";
  await post("/api/work/queue", { title });
  banner("Queued.");
  refreshWork();
});

/* ----------------------------------------------------------------- life -- */

function renderActivityNow(event) {
  if (!event.app) return;
  el.activityNow.textContent = `▶ ${event.app} · ${event.category} · ${event.minutes}m`;
  el.activityNow.dataset.category = event.category;
}

function renderLife(data) {
  if (!data) return;
  const play = data.play_minutes || 0;
  const limit = data.play_limit || 0;
  setBadge("life", limit && play >= limit ? "!" : "", limit && play >= limit);

  const monitor = data.monitor || {};
  if (!monitor.available) {
    el.activityNow.textContent = monitor.note || "activity monitoring unavailable";
    el.activityNow.classList.add("bad");
  } else {
    el.activityNow.classList.remove("bad");
  }

  renderTimeBars(el.timeBars, data.today || {}, { minutes: play, limit });
  renderInsights(data.insights || []);
}

function renderInsights(insights) {
  el.insightList.innerHTML = "";
  if (!insights.length) {
    return empty(el.insightList,
                 "nothing concluded yet — it needs a few days of watching");
  }
  for (const item of insights.slice(0, 30)) {
    const node = document.createElement("div");
    node.className = "insight";
    node.innerHTML =
      `<div class="insight-head"><span class="insight-topic"></span>`
      + `<span class="insight-conf"></span>`
      + `<button class="insight-forget" title="This is wrong — forget it">×</button></div>`
      + `<div class="insight-body"></div><div class="insight-evidence"></div>`;
    node.querySelector(".insight-topic").textContent = item.topic;
    node.querySelector(".insight-conf").textContent =
      `${Math.round((item.confidence || 0) * 100)}%`;
    node.querySelector(".insight-body").textContent = item.insight;
    node.querySelector(".insight-evidence").textContent = item.evidence || "";
    node.querySelector(".insight-forget").addEventListener("click", async () => {
      await post(`/api/insight/${item.id}/forget`);
      refreshLife();
    });
    el.insightList.appendChild(node);
  }
}

el.mineNow.addEventListener("click", async () => {
  banner("Reviewing what you've been doing…");
  const result = await post("/api/learning/mine");
  banner(result && result.insights
    ? `${result.insights.length} insight${result.insights.length === 1 ? "" : "s"} updated`
    : "Nothing new concluded.");
  refreshLife();
});

el.tickNow.addEventListener("click", async () => {
  await post("/api/cognition/tick");
  banner("Reflection pass run.");
});

/* --------------------------------------------------------------- memory -- */

el.recallForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await runRecall(el.recallInput.value.trim());
});

async function runRecall(query) {
  if (!query) return;
  const data = await api(`/api/recall?q=${encodeURIComponent(query)}`);
  el.recallResults.innerHTML = "";
  const hits = (data && data.hits) || [];
  if (!hits.length) return empty(el.recallResults, `nothing recalled for “${query}”`);
  for (const hit of hits) {
    const node = document.createElement("div");
    node.className = "row-item";
    node.innerHTML = `<span class="row-tag"></span><span class="row-text"></span>`;
    node.querySelector(".row-tag").textContent = hit.kind;
    node.querySelector(".row-text").textContent = hit.text;
    node.title = `score ${hit.score}`;
    el.recallResults.appendChild(node);
  }
}

el.captureForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = el.captureInput.value.trim();
  if (!content) return;
  el.captureInput.value = "";
  await post("/api/remember", { content });
  banner("Stored.");
});

el.goalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = el.goalInput.value.trim();
  if (!title) return;
  el.goalInput.value = "";
  await post("/api/goal", { title });
  banner("Objective set.");
  refreshMemoryPanel();
});

function renderGoals(goals) {
  const open = goals.filter((g) => g.status === "open").slice(0, 10);
  el.goals.innerHTML = "";
  if (!open.length) {
    const node = document.createElement("div");
    node.className = "goal empty";
    node.textContent = "no active objectives";
    return el.goals.appendChild(node);
  }
  for (const goal of open) {
    const node = document.createElement("div");
    node.className = "goal";
    node.innerHTML =
      `<span class="goal-title"></span>`
      + `<span class="goal-bar"><i style="width:${Math.round((goal.progress || 0) * 100)}%"></i></span>`;
    node.querySelector(".goal-title").textContent = `#${goal.id} ${goal.title}`;
    el.goals.appendChild(node);
  }
}

/* --------------------------------------------------------------- system -- */

function applyStatus(status) {
  if (!status) return;
  statusCache = status;

  // Online means "a backend is configured"; reachable means it actually
  // answers. A local model that was never pulled is the difference.
  const usable = status.online && status.reachable !== false;
  el.model.textContent = !status.online ? "OFFLINE"
    : usable ? shortModel(status.model) : "UNREACHABLE";
  el.model.classList.toggle("warn", !usable);
  el.model.title = usable ? (status.model || "") : (status.backend_error || "");

  if (status.tokens) el.tokens.textContent = formatTokens(status.tokens);
  renderVitals(status);
  renderBackendCard(status);
  setBadge("system", usable ? "" : "!", true);
}

const shortModel = (name = "") =>
  name.replace(/^claude-/, "").replace(/:latest$/, "");

function renderBackendCard(status) {
  const usable = status.online && status.reachable !== false;
  const paid = status.online && !status.local;
  el.backendCard.className = `card${usable ? "" : " bad"}`;
  if (!status.online) {
    el.backendCard.innerHTML =
      `<b>No reasoning core</b><br>The interface runs, but it cannot think.`
      + `<span class="hint">free:  ollama pull llama3.1:8b`
      + `<br>or put ANTHROPIC_API_KEY in jarvis/.env</span>`;
  } else if (!usable) {
    el.backendCard.innerHTML = `<b>Unreachable</b><br>`
      + `<span class="hint"></span>`;
    el.backendCard.querySelector(".hint").textContent = status.backend_error || "";
  } else {
    el.backendCard.innerHTML =
      `<b>${status.model}</b><br>`
      + (paid ? "Anthropic API — billed per token."
              : "Running on this machine — free.")
      + `<span class="hint">switch:  python3 backend.py `
      + `${paid ? "local" : "claude"}</span>`;
  }
}

function renderVitals(status) {
  const host = status.host || {};
  const memory = status.memory || {};
  const pct = (v) => (v == null ? null : Math.max(0, Math.min(100, v)));
  const rows = [
    ["CPU", host.cpu_percent != null ? `${host.cpu_percent}%` : "—", pct(host.cpu_percent)],
    ["MEMORY", host.memory_percent != null ? `${host.memory_percent}%` : "—", pct(host.memory_percent)],
    ["DISK", host.disk_percent != null ? `${host.disk_percent}%` : "—", pct(host.disk_percent)],
    ["FACTS", memory.facts ?? 0, null],
    ["EPISODES", memory.episodes ?? 0, null],
    ["SAFE MODE", status.safe_mode ? "ON" : "OFF", null],
    ["TOOLS", (status.tools || []).length, null],
  ];
  if (host.battery_percent != null) {
    rows.splice(3, 0, ["POWER", `${host.battery_percent}%`, pct(host.battery_percent)]);
  }
  el.vitals.innerHTML = "";
  for (const [label, value, fraction] of rows) {
    const row = document.createElement("div");
    row.className = "vital";
    row.innerHTML =
      `<span class="vital-label"></span><span class="vital-value"></span>`
      + (fraction != null
          ? `<span class="vital-bar"><i style="width:${fraction}%"></i></span>` : "");
    row.querySelector(".vital-label").textContent = label;
    row.querySelector(".vital-value").textContent = value;
    el.vitals.appendChild(row);
  }
}

function formatTokens(totals) {
  const compact = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  return `${compact(totals.input || 0)}↓ ${compact(totals.output || 0)}↑`
    + (totals.cache_read ? ` ${compact(totals.cache_read)}⚡` : "");
}

/* ------------------------------------------------------- the helix nudge -- */

function showNudge(event) {
  lastNudge = event.text || "";
  el.nudgeKind.textContent = (event.title || "JARVIS").toUpperCase();
  el.nudgeText.textContent = lastNudge;
  el.nudge.dataset.kind = event.tone || "coach";
  helix.setKind(event.tone || "coach");
  el.nudge.classList.add("visible");
  helix.start();
  if (event.speak !== false) voice.speak(lastNudge);
  brain.ripple(0.8);
  addThought(lastNudge, event.tone === "playtime" ? "concerned" : "curious");

  // It disappears on its own — a nudge you have to dismiss is a chore.
  clearTimeout(showNudge._timer);
  showNudge._timer = setTimeout(hideNudge, 45000);
}

function hideNudge() {
  clearTimeout(showNudge._timer);
  el.nudge.classList.remove("visible");
  helix.stop();
}

async function rateNudge(signal) {
  hideNudge();
  await post("/api/nudge/feedback", { signal, text: lastNudge });
}

el.nudgeClose.addEventListener("click", hideNudge);
el.nudgeOk.addEventListener("click", () => rateNudge("helpful"));
el.nudgeBad.addEventListener("click", () => rateNudge("rejected"));
el.nudgeSnooze.addEventListener("click", async () => {
  hideNudge();
  await post("/api/nudge/snooze", { minutes: 60 });
  banner("Quiet for an hour.");
});

function showBriefing(text, title = "BRIEFING") {
  el.briefingTitle.textContent = title;
  el.briefingBody.textContent = text;
  el.briefing.classList.add("visible");
}
el.briefingClose.addEventListener("click", () =>
  el.briefing.classList.remove("visible")
);

/* ------------------------------------------------------ command palette -- */

const COMMANDS = [
  { group: "Go", name: "Today", hint: "1", run: () => showView("today") },
  { group: "Go", name: "Mind", hint: "2", run: () => showView("mind") },
  { group: "Go", name: "Inbox", hint: "3", run: () => showView("inbox") },
  { group: "Go", name: "Work", hint: "4", run: () => showView("work") },
  { group: "Go", name: "Life", hint: "5", run: () => showView("life") },
  { group: "Go", name: "Ventures", hint: "6", run: () => showView("ventures") },
  { group: "Go", name: "Memory", hint: "7", run: () => showView("memory") },
  { group: "Go", name: "System", hint: "8", run: () => showView("system") },

  { group: "Run", name: "Check mail now", run: async () => {
      const r = await post("/api/email/sweep");
      banner(r && r.ok ? `Triaged ${r.triaged}.` : (r && r.error) || "Mail not configured.");
      refreshInbox();
    } },
  { group: "Run", name: "Deliver the morning briefing", run: async () => {
      await post("/api/routine/briefing");
      banner("Briefing running…");
    } },
  { group: "Run", name: "Read me the news", run: async () => {
      await post("/api/routine/news");
      banner("Searching the news…");
    } },
  { group: "Run", name: "Review money-making ideas", run: async () => {
      await post("/api/routine/ventures");
      banner("Venture review running…");
    } },
  { group: "Run", name: "Learn from what I've been doing", run: async () => {
      banner("Reviewing…");
      const r = await post("/api/learning/mine");
      banner(`${(r && r.insights || []).length} insights updated.`);
      refreshLife();
    } },
  { group: "Run", name: "Reflect now", run: async () => {
      await post("/api/cognition/tick");
      banner("Reflection pass run.");
    } },
  { group: "Run", name: "Check my play time", run: async () => {
      await post("/api/routine/playtime");
      refreshLife();
    } },

  { group: "Do", name: "Focus mode", hint: "F", run: () =>
      setFocus(!document.body.classList.contains("focus")) },
  { group: "Do", name: "Toggle microphone", hint: "space", run: () => el.mic.click() },
  { group: "Do", name: "Mute the voice", run: () => el.speaker.click() },
  { group: "Do", name: "Stop talking", hint: "esc", run: () => voice.stopSpeaking() },
  { group: "Do", name: "Quiet for an hour", run: async () => {
      await post("/api/nudge/snooze", { minutes: 60 });
      banner("Quiet for an hour.");
    } },
  { group: "Do", name: "Clear the conversation", run: () => {
      el.transcript.innerHTML = "";
      banner("Cleared on screen — the memory is untouched.");
    } },
];

let paletteMatches = [];
let paletteIndex = 0;

function openPalette() {
  el.palette.classList.add("visible");
  el.paletteInput.value = "";
  el.paletteInput.focus();
  renderPalette("");
}

function closePalette() {
  el.palette.classList.remove("visible");
  el.input.focus();
}

function renderPalette(query) {
  const needle = query.trim().toLowerCase();
  paletteMatches = COMMANDS.filter((c) =>
    !needle || c.name.toLowerCase().includes(needle) || c.group.toLowerCase().includes(needle)
  );

  // Anything that isn't a command becomes a memory search, so one box does
  // both without the user having to decide which they meant.
  if (needle && paletteMatches.length === 0) {
    paletteMatches = [{
      group: "Search",
      name: `Search memory for “${query.trim()}”`,
      run: () => { showView("memory"); el.recallInput.value = query.trim(); runRecall(query.trim()); },
    }, {
      group: "Ask",
      name: `Ask JARVIS: “${query.trim()}”`,
      run: () => submit(query.trim()),
    }];
  } else if (needle) {
    paletteMatches = paletteMatches.concat([{
      group: "Ask",
      name: `Ask JARVIS: “${query.trim()}”`,
      run: () => submit(query.trim()),
    }]);
  }

  paletteIndex = 0;
  paintPalette();
}

function paintPalette() {
  el.paletteList.innerHTML = "";
  let group = null;
  paletteMatches.forEach((command, index) => {
    if (command.group !== group) {
      group = command.group;
      const header = document.createElement("div");
      header.className = "palette-group";
      header.textContent = group;
      el.paletteList.appendChild(header);
    }
    const node = document.createElement("div");
    node.className = `palette-item${index === paletteIndex ? " selected" : ""}`;
    node.innerHTML = `<span class="palette-name"></span>`
      + (command.hint ? `<span class="palette-hint">${command.hint}</span>` : "");
    node.querySelector(".palette-name").textContent = command.name;
    node.addEventListener("click", () => runPalette(index));
    el.paletteList.appendChild(node);
  });
  el.paletteList.querySelector(".selected")?.scrollIntoView({ block: "nearest" });
}

function runPalette(index) {
  const command = paletteMatches[index];
  closePalette();
  command?.run();
}

el.paletteOpen.addEventListener("click", openPalette);
el.palette.addEventListener("click", (event) => {
  if (event.target === el.palette) closePalette();
});
el.paletteInput.addEventListener("input", () => renderPalette(el.paletteInput.value));
el.paletteInput.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    paletteIndex = Math.min(paletteIndex + 1, paletteMatches.length - 1);
    paintPalette();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    paletteIndex = Math.max(paletteIndex - 1, 0);
    paintPalette();
  } else if (event.key === "Enter") {
    event.preventDefault();
    runPalette(paletteIndex);
  } else if (event.key === "Escape") {
    closePalette();
  }
});

/* ------------------------------------------------------------ voice UI -- */

function populateVoices() {
  const voices = voice.listVoices();
  el.voiceSelect.innerHTML = "";

  if (!voices.length) {
    // getVoices() populates asynchronously, so this is normal for a moment —
    // onVoicesChanged re-runs us. If it never fires, the browser genuinely has
    // no speech synthesis and we should say so rather than spin forever.
    const option = document.createElement("option");
    option.textContent = "loading voices…";
    el.voiceSelect.appendChild(option);
    el.voiceHint.textContent = "Looking for installed voices…";
    clearTimeout(populateVoices._timer);
    populateVoices._timer = setTimeout(() => {
      if (!voice.listVoices().length) {
        el.voiceSelect.innerHTML = "";
        const none = document.createElement("option");
        none.textContent = "no voices available";
        el.voiceSelect.appendChild(none);
        el.voiceHint.textContent =
          "This browser reports no speech voices. Chrome, Edge and Safari all "
          + "ship them; Firefox needs system voices installed.";
      }
    }, 2500);
    return;
  }
  clearTimeout(populateVoices._timer);

  const good = /Natural|Online|Enhanced|Premium|Siri|^Google/i;
  for (const item of voices) {
    const option = document.createElement("option");
    option.value = item.name;
    // Flag the ones that actually sound good: the list is long and the
    // quality difference between them is enormous.
    option.textContent = `${good.test(item.name) ? "★ " : ""}${item.name} · ${item.lang}`;
    option.selected = item.selected;
    el.voiceSelect.appendChild(option);
  }
  el.voiceHint.textContent = voices.some((v) => good.test(v.name))
    ? "★ marks the higher-quality voices. Pick one and press Test."
    : "Only basic system voices are installed — see the README for better ones.";
}

function syncVoiceControls() {
  el.voiceRate.value = String(voice.rate);
  el.voicePitch.value = String(voice.pitch);
  el.voiceRateValue.textContent = `${Number(voice.rate).toFixed(2)}×`;
  el.voicePitchValue.textContent = Number(voice.pitch).toFixed(2);
}

voice.onVoicesChanged = () => populateVoices();

el.voiceSelect.addEventListener("change", () => {
  if (voice.setVoice(el.voiceSelect.value)) {
    voice.preview("Voice set. This is how I'll sound.");
  }
});
el.voiceRate.addEventListener("input", () => { voice.setRate(el.voiceRate.value); syncVoiceControls(); });
el.voiceRate.addEventListener("change", () => voice.preview("Speed set like this."));
el.voicePitch.addEventListener("input", () => { voice.setPitch(el.voicePitch.value); syncVoiceControls(); });
el.voicePitch.addEventListener("change", () => voice.preview("Pitch set like this."));
el.voiceTest.addEventListener("click", () => voice.preview());
el.voiceReset.addEventListener("click", () => {
  voice.setRate(1.04);
  voice.setPitch(0.92);
  syncVoiceControls();
  voice.preview("Reset to the default delivery.");
});

/* ------------------------------------------------------------------ input -- */

function submit(text, spoken = false) {
  const message = (text ?? el.input.value).trim();
  if (!message) return;
  el.input.value = "";
  el.interim.textContent = "";
  send({ action: "message", text: message, spoken });
}

el.send.addEventListener("click", () => submit());
el.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});

el.mic.addEventListener("click", async () => {
  const listening = await voice.toggleListening();
  if (!listening) {
    el.mic.classList.remove("active");
    el.micLabel.textContent = "MIC OFF";
  }
});

el.speaker.addEventListener("click", () => {
  const muted = !voice.muted;
  voice.setMuted(muted);
  el.speaker.classList.toggle("muted", muted);
  el.speaker.title = muted ? "Voice output muted" : "Voice output active";
});

el.wake.addEventListener("change", () => {
  voice.requireWakeWord = el.wake.checked;
  banner(el.wake.checked
    ? 'Wake word armed — say "Jarvis" before a command.'
    : "Wake word off — everything heard is a command.");
});

const OPENERS = [
  "What should I be doing right now?",
  "Where did my time go today?",
  "What's in my inbox that matters?",
  "Give me a business idea that fits me.",
];

function showOpeners() {
  el.suggestions.innerHTML = "";
  for (const text of OPENERS) {
    const button = document.createElement("button");
    button.textContent = text;
    button.addEventListener("click", () => submit(text));
    el.suggestions.appendChild(button);
  }
}

window.addEventListener("keydown", (event) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);

  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    return el.palette.classList.contains("visible") ? closePalette() : openPalette();
  }
  if (event.key === "Escape") {
    // One key backs out of everything, innermost first, so it is always the
    // way out — including out of focus mode, where the rail is hidden.
    if (el.palette.classList.contains("visible")) return closePalette();
    if (el.briefing.classList.contains("visible")) {
      return el.briefing.classList.remove("visible");
    }
    if (el.nudge.classList.contains("visible")) return hideNudge();
    if (document.body.classList.contains("focus")) return setFocus(false);
    voice.stopSpeaking();
    return el.input.focus();
  }
  if (typing) return;

  if (event.code === "Space") {
    event.preventDefault();
    el.mic.click();
  } else if (event.key.toLowerCase() === "f") {
    setFocus(!document.body.classList.contains("focus"));
  } else if (event.key >= "1" && event.key <= "8") {
    showView(VIEWS[Number(event.key) - 1]);
  }
});

/* ------------------------------------------------------------------- loop -- */

function frame(now) {
  const level = voice.level;
  brain.setAudio(level);
  hud.setLevel(level);
  brain.render(now);
  hud.render(now);

  if (now - (frame._statsAt || 0) > 500) {
    frame._statsAt = now;
    el.fps.textContent = `${brain.stats().fps} fps`;
  }
  requestAnimationFrame(frame);
}

/* --------------------------------------------------------------- refresh -- */

const refreshToday = async () => renderToday(await api("/api/today"));
const refreshInbox = async () => renderInbox(await api("/api/inbox"));
const refreshWork = async () => renderWork(await api("/api/work"));
const refreshLife = async () => renderLife(await api("/api/activity"));

async function refreshStatus() {
  applyStatus(await api("/api/status"));
}

async function refreshMemoryPanel() {
  const data = await api("/api/memory");
  if (data) renderGoals(data.goals || []);
}

// Only the visible panel refreshes on demand; the polling below keeps the
// badges honest without re-rendering panels nobody is looking at.
const REFRESHERS = {
  today: refreshToday,
  inbox: refreshInbox,
  work: refreshWork,
  ventures: refreshWork,
  life: refreshLife,
  memory: refreshMemoryPanel,
  system: refreshStatus,
  mind: () => {},
};

window.__jarvis = {
  brain, hud, helix, voice, setState, submit, showView, setFocus,
  palette: openPalette,
  nudge: (text, tone = "coach") => showNudge({ text, tone, title: "JARVIS" }),
  status: () => statusCache,
};

window.addEventListener("resize", () => helix.resize());

showView(activeView);
if (store.get("focus", false)) setFocus(true);
showOpeners();
syncVoiceControls();
populateVoices();
connect();

refreshStatus();
refreshToday();
refreshInbox();
refreshWork();
refreshLife();
refreshMemoryPanel();

setInterval(refreshStatus, 8000);
setInterval(() => { refreshToday(); refreshInbox(); }, 20000);
setInterval(refreshWork, 15000);
setInterval(refreshLife, 30000);

requestAnimationFrame(frame);
el.input.focus();
