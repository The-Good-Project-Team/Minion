# Minion

**Minion** is a **capture-first macOS second brain**: it watches what you work on (window focus, accessibility text, selective screenshots when text is thin), ingests files you drop in, and stores everything in a **local vault** at `~/Library/Application Support/Minion/data`. The app is **chat-first**: on first run it introduces itself in plain language, asks what to call you, and only then requests optional permissions (Contacts sync happens in the background). You can paste text or add files to context in a couple clicks, and your assistants can read the same vault over **MCP**—either via **stdio** (Claude Desktop) or **LAN HTTP** (other clients on your network).

---

## What you get

| Surface | Role |
|--------|------|
| **Chat (onboarding + daily loop)** | A conversation-first UI that keeps setup human: intro → name → optional permissions → connector choices. |
| **Activity** | Chronological river of context—ambient capture, ingest/sync jobs, health issues, council suggestions, and threads. |
| **42** | Proactive guide inside the stream. Surfaces questions and nudges so you do not have to invent good prompts; reply or dismiss. |
| **Sources** | Drop zone, inbox watcher, ingest toggles, and search over your indexed corpus. |
| **Add context** | Paste text to ingest immediately, or pick files/folders and copy them into the inbox for indexing. |
| **Mirror / graph** | Life-graph scaffold (people, projects, obligations, …) with evidence attached; identity mirror and wiki routes where enabled. |
| **MCP** | Memory tools (`ask_minion`, working context, wiki/work proposals). Works over stdio (Claude Desktop) and also over LAN HTTP (`POST /mcp`, password protected). |

Under the hood: one **`memory.db`** (plus vectors) in the vault; override the data directory with `MINION_DATA_DIR`. macOS capture respects deny lists and keeps screenshots sparse (OCR when accessibility text is thin). Settings cover sidecar paths, Claude one-click MCP setup, and per-format ingest.

**Ships today:** Tauri desktop, Python sidecar, ambient capture, Activity + 42, vault + MCP, life-graph seed, council-style suggestions. **Roadmap** (mirror sharing, belief plasticity, tiered compression at scale, ambient audio, and more) is spelled out in [`docs/ROADMAP.md`](docs/ROADMAP.md)—not promised here.

### Documentation

| Doc | Contents |
|-----|----------|
| [`docs/SECOND_BRAIN.md`](docs/SECOND_BRAIN.md) | Four-loop model, routes, Activity feed, MCP posture |
| [`docs/LIFE_GRAPH.md`](docs/LIFE_GRAPH.md) | Graph scaffold, domains, retrieval order |
| [`docs/SCREEN_MEMORY.md`](docs/SCREEN_MEMORY.md) | Screen-memory capture, fuse, retrieval, and guidance loop |
| [`docs/SCREEN_ADAPTERS.md`](docs/SCREEN_ADAPTERS.md) | Playwright, Marlin, and OmniParser adapter command setup |
| [`docs/SCREEN_MEMORY_AUDIT.md`](docs/SCREEN_MEMORY_AUDIT.md) | Requirement-to-evidence checklist for the screen-memory MVP |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Product direction and phased themes |
| [`docs/CONTEXT_PLATFORM.md`](docs/CONTEXT_PLATFORM.md) | Product spine: vault → context server → world model → live preferences |
| [`docs/PRIVACY_MATRIX.md`](docs/PRIVACY_MATRIX.md) | Reader scopes and privacy strata (desktop vs MCP vs LAN) |
| [`docs/PRODUCT_SPINE_DECISIONS.md`](docs/PRODUCT_SPINE_DECISIONS.md) | Decisions and invariants for the product backbone |
| [`docs/TESTING.md`](docs/TESTING.md) | Tests that reflect user journeys (release bar) |
| [`chatgpt_mcp_memory/README.md`](chatgpt_mcp_memory/README.md) | Tool tables, parsers, env flags, CLI |
| [`desktop/README.md`](desktop/README.md) | Tauri architecture, `tauri dev` / release bundles |
| [`AGENTS.md`](AGENTS.md) · [`PROCESS.md`](PROCESS.md) | Agent playbook and delivery loop (contributors) |

### Repo layout

| Path | What it is |
|------|------------|
| [`desktop/`](desktop/) | macOS app: **SvelteKit** UI + **Tauri 2** Rust shell (`src-tauri/`). |
| [`chatgpt_mcp_memory/`](chatgpt_mcp_memory/) | **Python** sidecar (FastAPI, ingest, SQLite, MCP). |

On `tauri build`, `desktop/src-tauri/scripts/sync_sidecar.sh` copies `chatgpt_mcp_memory` into `desktop/src-tauri/resources/sidecar/` (gitignored; canonical source stays `chatgpt_mcp_memory/`).

---

## Context makes the difference

When an AI has access to Minion's index, the output changes character entirely. Here Claude is asked to rewrite website copy *"in my voice — ask minion"*. It calls `ask_minion` across dozens of past conversations, reads the pattern ("short sentences, direct subject-verb, 'we' naturally, thinks out loud without hedging"), and rewrites the copy in that register — quoting actual lines back as evidence.

Without Minion: generic, polished, forgettable.
With Minion: the AI knows how you actually write and produces something that sounds like you wrote it yourself.

![Claude rewriting copy in the user's voice after calling ask_minion across dozens of conversations](docs/readme/context-enhanced-voice.png)

---

## Screenshots

| Main window — drop zone and ingest | Activity — live parse / embed log |
|:---:|:---:|
| ![Minion main window: drop files or folders, supported types, Settings](docs/readme/main-window.png) | ![Activity log showing ingested markdown files and chunk counts](docs/readme/activity-log.png) |

| Claude Desktop — Minion MCP in a real thread | macOS — Minion in Launchpad |
|:---:|:---:|
| ![Claude using Minion tools to answer from indexed memory](docs/readme/claude-desktop-memory.png) | ![Minion app icon in the macOS Launchpad grid](docs/readme/macos-launchpad.png) |

**Preferences** — Status (sources, chunks, sidecar, paths), **Claude (MCP)** one-click config, and **Ingest & file types**:

| Status | Claude (MCP) | Ingest & types |
|:---:|:---:|:---:|
| ![Preferences: Status tab with sources, chunks, inbox watch, sidecar](docs/readme/preferences-status.png) | ![Preferences: Claude MCP tab with Add to Claude and success message](docs/readme/preferences-claude-mcp.png) | ![Preferences: Ingest and file types with toggles per format](docs/readme/preferences-ingest.png) |

---

## Install (macOS app)

### 1. Pick the right download (Intel vs Apple Silicon)

On [**GitHub Releases**](https://github.com/reif-is-a-foofie/Minion/releases) there are **two** macOS downloads. **Read the file name**—it says which kind of Mac it is for:

| Download file name contains… | Use for | How to tell on the Mac |
|------------------------------|---------|---------------------------|
| **`…-macOS-Apple-Silicon.zip`** | **Apple Silicon** — M1, M2, M3, M4, … | Apple menu → **About This Mac** → **Chip:** “Apple M2” (or any Apple M…). |
| **`…-macOS-Intel.zip`** | **Intel** — Core i5 / i7 / i9, Xeon, … | **About This Mac** → **Processor:** line includes **Intel**. |

If you pick the wrong one, macOS may refuse to open the app or show an architecture error. Delete **Minion.app**, download the **other** zip, unzip, and drag **Minion.app** to **Applications** again.

### 2. Install and run

1. Download the matching zip from [**GitHub Releases**](https://github.com/reif-is-a-foofie/Minion/releases) (or build from source — below).
2. Unzip, then move **Minion.app** to **Applications** when prompted (avoid running forever from the disk image or a translocated copy).
3. **First launch** can take a few minutes while Minion prepares its embedded Python environment and starts the sidecar. Later launches are quick.
4. Follow the chat onboarding (name, optional permissions). You can paste text or add files to context from the main window.
5. Optional: open **Settings → Claude (MCP)** and click **Add to Claude**, then **fully quit and reopen Claude Desktop**.
6. Optional (LAN MCP for other clients): point an MCP client at `http://<this-mac-lan-ip>:8765/mcp` and set `Authorization: Bearer foofie` (or override with `MINION_MCP_HTTP_TOKEN`).
5. Optional: import a **ChatGPT export** (zip or folder) via the drop zone so the index has history on day one.

**Ollama (image captions):** If you do not already have Ollama installed, Minion can download the **official macOS Ollama app** into your Minion data folder on first launch and start it locally. To skip that behavior, set **`MINION_SKIP_MANAGED_OLLAMA=1`** before opening the app.

---

## Build from source (developers)

```bash
git clone https://github.com/reif-is-a-foofie/Minion.git
cd Minion   # or your clone path, e.g. ~/Classified/minion
git submodule update --init --recursive

cd chatgpt_mcp_memory
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd ../desktop
npm install
npm run tauri dev
```

The Rust shell prefers `../chatgpt_mcp_memory/.venv/bin/python`. Release builds produce `desktop/src-tauri/target/release/bundle/macos/Minion.app`; see [`desktop/README.md`](desktop/README.md) for bundling notes.

---

## CLI (`minion` command)

For a **terminal-first** setup (export path, `minion doctor`, `minion setup`, inbox CRUD without the GUI), use the launcher in **`bin/minion`** and [`chatgpt_mcp_memory/README.md`](chatgpt_mcp_memory/README.md). The desktop app and the CLI share the same store and MCP server code.

Screen-memory MVP commands:

```bash
./bin/minion remember-screen
./bin/minion search "where did I see the export button?"
./bin/minion summarize-last 30m
./bin/minion what-was-i-doing
./bin/minion guidance
./bin/minion screen-memory-status --probe
./bin/minion verify-screen-memory
```

When run from this checkout, screen-memory commands use the repo-local `chatgpt_mcp_memory/.venv` automatically. Use `--workspace` only when you want to run against another installed workspace.

---

## Privacy

Indexing, capture, and search run **on your machine**. MCP speaks **stdio** to Claude Desktop by default—no cloud “memory service” for your chunks. Optional components (for example **Ollama** for voice or captions, or **HF Hub** for some embedding downloads) only touch the network if you configure them. Keep exports and large corpora **outside** the git tree if you prefer.

If you use LAN MCP, the sidecar can bind to `0.0.0.0` (see `MINION_API_HOST`) so other machines on your network can reach `POST /mcp`. `POST /mcp` is password-protected (default `foofie`, override with `MINION_MCP_HTTP_TOKEN`). Other HTTP API routes may require `MINION_API_TOKEN` depending on route and configuration.

---

## Credits

Built and dogfooded by **Reif** — questions: [reif@thegoodproject.net](mailto:reif@thegoodproject.net).

**Cursor skills catalog:** [Awesome Cursor Skills](https://github.com/spencerpauly/awesome-cursor-skills) by **[Spencer Pauly](https://github.com/spencerpauly)** — vendored in-repo at [`third_party/awesome-cursor-skills`](third_party/awesome-cursor-skills). Third-party list; not affiliated with Minion.
