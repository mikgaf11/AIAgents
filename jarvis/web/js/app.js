/*
 * The HUD application: websocket transport, panel rendering, voice wiring,
 * and the single animation loop that drives both canvases.
 */

import { Brain } from "./brain.js";
import { Hud } from "./hud.js";
import { Voice } from "./voice.js";

const $ = (id) => document.getElementById(id);

const el = {
  brain: $("brain-canvas"),
  hud: $("hud-canvas"),
  state: $("state-label"),
  stateDot: $("state-dot"),
  model: $("stat-model"),
  latency: $("stat-latency"),
  rate: $("stat-rate"),
  tokens: $("stat-tokens"),
  fps: $("stat-fps"),
  neurons: $("stat-neurons"),
  transcript: $("transcript"),
  input: $("composer-input"),
  send: $("composer-send"),
  mic: $("mic-button"),
  micLabel: $("mic-label"),
  speaker: $("speaker-button"),
  wake: $("wake-toggle"),
  thoughts: $("thought-stream"),
  tools: $("tool-trace"),
  memory: $("memory-stream"),
  goals: $("goal-list"),
  vitals: $("vitals"),
  mood: $("mood-label"),
  interim: $("interim"),
  banner: $("banner"),
  inboxList: $("inbox-list"),
  inboxBadge: $("inbox-badge"),
  emailStatus: $("email-status"),
  draftList: $("draft-list"),
  workList: $("work-list"),
  workNow: $("work-now"),
  workBadge: $("work-badge"),
  ventureList: $("venture-list"),
  briefing: $("briefing"),
  briefingBody: $("briefing-body"),
  briefingClose: $("briefing-close"),
  voiceButton: $("voice-settings"),
  voicePanel: $("voice-panel"),
  voiceSelect: $("voice-select"),
  voiceRate: $("voice-rate"),
  voiceRateValue: $("voice-rate-value"),
  voicePitch: $("voice-pitch"),
  voicePitchValue: $("voice-pitch-value"),
  voiceTest: $("voice-test"),
  voiceReset: $("voice-reset"),
  voiceClose: $("voice-close"),
  voiceHint: $("voice-hint"),
};

const PRIORITY_LABELS = ["noise", "low", "normal", "high", "critical"];

/* ---------------------------------------------------------------- state -- */

const brain = new Brain(el.brain);
const hud = new Hud(el.hud);

let socket = null;
let reconnectDelay = 500;
let currentState = "idle";
let liveTurn = null; // the assistant bubble currently being streamed into
let spokenSoFar = ""; // how much of the live reply has been sent to the voice
let pendingThought = "";
let statusCache = {};

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

/* ---------------------------------------------------------- event router -- */

function handleEvent(event) {
  switch (event.kind) {
    case "hello":
    case "status":
      applyStatus(event.status);
      break;

    case "boot":
      banner(`${event.name} core online — ${event.model}`);
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

    case "tool_start":
      addTool(event);
      break;

    case "tool_end":
      completeTool(event);
      break;

    case "tool_args":
      break; // streamed argument fragments; the trace shows the final input

    case "tool_pause":
      addThought("server-side tool paused — resuming", "sys");
      break;

    case "memory":
      addMemory(event);
      break;

    case "cognition":
      addThought(event.text, event.mood || "calm");
      el.mood.textContent = (event.mood || "calm").toUpperCase();
      break;

    case "email":
      addInboxEntry(event);
      refreshInbox();
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
      addWorkResult(event);
      refreshWork();
      break;

    case "briefing":
      showBriefing(event.text);
      break;

    case "draft":
      refreshInbox();
      banner(
        event.status === "sent"
          ? `Sent to ${event.to}`
          : `Draft ${event.status}`
      );
      break;

    case "proactive":
      finishLive();
      addBubble("assistant", event.text, "proactive");
      voice.speak(event.text);
      refreshStatus();
      break;

    case "usage":
      el.latency.textContent = `${event.latency_ms} ms`;
      el.rate.textContent = `${event.tokens_per_second} tok/s`;
      if (event.totals) {
        el.tokens.textContent = formatTokens(event.totals);
      }
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

/* -------------------------------------------------------------- rendering -- */

function setState(state, intensity = 0.5, tool = null) {
  currentState = state;
  brain.setState(state, intensity);
  hud.setState(state, intensity);
  el.state.textContent = tool ? `${state.toUpperCase()} · ${tool}` : state.toUpperCase();
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

function addBubble(role, text, extra = "") {
  const node = document.createElement("div");
  node.className = `bubble ${role} ${extra}`.trim();
  node.innerHTML = `<span class="who">${role === "user" ? "YOU" : "JARVIS"}</span>`
    + `<span class="body"></span>`;
  node.querySelector(".body").textContent = text;
  el.transcript.appendChild(node);
  trimChildren(el.transcript, 60);
  el.transcript.scrollTop = el.transcript.scrollHeight;
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

  // Speak sentence by sentence as they complete, so the voice keeps pace
  // with the stream instead of waiting for the whole reply.
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
  // Anything the sentence-splitter didn't reach gets spoken now.
  const remainder = body.textContent.slice(spokenSoFar.length).trim();
  if (remainder) voice.speak(remainder);
  liveTurn.classList.remove("streaming");
  liveTurn = null;
  spokenSoFar = "";
}

function flushThought() {
  // Thinking arrives as a token stream; emit it in sentence-ish chunks so
  // the panel reads as thoughts rather than a character firehose.
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
  trimChildren(el.thoughts, 40);
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
  trimChildren(el.tools, 30);
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
  el.memory.appendChild(node);
  trimChildren(el.memory, 25);
  el.memory.scrollTop = el.memory.scrollHeight;
}

/* -------------------------------------------------------- inbox & work -- */

function priorityName(value) {
  return PRIORITY_LABELS[value] || "normal";
}

function addInboxEntry(event) {
  // Live arrival notice; the full list is re-rendered from the API.
  addThought(
    `mail [${event.label}] ${event.sender}: ${event.subject}`,
    event.priority >= 3 ? "concerned" : "reason"
  );
  if (event.priority >= 3) brain.ripple(0.9);
}

function renderInbox(data) {
  const messages = data.messages || [];
  const unhandled = messages.filter((m) => !m.handled);
  const urgent = unhandled.filter((m) => m.priority >= 3);

  el.inboxBadge.textContent = urgent.length ? String(urgent.length) : "";
  el.inboxBadge.classList.toggle("hot", urgent.length > 0);

  const status = data.email || {};
  if (!status.configured) {
    el.emailStatus.innerHTML =
      'not connected — run <code>python3 connect.py</code>';
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
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = status.configured ? "inbox clear" : "no mail connected";
    el.inboxList.appendChild(empty);
    return;
  }

  for (const message of unhandled.slice(0, 30)) {
    const node = document.createElement("div");
    node.className = `mail p${message.priority}`;
    node.innerHTML =
      `<div class="mail-head">` +
      `<span class="mail-priority"></span>` +
      `<span class="mail-from"></span>` +
      `<button class="mail-done" title="Mark handled">✓</button></div>` +
      `<div class="mail-subject"></div>` +
      `<div class="mail-summary"></div>`;
    node.querySelector(".mail-priority").textContent = priorityName(message.priority);
    node.querySelector(".mail-from").textContent = message.sender;
    node.querySelector(".mail-subject").textContent = message.subject;
    node.querySelector(".mail-summary").textContent =
      message.summary + (message.action ? ` → ${message.action}` : "");
    node.querySelector(".mail-done").addEventListener("click", async () => {
      await fetch(`/api/email/handled/${message.id}`, { method: "POST" });
      refreshInbox();
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
      `<div class="draft-head">DRAFT → <span class="draft-to"></span></div>` +
      `<div class="draft-subject"></div>` +
      `<div class="draft-body"></div>` +
      `<div class="draft-actions">` +
      `<button class="approve">APPROVE &amp; SEND</button>` +
      `<button class="discard">DISCARD</button></div>`;
    node.querySelector(".draft-to").textContent = draft.to_addr;
    node.querySelector(".draft-subject").textContent = draft.subject;
    node.querySelector(".draft-body").textContent = draft.body;

    node.querySelector(".approve").addEventListener("click", async () => {
      const response = await fetch(`/api/draft/${draft.id}/approve`, { method: "POST" });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        banner(detail.error || "Could not send", true);
      }
      refreshInbox();
    });
    node.querySelector(".discard").addEventListener("click", async () => {
      await fetch(`/api/draft/${draft.id}/discard`, { method: "POST" });
      refreshInbox();
    });
    el.draftList.appendChild(node);
  }
}

function addWorkResult(event) {
  addThought(
    `${event.ok ? "done" : "failed"}: ${event.title}`,
    event.ok ? "focused" : "error"
  );
}

function renderWork(data) {
  const tasks = data.tasks || [];
  const pending = tasks.filter((t) => t.status === "pending" || t.status === "running");
  el.workBadge.textContent = pending.length ? String(pending.length) : "";
  el.workBadge.classList.toggle("hot", pending.some((t) => t.status === "running"));

  const working = (data.autonomy || {}).working_on;
  if (working) {
    el.workNow.textContent = `▶ ${working}`;
    el.workNow.classList.add("busy");
  } else if (!el.workNow.classList.contains("busy")) {
    el.workNow.textContent = "";
  }

  el.workList.innerHTML = "";
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "nothing queued";
    el.workList.appendChild(empty);
    return;
  }
  for (const task of tasks.slice(0, 20)) {
    const node = document.createElement("div");
    node.className = `task ${task.status}`;
    node.innerHTML =
      `<div class="task-head"><span class="task-status"></span>` +
      `<span class="task-title"></span></div>` +
      (task.result ? `<div class="task-result"></div>` : "");
    node.querySelector(".task-status").textContent = task.status;
    node.querySelector(".task-title").textContent = task.title;
    if (task.result) node.querySelector(".task-result").textContent = task.result;
    el.workList.appendChild(node);
  }
}

function renderVentures(ventures) {
  el.ventureList.innerHTML = "";
  if (!ventures.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "no opportunities tracked yet";
    el.ventureList.appendChild(empty);
    return;
  }
  for (const venture of ventures.slice(0, 20)) {
    const node = document.createElement("div");
    node.className = `venture ${venture.status}`;
    const confidence = Math.round((venture.confidence || 0) * 100);
    node.innerHTML =
      `<div class="venture-head"><span class="venture-title"></span>` +
      `<span class="venture-conf"></span></div>` +
      `<div class="venture-meta"></div>` +
      `<div class="venture-thesis"></div>` +
      `<div class="venture-next"><b>next</b> <span></span></div>`;
    node.querySelector(".venture-title").textContent = venture.title;
    node.querySelector(".venture-conf").textContent = `${confidence}%`;
    node.querySelector(".venture-meta").textContent =
      `${venture.status} · ${venture.effort} effort · ${venture.horizon}`;
    node.querySelector(".venture-thesis").textContent = venture.thesis || "";
    node.querySelector(".venture-next span").textContent = venture.next_step || "—";
    el.ventureList.appendChild(node);
  }
}

function showBriefing(text) {
  el.briefingBody.textContent = text;
  el.briefing.classList.add("visible");
}

el.briefingClose.addEventListener("click", () =>
  el.briefing.classList.remove("visible")
);

async function refreshInbox() {
  try {
    renderInbox(await fetch("/api/inbox").then((r) => r.json()));
  } catch { /* link banner already reports outages */ }
}

async function refreshWork() {
  try {
    const data = await fetch("/api/work").then((r) => r.json());
    renderWork(data);
    renderVentures(data.ventures || []);
  } catch { /* as above */ }
}

/* ------------------------------------------------------------ voice UI -- */

function populateVoices() {
  const voices = voice.listVoices();
  el.voiceSelect.innerHTML = "";

  if (!voices.length) {
    // getVoices() populates asynchronously, so this is normal for a moment —
    // onVoicesChanged re-runs us. If it never fires, the browser genuinely
    // has no speech synthesis and we should say so rather than spin forever.
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

  for (const item of voices) {
    const option = document.createElement("option");
    option.value = item.name;
    // Flag the ones that actually sound good, since the list is long and
    // the quality difference between them is enormous.
    const good = /Natural|Online|Enhanced|Premium|Siri|^Google/i.test(item.name);
    option.textContent = `${good ? "★ " : ""}${item.name} · ${item.lang}`;
    option.selected = item.selected;
    el.voiceSelect.appendChild(option);
  }

  const starred = voices.filter((v) =>
    /Natural|Online|Enhanced|Premium|Siri|^Google/i.test(v.name)
  ).length;
  el.voiceHint.textContent = starred
    ? "★ marks the higher-quality voices. Pick one and press Test."
    : "Only basic system voices are installed — see the README for how to add better ones.";
}

function syncVoiceControls() {
  el.voiceRate.value = String(voice.rate);
  el.voicePitch.value = String(voice.pitch);
  el.voiceRateValue.textContent = `${Number(voice.rate).toFixed(2)}×`;
  el.voicePitchValue.textContent = Number(voice.pitch).toFixed(2);
}

voice.onVoicesChanged = () => populateVoices();

el.voiceButton.addEventListener("click", () => {
  const open = el.voicePanel.classList.toggle("visible");
  if (open) {
    populateVoices();
    syncVoiceControls();
  }
});

el.voiceClose.addEventListener("click", () =>
  el.voicePanel.classList.remove("visible")
);

el.voiceSelect.addEventListener("change", () => {
  if (voice.setVoice(el.voiceSelect.value)) {
    voice.preview("Voice set. This is how I'll sound.");
  }
});

el.voiceRate.addEventListener("input", () => {
  voice.setRate(el.voiceRate.value);
  syncVoiceControls();
});
el.voiceRate.addEventListener("change", () => voice.preview("Speed set like this."));

el.voicePitch.addEventListener("input", () => {
  voice.setPitch(el.voicePitch.value);
  syncVoiceControls();
});
el.voicePitch.addEventListener("change", () => voice.preview("Pitch set like this."));

el.voiceTest.addEventListener("click", () => voice.preview());

el.voiceReset.addEventListener("click", () => {
  voice.setRate(1.04);
  voice.setPitch(0.92);
  syncVoiceControls();
  voice.preview("Reset to the default delivery.");
});

/* tabs */
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab")) {
      other.classList.toggle("active", other === tab);
    }
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.classList.toggle("active", panel.dataset.panel === tab.dataset.tab);
    }
  });
}

function trimChildren(container, max) {
  while (container.children.length > max) container.removeChild(container.firstChild);
}

function formatTokens(totals) {
  const compact = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  return `${compact(totals.input || 0)}↓ ${compact(totals.output || 0)}↑`
    + (totals.cache_read ? ` ${compact(totals.cache_read)}⚡` : "");
}

/* ------------------------------------------------------------ status/API -- */

function applyStatus(status) {
  if (!status) return;
  statusCache = status;
  el.model.textContent = status.online ? status.model : "OFFLINE CORE";
  el.model.classList.toggle("warn", !status.online);
  if (status.tokens) el.tokens.textContent = formatTokens(status.tokens);
  renderVitals(status);
}

function renderVitals(status) {
  const host = status.host || {};
  const memory = status.memory || {};
  const rows = [
    ["CPU", host.cpu_percent != null ? `${host.cpu_percent}%` : "—", pct(host.cpu_percent)],
    ["MEM", host.memory_percent != null ? `${host.memory_percent}%` : "—", pct(host.memory_percent)],
    ["DISK", host.disk_percent != null ? `${host.disk_percent}%` : "—", pct(host.disk_percent)],
    ["FACTS", memory.facts ?? 0, null],
    ["EPISODES", memory.episodes ?? 0, null],
    ["SAFE MODE", status.safe_mode ? "ON" : "OFF", null],
  ];
  if (host.battery_percent != null) {
    rows.splice(3, 0, ["PWR", `${host.battery_percent}%`, pct(host.battery_percent)]);
  }
  el.vitals.innerHTML = "";
  for (const [label, value, fraction] of rows) {
    const row = document.createElement("div");
    row.className = "vital";
    row.innerHTML =
      `<span class="vital-label"></span><span class="vital-value"></span>` +
      (fraction != null ? `<span class="vital-bar"><i style="width:${fraction}%"></i></span>` : "");
    row.querySelector(".vital-label").textContent = label;
    row.querySelector(".vital-value").textContent = value;
    el.vitals.appendChild(row);
  }
}

const pct = (v) => (v == null ? null : Math.max(0, Math.min(100, v)));

async function refreshStatus() {
  try {
    const [status, memory] = await Promise.all([
      fetch("/api/status").then((r) => r.json()),
      fetch("/api/memory").then((r) => r.json()),
    ]);
    applyStatus(status);
    renderGoals(memory.goals || []);
  } catch {
    /* the websocket banner already reports link problems */
  }
}

function renderGoals(goals) {
  const open = goals.filter((g) => g.status === "open").slice(0, 8);
  el.goals.innerHTML = "";
  if (!open.length) {
    const empty = document.createElement("div");
    empty.className = "goal empty";
    empty.textContent = "no active objectives";
    el.goals.appendChild(empty);
    return;
  }
  for (const goal of open) {
    const node = document.createElement("div");
    node.className = "goal";
    node.innerHTML =
      `<span class="goal-title"></span>` +
      `<span class="goal-bar"><i style="width:${Math.round((goal.progress || 0) * 100)}%"></i></span>`;
    node.querySelector(".goal-title").textContent = `#${goal.id} ${goal.title}`;
    el.goals.appendChild(node);
  }
}

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
  banner(
    el.wake.checked
      ? 'Wake word armed — say "Jarvis" before a command.'
      : "Wake word off — everything heard is a command."
  );
});

// Space toggles the mic when you aren't typing.
window.addEventListener("keydown", (event) => {
  if (event.code === "Space" && document.activeElement !== el.input) {
    event.preventDefault();
    el.mic.click();
  }
  if (event.key === "Escape") {
    voice.stopSpeaking();
    el.input.focus();
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
    const stats = brain.stats();
    el.fps.textContent = `${stats.fps} fps`;
    el.neurons.textContent = `${stats.neurons} · ${stats.synapses}`;
  }
  requestAnimationFrame(frame);
}

// Exposed for debugging and for driving the visualization by hand:
//   __jarvis.setState("thinking", 1)   __jarvis.brain.ripple()
window.__jarvis = { brain, hud, voice, setState, submit, status: () => statusCache };

connect();
refreshStatus();
refreshInbox();
refreshWork();
setInterval(refreshStatus, 6000);
// Mail and background work change on their own schedule, so poll them
// independently of the conversation.
setInterval(refreshInbox, 20000);
setInterval(refreshWork, 15000);
requestAnimationFrame(frame);
el.input.focus();
