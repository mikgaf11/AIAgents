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
│  Tools         files · shell · python · web · memory     │
│  Memory        SQLite + FTS5, facts/episodes/goals       │
│  Cognition     background reflection, proactive speech   │
└──────────────────────────┬──────────────────────────────-┘
                           │
                    Claude Opus 5 (adaptive thinking)
```

## Install it once, then forget it exists

**macOS / Linux** — double-click **`Install JARVIS.command`**
**Windows** — double-click **`Install JARVIS.bat`**

(Or from a terminal: `python3 install.py`.)

It creates the virtual environment, installs dependencies, asks for your
Anthropic API key once, and then:

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

Click the icon, then either talk or type.

- **Space** — toggle the microphone
- **Escape** — cut off speech mid-sentence
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

## What it can actually do

**Reasoning.** Claude Opus 5 with adaptive thinking and configurable effort.
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
| `web_search` / `web_fetch` | Anthropic server-side tools, run on their infra |

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

## The HUD

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

Drag to rotate. Space toggles the mic. Escape stops speech.

## Configuration

Everything is environment variables (see `.env.example`). The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `JARVIS_MODEL` | `claude-opus-5` | The reasoning core |
| `JARVIS_EFFORT` | `high` | `low`–`max`; `xhigh` for the hardest agentic work |
| `JARVIS_SAFE_MODE` | `1` | See below |
| `JARVIS_COGNITION_INTERVAL` | `45` | Seconds between reflection passes |
| `JARVIS_PROACTIVE_AFTER_IDLE` | `120` | Silence required before it speaks first |

### Safe mode

On by default, and it is the difference between a demo and something with
real reach:

- File access is confined to `jarvis/workspace/`; paths are canonicalized and
  traversal is rejected.
- Shell commands must be a single program from an allowlist, with no shell
  operators (`;`, `|`, `&`, backticks, redirection).
- `run_python` blocks imports of `socket`, `subprocess`, `ctypes`, and
  `multiprocessing`.

Setting `JARVIS_SAFE_MODE=0` gives the model an unrestricted shell and the
whole filesystem, as the user running the process. That is a real decision —
make it deliberately, and read the persona prompt's operating limits first.

Credentials are never written to memory, and the system prompt says so
explicitly.

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q      # 30 tests, no API key needed
```

The suite covers memory retrieval and FTS injection safety, tool sandboxing,
the event bus, and — via a scripted mock model — the real streaming tool loop:
parallel tool rounds, `pause_turn` resumption, refusal handling, usage
accounting, history trimming that never orphans a `tool_result`, and the
mid-conversation-system-message fallback.

## Layout

```
jarvis/
├── install.py         one-time setup: autostart service + app icon
├── uninstall.py       removes both, leaves your data alone
├── launcher.py        what the icon runs: ensures the core is up, opens the HUD
├── server/
│   ├── core.py        streaming turn loop, tool orchestration, telemetry
│   ├── cognition.py   the background mind
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
│   └── js/app.js      transport, panels, animation loop
└── tests/
```
