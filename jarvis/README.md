# JARVIS

A resident AI assistant with a voice, persistent memory, real tools, a mind
that keeps running when you're not talking to it, and a HUD built around a
live neural core that visualizes what it's actually doing.

It is not a chat wrapper. The brain on screen is driven by real telemetry
from the reasoning loop: when Claude is thinking, the network contracts and
ignites; when it calls a tool, the palette shifts to amber and signals march;
when it speaks, a shockwave crosses the volume in time with the voice.

```
┌─ browser ────────────────────────────────────────────────┐
│  WebGL neural core · 2D reticle overlay · panels         │
│  Web Speech recognition ─┐          ┌─ speech synthesis  │
└──────────────────────────┼──────────┼───────────────────-┘
                           │ websocket│  (token + state stream)
┌─ python core ────────────▼──────────▼───────────────────-┐
│  Jarvis        streaming turn loop, parallel tool rounds │
│  Tools         files · shell · python · web · desktop    │
│  Memory        SQLite + FTS5, facts/episodes/goals       │
│  Cognition     background reflection, proactive speech   │
│  Activity      what you're doing, in app + title only    │
│  Learning      patterns mined from it, fed back as context│
└──────────────────────────┬──────────────────────────────-┘
                           │
        Claude Opus 5  ──or──  a local model via Ollama (free)
```

## Install it once, then forget it exists

**macOS / Linux** — double-click **`Install JARVIS.command`**
**Windows** — double-click **`Install JARVIS.bat`**

(Or from a terminal: `python3 install.py`.)

It creates the virtual environment, installs dependencies, asks once how you
want it to think (Claude, or a free local model — see below), and then:

- **starts JARVIS automatically when you log in**, restarting it if it ever dies
- **adds a JARVIS icon** to your Applications / app menu / Desktop
- **opens the HUD in its own window** when you click that icon — no address bar,
  no tab, behaves like a native app

After that you never touch a terminal. Click the icon, or just leave the tab
open — the core is already running in the background either way.

Everything is per-user and local: no admin rights, nothing system-wide, and
the server only ever binds `127.0.0.1`, so it is not reachable from your
network.

To remove the autostart and the icon: `python3 uninstall.py`. It leaves your
project, key and memory alone.

### Using it

Click the icon, then either talk or type. Press **⌘K** (Ctrl-K) for the
command palette — it's the front door to everything, and anything it doesn't
recognise as a command becomes a question for JARVIS.

- **Space** — toggle the microphone
- **Escape** — back out of whatever is open; then cut off speech
- **F** — focus mode: just the brain and the conversation
- **1–8** — jump to a panel
- **Wake word** — tick it and JARVIS only acts on speech that starts with
  "Jarvis"; leave it off and everything it hears is a command
- **Drag** the brain to rotate it

It interrupts you cleanly: start talking while it's speaking and it stops.

Try: *"What's my CPU doing?"* · *"Remember I prefer metric units."* ·
*"Remind me in ten minutes to check the oven."* · *"What did we decide about
the launch date?"* · *"Search the web for the latest on X and summarize it."*

> Voice needs Chrome, Edge, Brave or Safari — Firefox has no Web Speech
> recognition. The launcher picks a Chromium-family browser automatically if
> you have one, whatever your system default is.

### Is it actually running?

```bash
python3 status.py
```

One command, and every failing line names the command that fixes it:

```
  [  ok  ] core                   up at http://127.0.0.1:8788
  [  ok  ] reasoning              llama3.1:8b — on this machine, free
  [  ok  ] starts at login        systemd user unit enabled
  [ down ] watching               cannot read the foreground window
         └─ sudo apt install xdotool
  [ warn ] file access            workspace only
         └─ to reach your real files, set JARVIS_FILE_ROOTS in jarvis/.env
```

Closing the browser window doesn't stop anything — the core is a separate
background process. Mail still gets triaged, work still runs, and nudges
still reach you through OS notifications. The window is just a view onto it.

Three commands are the whole maintenance surface:

| | |
|---|---|
| `python3 status.py` | Is everything working, and what's the fix if not |
| `python3 restart.py` | Pick up changes to `.env` (waits for the old process to die) |
| `python3 uninstall.py` | Remove autostart and the icon; data and keys untouched |

### Changing the voice

Click **VOICE** in the composer bar. You get every voice your system has,
with the good ones sorted to the top and marked ★, plus speed and pitch
sliders and a Test button. Your choice is remembered.

The quality gap between voices is large, and it's worth installing a better
one rather than settling for the default:

- **Windows** — Settings → Time & Language → Speech → Add voices. The
  "Natural" voices (Ryan, Sonia, Guy, Aria) are dramatically better than the
  rest. Edge exposes them to JARVIS automatically.
- **macOS** — System Settings → Accessibility → Spoken Content → System
  Voice → Manage Voices. Download a "Premium" or "Enhanced" English voice;
  Daniel (UK) is the closest to the film.
- **Linux** — install `speech-dispatcher` with `espeak-ng` or, for much
  better results, `mbrola` voices via your package manager.

If you want genuinely film-grade speech, that needs a paid neural TTS
service (ElevenLabs and similar) rather than the browser's built-in
synthesis. Nothing in the current build calls one — say the word if you
want that wired in.

### Running it by hand instead

If you'd rather not install the autostart:

```bash
cd jarvis
cp .env.example .env          # add your ANTHROPIC_API_KEY
./run.sh                      # creates a venv, installs deps, boots the core
```

Without credentials it still boots: the HUD, memory and tools all run, and it
tells you plainly that the reasoning core is missing.

### What it costs to leave running

The background mind wakes every 45 seconds, but a tick only calls the model
when something actually changed — a new message, a goal moved, a reminder
appeared. On an idle machine it falls back to one cheap pass every 15 minutes,
so leaving JARVIS on all day costs a few cents rather than a few dollars.

Tune it in `.env`: `JARVIS_DEEP_REFLECTION` (seconds between passes on a static
world), or `JARVIS_COGNITION=0` to switch the background mind off entirely and
make it purely reactive.

## Free, or better: pick a reasoning core

There are two, and the installer asks which you want.

**Claude** (`JARVIS_BACKEND=anthropic`). Far better reasoning, and the only
option that gets you web search, long agentic tool chains that stay coherent,
and thinking summaries rich enough to drive the deep-layer brain activity.
Costs per token — see the running-cost section above.

**A local model** (`JARVIS_BACKEND=ollama`). Free forever, runs entirely on
your machine, nothing leaves it. Install [Ollama](https://ollama.com), then:

```bash
ollama pull llama3.1:8b      # ~5 GB, runs on 8 GB of RAM
```

That's the whole setup. JARVIS speaks Ollama's protocol through an adapter
that presents the same surface as the Anthropic client, so tools, memory,
autonomy and the HUD all work identically.

Be honest with yourself about the tradeoff: an 8B model is noticeably weaker.
It follows multi-step tool chains less reliably, its judgement about when to
interrupt you is cruder, and it has no web search (that tool runs on
Anthropic's infrastructure and has no local equivalent, so it's silently
withheld rather than offered and broken). If you have a bigger machine,
`llama3.1:70b` or `qwen2.5:32b` close much of the gap. Models that emit
`<think>` blocks — deepseek-r1, qwen3 — get that text routed to the thinking
stream, so the cognition panel works locally too.

The default, `JARVIS_BACKEND=auto`, uses Claude when a key is present and
falls back to the local model when it isn't. Start free, add a key later,
change nothing else.

**If it's still on Claude and you wanted free**, that's `auto` doing its job —
it prefers Claude whenever it finds credentials. Force it either way:

```bash
python3 backend.py          # what is it using right now?
python3 backend.py local    # free, on this machine
python3 backend.py claude   # back to the API
```

That writes the setting into `.env` and restarts the core. It refuses to
switch to a local model that isn't pulled yet, rather than leaving you with a
core that can't think.

## Watching you work

With `JARVIS_ACTIVITY=1` (the default), JARVIS samples the **foreground
application name and window title** every 20 seconds. That is the entire
scope: no screenshots, no keystrokes, no page contents, no network capture.
An app name and a window title are enough to know you spent two hours in a
game or forty minutes in an editor, which is all the features below need.

Everything stays in `jarvis/data/jarvis.db` on your machine. `JARVIS_ACTIVITY=0`
switches it off completely.

Sessions shorter than 45 seconds are discarded as alt-tab noise. Browsers are
classified by what they're showing rather than by being a browser — a tab on
YouTube counts as media, one on GitHub counts as code. To teach it your own
apps, drop a JSON file at `data/activity_rules.json`:

```json
{"game": ["mygameclient"], "work": ["ourinternaltool"]}
```

The **Life** tab shows where today went, how it compares to your play limit,
and every conclusion it has drawn about you — each with the evidence behind
it and an **×** to delete it. Wrong beliefs have to be erasable, or the thing
becomes annoying and then useless.

### What "learning" means here, precisely

Nothing on this machine retrains a model. Fine-tuning needs GPUs and a
curated dataset, and on a single person's data it reliably makes a model
*worse*, not better. Anyone telling you their assistant "learns by watching"
in the weights sense is selling something.

What actually happens is more useful and much cheaper: every couple of hours
it reviews where your time went, what you asked for, and what you corrected,
and writes down durable **insights** — "they code most between 9pm and 1am",
"they abandon tasks queued on Fridays". Those are injected as context into
every future conversation. The retrieval gets richer as the file grows, which
is what makes it feel like it knows you. It is grounded in evidence you can
read and delete, rather than in a black box.

### Interrupting you

Two things can interrupt: the **playtime register** and the **coach**.

The playtime register is pure arithmetic — no model call, no judgement. Past
`JARVIS_PLAY_LIMIT` minutes of game and media time in a day, it tells you the
number once, then holds off for `JARVIS_PLAY_REMINDER_EVERY` minutes. It
reports and gets out of the way; the point is to make time visible, not to
police it.

The coach is the model deciding whether right now is worth saying something
about, given what you're doing, how long you've been at it, your goals, and
what it knows about you. Its prompt makes silence the default and forbids
checking in, praising, greeting, or nagging about the same thing twice.

Both arrive as a **floating helix popup** that speaks aloud, plus a real OS
notification so it lands even when the HUD is buried behind a game. Every
nudge carries *Got it* / *Not helpful* / *Snooze*, and your answer is stored
as feedback that shapes later ones.

Interruptions are rate limited hard: a floor of `JARVIS_NUDGE_MIN_GAP`
seconds between them and at most `JARVIS_NUDGE_MAX_PER_HOUR`. An assistant
that interrupts freely gets muted within a day, which makes it worthless.

## Reaching the rest of your machine

Safe mode confines file access to `jarvis/workspace/`. That's the right
default and the wrong place to stop — an assistant that can't touch your real
files can't do much. Rather than making you choose between a sandbox and an
unrestricted shell, name the folders you actually want it in:

```bash
JARVIS_FILE_ROOTS=~/Documents,~/Projects,~/Desktop
```

Safe mode stays on. Those directories become readable, writable and
searchable; everything else is still refused, and traversal out of them is
rejected. With that set it can find a file you half remember, open it, edit
it, and put it back.

It can also open files and folders with whatever app normally handles them,
launch applications by name, and play music from `JARVIS_MUSIC_DIRS` or a
streaming URL — including play/pause/skip against whatever is already
playing, via media keys on Windows, MPRIS on Linux and AppleScript on macOS.
Set `JARVIS_ALLOW_OPEN=0` if you'd rather it only read and wrote.

Every one of those calls hands the OS an argv list, never a shell string, so
a filename containing `;` opens a file instead of running a command.

## Connecting your email

```bash
python3 connect.py
```

It asks for your address and an app password, works out the IMAP/SMTP
settings from the domain (Gmail, Outlook, iCloud, Fastmail, Yahoo, Proton
Bridge, or anything you type in by hand), proves the login works before
saving, and stores it in `jarvis/.env` at mode 600.

**Use an app password, not your real one.** Every major provider issues them
once you have 2FA on — Google Account → Security → App passwords, and the
equivalent on the others. `connect.py` names the right page for your provider.

From then on JARVIS checks your inbox every few minutes and, for each new
message, decides:

- **priority** — critical, high, normal, low, or noise
- **category** — personal, work, financial, opportunity, admin, newsletter,
  automated, spam
- **a one-line summary** and, when there is one, **the next action**

Crucially it judges importance *to you*: the classifier is handed your stored
facts, open goals and tracked ventures, so a message about a project you're
actually working on outranks a louder one that has nothing to do with you.
Mail that scores high or critical gets **flagged in your real mailbox**, so
the triage shows up in whatever mail app you already use — JARVIS isn't
another inbox to check. Only *critical* mail is allowed to speak up.

### Nothing gets sent without you

JARVIS drafts replies freely. Sending is a separate, deliberate act:

1. It writes a draft — into the Inbox tab, never onto the wire.
2. You read it and press **Approve & Send** or **Discard**.
3. Sending only works at all if you set `JARVIS_EMAIL_ALLOW_SEND=1`.

Both gates are on by default. Autonomous background work can draft, but it
has no send tool at all — that's enforced by its restricted tool surface,
not just by instructions.

## Working on its own

These run in the background whether or not you're there:

| Routine | Cadence | What it does |
|---|---|---|
| Mail sweep | every 3 min | Fetch, triage, flag, and queue drafts for anything needing a reply |
| Morning briefing | 08:00 daily | Spoken digest: what landed overnight, what deserves attention today |
| News bulletin | 08:02 daily | Searches your topics, reads back what actually happened |
| Venture review | every 6h | Propose and sharpen money-making opportunities |
| Work queue | continuous | Execute queued tasks with full tools, then report back |
| Playtime register | every minute | Free arithmetic on today's game and media time |
| Coach | every 30 min | Decides whether right now is worth interrupting for; usually not |
| Learning pass | every 2h | Mines your activity into insights fed back as context |

The news bulletin is off until you name topics (`JARVIS_NEWS_TOPICS=AI,
markets, Arsenal`) and needs web search, so it's a Claude-backend feature.

The **work queue** is the interesting one. JARVIS can queue work for itself —
research a question you left open, draft something you'll need, dig into a
stalled goal — and each task runs as a real agentic loop with tools: it reads
files, runs code, searches the web, writes results to disk, and reports what
it actually did. You can queue work yourself by just asking; watch it run in
the **Work** tab.

Background runs can't ask you questions mid-task, so their prompt tells them
to make reasonable calls and state the assumption rather than stalling — and
to report honestly when a tool failed instead of claiming success.

## Money and ventures

The **Ventures** tab tracks concrete opportunities rather than a list of
generic ideas. Every proposal has to carry a thesis grounded in what you can
actually do, a **first step you could take this week**, an honest confidence
score, and what the assistant could and couldn't verify.

Be clear-eyed about what this is. It is a good research assistant and a
candid sounding board: it will tell you when an idea is weak, name the risk
alongside the upside, and check claims about pricing or demand against the
web. It cannot predict markets, and it doesn't do get-rich schemes,
stock-picking, or anything requiring capital you haven't told it about. The
prompt pushes it toward *below* 0.5 confidence on most ideas, because most
ideas deserve that. Your judgement decides; its job is to be specific and
honest.

## What it can actually do

**Reasoning.** Claude Opus 5 with adaptive thinking and configurable effort,
or a local model via Ollama for free.
Thinking is requested as `display: "summarized"`, which is what feeds the
Cognition Stream panel and the deep-layer brain activity — with the default
(`omitted`) there would be nothing to show.

**Tools.** Executed in parallel within a turn, each one narrated to the HUD:

| Tool | What it does |
|---|---|
| `get_time` | Current local time — called before any temporal reasoning |
| `remember` / `recall` / `forget` | Durable facts that survive restarts |
| `set_goal` / `list_goals` / `update_goal` | Objectives pursued across sessions |
| `set_reminder` / `list_reminders` | Timed nudges that interrupt when due |
| `read_file` / `write_file` / `list_dir` / `search_files` | Real filesystem access |
| `run_python` | In-process execution for calculation and analysis |
| `shell` | Shell commands in the workspace |
| `system_status` | CPU, memory, disk, uptime, battery |
| `find_files` | Find a file by name anywhere it's allowed to look |
| `open_path` / `launch_app` | Open a file, folder or URL; start an application |
| `play_music` / `media_control` | Play a track or URL; pause/skip whatever is playing |
| `activity_report` | Where your hours actually went, per app and category |
| `notify_me` | Surface a helix popup and speak, even behind a fullscreen game |
| `web_search` / `web_fetch` | Anthropic server-side tools, run on their infra |
| `list_emails` / `read_email` / `search_email` | Its view of your triaged inbox |
| `draft_reply` / `mark_email_handled` | Prepare replies for approval; clear the queue |
| `queue_task` / `list_tasks` | Give itself background work and check on it |
| `propose_venture` / `list_ventures` / `update_venture` | The opportunity pipeline |

**Memory.** SQLite with FTS5. Retrieval blends lexical relevance, a recency
half-life, and a stored importance weight, which behaves much like a small
vector store while staying inspectable with any SQLite browser. Facts are
upserted by subject rather than accumulated, so it doesn't collect
contradictory near-duplicates.

Retrieved context rides in the `messages` array as a mid-conversation
`role: "system"` turn, which keeps the cached system prefix byte-identical
across the whole session. Models that reject that role fall back
automatically to folding context into the user turn.

**Autonomy.** A background loop wakes every ~45s, builds a snapshot (recent
conversation, open goals, stored facts, its own prior notes, host telemetry,
idle time), and reflects on it with a cheaper model. It can record
observations, promote facts, advance goals, and decide to speak first — but
only after you've been quiet for a while and not more often than the cooldown
allows. Silence is the designed default. Due reminders bypass the model
entirely and always speak.

**Voice.** Continuous recognition with an optional wake word, barge-in (it
stops talking the moment you start), and sentence-by-sentence synthesis so
speech keeps pace with the token stream instead of waiting for the full reply.

## The interface

The brain owns the screen. Everything else is chrome that stays out of its
way: a slim top bar, an icon rail, one contextual dock on the right, and the
conversation floating at the bottom.

| | |
|---|---|
| **⌘K / Ctrl-K** | Command palette — run any routine, jump anywhere, or search memory. Anything it doesn't recognise becomes a question for JARVIS. |
| **1–8** | Jump straight to a panel |
| **F** | Focus mode — everything but the brain and the conversation gets out of the way |
| **Space** | Toggle the microphone |
| **Escape** | Backs out of whatever is innermost: palette, overlay, nudge, focus mode, then speech |

Eight panels, one at a time, so each gets real room instead of five cramped
tabs:

| Panel | What's in it |
|---|---|
| **Today** | The dashboard: what needs a reply, where the day went, what it noticed |
| **Mind** | Live reasoning stream and the tool trace |
| **Inbox** | Triaged mail and drafts waiting for approval |
| **Work** | The background queue — and a box to give it something to do |
| **Life** | Time per category, play against your limit, everything it has concluded about you |
| **Ventures** | The money-making pipeline |
| **Memory** | Search everything it remembers, capture a fact, set an objective |
| **System** | Which backend is running and what it costs, host vitals, voice settings |

### The brain

| Cognitive state | Look |
|---|---|
| `idle` | Slow teal drift, occasional sparks |
| `listening` | Bright cyan, surface bulges with your voice |
| `thinking` | Violet, network contracts inward, cascades fire, spin accelerates |
| `tool` | Amber, dense and structured, signals march in lockstep |
| `speaking` | Cyan-white, shockwave crossing the volume per utterance |
| `dreaming` | Deep indigo, slow — the background mind reflecting |
| `alert` | Red, jittery, fast |

The geometry is generated, not modelled: points are rejection-sampled inside
an implicit brain volume (two hemispheres split by a longitudinal fissure,
plus a cerebellum and brainstem), biased toward the cortex, displaced along
their normals by a fold function to carve gyri, then wired with a spatial
hash into a synapse graph with a few long-range association fibres. Signal
pulses ride the edges, positioned entirely in the vertex shader.

The whole interface palette is a CSS variable rewritten by `body[data-state]`,
so the panels shift hue along with the core.

Drag to rotate.

## Configuration

Everything is environment variables (see `.env.example`). The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `JARVIS_BACKEND` | `auto` | `auto` \| `anthropic` \| `ollama` |
| `JARVIS_MODEL` | `claude-opus-5` | The reasoning core |
| `JARVIS_OLLAMA_MODEL` | `llama3.1:8b` | Used when running locally |
| `JARVIS_EFFORT` | `high` | `low`–`max`; `xhigh` for the hardest agentic work |
| `JARVIS_SAFE_MODE` | `1` | See below |
| `JARVIS_FILE_ROOTS` | *(empty)* | Extra folders reachable without leaving safe mode |
| `JARVIS_ACTIVITY` | `1` | Watch the foreground app; `0` disables it entirely |
| `JARVIS_PLAY_LIMIT` | `120` | Minutes of play before it says something |
| `JARVIS_NUDGE_MIN_GAP` | `900` | Seconds between interruptions |
| `JARVIS_NEWS_TOPICS` | *(empty)* | Topics for the morning bulletin |
| `JARVIS_COGNITION_INTERVAL` | `45` | Seconds between reflection passes |
| `JARVIS_PROACTIVE_AFTER_IDLE` | `120` | Silence required before it speaks first |

### Safe mode

On by default, and it is the difference between a demo and something with
real reach:

- File access is confined to `jarvis/workspace/` plus anything you list in
  `JARVIS_FILE_ROOTS`; paths are canonicalized and traversal is rejected.
- Shell commands must be a single program from an allowlist, with no shell
  operators (`;`, `|`, `&`, backticks, redirection).
- `run_python` blocks imports of `socket`, `subprocess`, `ctypes`, and
  `multiprocessing`.

Setting `JARVIS_SAFE_MODE=0` gives the model an unrestricted shell and the
whole filesystem, as the user running the process. That is a real decision —
make it deliberately, and read the persona prompt's operating limits first.

Credentials are never written to memory, and the system prompt says so
explicitly. Your email password lives only in `jarvis/.env` (mode 600) and is
never stored in the database, spoken, or written into a memory record.

Email specifically has three independent gates: sending is off unless you
enable it, every draft waits for a click, and the background worker has no
send tool in its surface at all.

### What it does *not* connect to

Email is the connector that's built. Calendar, Slack, Notion and the rest
aren't wired up — `server/connectors/` is the seam where they'd go, and each
needs its own auth and its own thinking about what autonomous access should
mean. I'd rather ship one connector that works properly than five that
half-work with your real accounts.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q      # 196 tests, no API key needed
```

The suite covers memory retrieval and FTS injection safety, tool sandboxing,
the event bus, and — via a scripted mock model — the real streaming tool loop:
parallel tool rounds, `pause_turn` resumption, refusal handling, usage
accounting, history trimming that never orphans a `tool_result`, and the
mid-conversation-system-message fallback.

It also covers the parts that reach outside: activity classification and
session accounting, nudge rate limiting, the playtime register firing once
rather than every tick, path escapes out of an allowed root, filenames
containing shell metacharacters, and the Ollama adapter's translation in both
directions — driven by a stubbed HTTP transport, so no local model is needed
to run it.

## Layout

```
jarvis/
├── install.py         one-time setup: autostart service + app icon
├── status.py          one-command health check with the fix for each failure
├── backend.py         switch between Claude and the free local model
├── connect.py         email setup wizard
├── uninstall.py       removes both, leaves your data alone
├── launcher.py        what the icon runs: ensures the core is up, opens the HUD
├── server/
│   ├── agent.py       the streaming tool loop, shared by chat and background work
│   ├── core.py        conversation state, memory context, telemetry
│   ├── autonomy.py    scheduled routines and the background work queue
│   ├── triage.py      email classification against your priorities
│   ├── connectors/    external services (email today; the seam for more)
│   ├── cognition.py   the background mind
│   ├── activity.py    foreground-app sampling and time accounting
│   ├── learning.py    mines behaviour into insights; decides when to coach
│   ├── notify.py      rate-limited nudges to the HUD and the desktop
│   ├── backends/      Claude and Ollama behind one interface
│   ├── memory.py      SQLite + FTS5 store and hybrid retrieval
│   ├── tools.py       every capability, plus the safe-mode sandbox
│   ├── persona.py     system prompts
│   ├── events.py      pub/sub bus feeding the HUD
│   ├── config.py      environment-driven configuration
│   └── main.py        FastAPI + websocket
├── web/
│   ├── js/brain.js    the WebGL neural core
│   ├── js/hud.js      2D reticle overlay
│   ├── js/voice.js    recognition, synthesis, amplitude
│   ├── js/helix.js    the helix that appears when it interrupts you
│   └── js/app.js      transport, panels, animation loop
└── tests/
```
