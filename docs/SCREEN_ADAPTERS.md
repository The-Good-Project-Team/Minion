# Screen Adapter Setup

Minion owns capture, fusion, SQLite storage, search, and graph fill. Heavy
visual models stay outside the repo and feed normalized JSON back through
adapter commands.

Run readiness after any setup:

```bash
./bin/minion screen-memory-status \
  --workspace /Users/reify/Classified/minion/chatgpt_mcp_memory \
  --probe
```

## Adapter Contract

Commands receive one input path or URL and write JSON or JSONL to stdout.
Use `{input}` for files and `{url}` for browser URLs.

```bash
export MINION_MARLIN_CMD="python chatgpt_mcp_memory/scripts/screen_adapters/marlin_hf_adapter.py {input}"
export MINION_OMNIPARSER_CMD="python chatgpt_mcp_memory/scripts/screen_adapters/omniparser_json_adapter.py {input}"
export MINION_GENERAL_VLM_CMD="python chatgpt_mcp_memory/scripts/screen_adapters/general_vlm_json_adapter.py {input}"
export MINION_PLAYWRIGHT_DOM_CMD="node /path/to/playwright-dom-snapshot.mjs {url}"
```

`remember-screen` normalizes these records into ambient events before fusion.
`screen-memory-status --probe` executes configured Marlin, OmniParser, and
general VLM commands against one recent input without appending records.

## Playwright DOM

Built in:

```bash
desktop/scripts/playwright-dom-snapshot.mjs "https://example.com"
```

Playwright DOM is enabled by default when the shipped script exists. Disable:

```bash
export MINION_DISABLE_PLAYWRIGHT_DOM=1
```

Expected output shape:

```json
{
  "kind": "dom_snapshot",
  "url": "https://example.com",
  "dom_text_sample": "visible page text",
  "visible_elements": [
    {"role": "button", "label": "Export", "bounds": [10, 20, 80, 24], "source": "Playwright"}
  ]
}
```

## Marlin-2B

Marlin should answer what happened and when for a rolling video clip. The
included `marlin_hf_adapter.py` is a thin Hugging Face wrapper around
`NemoStation/Marlin-2B`; install its heavy dependencies outside this repo.
The wrapper emits one JSON object per clip:

```json
{
  "scene": "User is reviewing payout history",
  "start_sec": 2,
  "end_sec": 8.5,
  "events": [
    {"type": "review", "summary": "Payouts table is visible", "timestamp": 4, "duration": 2}
  ],
  "confidence": 0.82
}
```

Minion accepts `start_sec`, `end_sec`, `start_time`, `end_time`, `timestamp`,
and `duration`, normalizes them into `time_range`, and returns `time_range`
and `clip_path` in `search_screen_memory` hits.

## OmniParser

Install Microsoft OmniParser outside this repo and point `OMNIPARSER_CMD` at a
command that accepts a screenshot path and prints JSON. Then use Minion's
`omniparser_json_adapter.py` as `MINION_OMNIPARSER_CMD`; it normalizes common
OmniParser output shapes. Expected normalized output:

```json
{
  "visible_elements": [
    {"role": "button", "label": "Export", "bounds": [812, 210, 96, 38], "source": "OmniParser", "confidence": 0.9}
  ],
  "confidence": 0.74
}
```

If your wrapper returns `elements` instead of `visible_elements`, Minion maps it
automatically.

## General VLM Fallback

General VLM reasoning is the lowest-trust layer and should only fill gaps when
DOM/accessibility, user events, Marlin, OmniParser, and OCR are not enough. Set
`GENERAL_VLM_CMD` to a screenshot caption command and point
`MINION_GENERAL_VLM_CMD` at the included normalizer:

```bash
export GENERAL_VLM_CMD="/path/to/your/vlm-captioner {input}"
export MINION_GENERAL_VLM_CMD="python chatgpt_mcp_memory/scripts/screen_adapters/general_vlm_json_adapter.py {input}"
```

Expected normalized output:

```json
{
  "scene": "The screen appears to show a payout dashboard",
  "confidence": 0.35
}
```

## Verification Loop

1. Make sure the desktop app has Screen Recording and Accessibility permissions.
2. Let it collect at least one screenshot and one rolling video clip.
3. Export the adapter env vars in the environment that launches the sidecar.
4. Run `minion remember-screen`.
5. Run `minion screen-memory-status --probe`.
6. Search:

```bash
./bin/minion search "where did I see the export button?"
./bin/minion search "what happened in the payout clip?"
```
