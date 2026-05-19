<script lang="ts">
  import {
    connectClaudeDesktop,
    fetchConsentPolicy,
    fetchKeyCapabilities,
    fetchSettings,
    keychainAdd,
    keychainSearch,
    linkKeyCapability,
    fullListeningSync,
    listeningStart,
    listeningStatus,
    listeningStop,
    screenContextStatus,
    snapshotLifeEvidence,
    updateConsentPolicy,
    updateSettings,
    type CapabilityRef,
    type ConsentPolicy,
    type KeychainItemMeta,
    type ScreenContextStatus,
    type Settings,
  } from "$lib/api";

  let screenWatch = $state<ScreenContextStatus | null>(null);
  let lifeSyncing = $state(false);
  let lifeMsg = $state("");

  async function loadScreen() {
    try {
      screenWatch = await screenContextStatus();
    } catch {
      screenWatch = null;
    }
  }
  void loadScreen();

  let policy = $state<ConsentPolicy | null>(null);
  let appSettings = $state<Settings | null>(null);
  let saving = $state(false);
  let settingsSaving = $state(false);
  let msg = $state("");
  let settingsMsg = $state("");
  let connecting = $state(false);
  let micActive = $state(false);
  let micBusy = $state(false);
  let fullListeningOn = $state(false);
  let lastWakeTs = $state(0);
  let lastWakeExcerpt = $state("");

  const collectorLabels: Record<string, string> = {
    window_focus: "Window focus",
    ax_content_changed: "Accessibility text changes",
    process_snapshot: "Process snapshots (~60s)",
    app_launched: "App launches",
    browser_visit: "Browser URL hints",
    listening: "Mic listening sessions",
    screenshot_fallback: "Screenshot when AX empty",
    screen_reader: "Screen reader (all visible windows)",
  };

  let keyQuery = $state("");
  let keyResults = $state<KeychainItemMeta[]>([]);
  let keyCaps = $state<CapabilityRef[]>([]);
  let keyCapKey = $state("payment_method");
  let keyService = $state("");
  let keyAccount = $state("");
  let keySecret = $state("");
  let keyMsg = $state("");
  let keyBusy = $state(false);

  async function loadKeys() {
    try {
      const r = await fetchKeyCapabilities();
      keyCaps = r.items;
    } catch {
      keyCaps = [];
    }
  }

  async function searchKeys() {
    keyBusy = true;
    keyMsg = "";
    try {
      keyResults = await keychainSearch(keyQuery);
    } catch (e) {
      keyMsg = e instanceof Error ? e.message : String(e);
    } finally {
      keyBusy = false;
    }
  }

  async function addKey() {
    keyBusy = true;
    keyMsg = "";
    try {
      const item = await keychainAdd(keyService, keyAccount, keySecret);
      await linkKeyCapability({
        cap_key: keyCapKey,
        vault_ref: item.vault_ref,
        label: item.label,
        provider: "keychain",
      });
      keySecret = "";
      keyMsg = `Linked ${item.label}.`;
      await loadKeys();
    } catch (e) {
      keyMsg = e instanceof Error ? e.message : String(e);
    } finally {
      keyBusy = false;
    }
  }

  async function linkExisting(item: KeychainItemMeta) {
    keyBusy = true;
    keyMsg = "";
    try {
      await linkKeyCapability({
        cap_key: keyCapKey,
        vault_ref: item.vault_ref,
        label: item.label,
        provider: "keychain",
      });
      keyMsg = `Linked ${item.label}.`;
      await loadKeys();
    } catch (e) {
      keyMsg = e instanceof Error ? e.message : String(e);
    } finally {
      keyBusy = false;
    }
  }

  void loadKeys();

  async function load() {
    policy = await fetchConsentPolicy();
    const s = await fetchSettings();
    appSettings = s.settings;
    try {
      const st = await listeningStatus();
      micActive = st.active && !st.full_listening;
      fullListeningOn = st.full_listening === true;
      lastWakeTs = st.last_wake_ts ?? 0;
      lastWakeExcerpt = st.last_wake_excerpt ?? "";
    } catch {
      micActive = false;
      fullListeningOn = false;
    }
  }

  async function saveFullListening() {
    if (!appSettings) return;
    settingsSaving = true;
    settingsMsg = "";
    try {
      const r = await updateSettings({
        full_listening_enabled: appSettings.full_listening_enabled,
        ambient_collectors: {
          ...(appSettings.ambient_collectors ?? {}),
          full_listening: appSettings.full_listening_enabled,
        },
      });
      appSettings = r.settings;
      const st = await fullListeningSync();
      fullListeningOn = st.full_listening === true;
      micActive = st.active && !st.full_listening;
      lastWakeTs = st.last_wake_ts ?? 0;
      settingsMsg = appSettings.full_listening_enabled
        ? "Full listening on — macOS may ask for Microphone access."
        : "Full listening off.";
      await loadScreen();
    } catch (e) {
      settingsMsg = e instanceof Error ? e.message : String(e);
    } finally {
      settingsSaving = false;
    }
  }

  async function saveAmbient() {
    if (!appSettings) return;
    settingsSaving = true;
    settingsMsg = "";
    try {
      const r = await updateSettings({
        ambient_sensing_enabled: appSettings.ambient_sensing_enabled,
        capture_on_empty_ax: appSettings.capture_on_empty_ax !== false,
        ambient_collectors: appSettings.ambient_collectors,
      });
      appSettings = r.settings;
      settingsMsg = "Ambient settings saved.";
      await loadScreen();
    } catch (e) {
      settingsMsg = e instanceof Error ? e.message : String(e);
    } finally {
      settingsSaving = false;
    }
  }

  async function toggleMic() {
    micBusy = true;
    settingsMsg = "";
    try {
      if (micActive) {
        await listeningStop();
        micActive = false;
        settingsMsg = "Listening stopped.";
      } else {
        await listeningStart();
        micActive = true;
        settingsMsg = "Listening started — macOS may ask for Microphone access.";
      }
    } catch (e) {
      settingsMsg = e instanceof Error ? e.message : String(e);
    } finally {
      micBusy = false;
    }
  }

  async function save() {
    if (!policy) return;
    saving = true;
    msg = "";
    try {
      policy = await updateConsentPolicy(policy);
      msg = "Saved.";
    } catch (e) {
      msg = e instanceof Error ? e.message : String(e);
    } finally {
      saving = false;
    }
  }

  load();

  async function addClaude() {
    connecting = true;
    msg = "";
    try {
      await connectClaudeDesktop();
      msg = "Added to Claude Desktop — quit and reopen Claude.";
    } catch (e) {
      msg = e instanceof Error ? e.message : String(e);
    } finally {
      connecting = false;
    }
  }
</script>

<h1 style="margin:0 0 1rem;">Settings</h1>

<div class="card">
  <h2>Watching your desk</h2>
  <p class="muted">
    Minion’s primary substrate is what’s on your Mac: focused windows, visible apps, browser page text,
    and optional mic. Grant <strong>Accessibility</strong> and <strong>Automation</strong> (browsers);
  <strong>Screen Recording</strong> enables per-window screenshots when text APIs are empty; <strong>Microphone</strong> for listening.
    File imports and MCP are secondary.
  </p>
</div>

<div class="card">
  <h2>Claude (MCP)</h2>
  <button type="button" class="btn btn-primary" disabled={connecting} onclick={() => void addClaude()}>
    {connecting ? "…" : "Add to Claude"}
  </button>
</div>

{#if policy}
  <div class="card">
    <h2>Claude MCP consent</h2>
    <label style="display:flex;gap:0.5rem;align-items:center;margin:0.5rem 0;">
      <input type="checkbox" bind:checked={policy.readers.mcp.allow_screen_context_tools} />
      Allow screen-context MCP tools
    </label>
    <label style="display:flex;gap:0.5rem;align-items:center;margin:0.5rem 0;">
      <input
        type="checkbox"
        checked={!policy.readers.mcp.deny_chunk_source_kinds.includes("ambient-ax")}
        onchange={(e) => {
          const on = (e.currentTarget as HTMLInputElement).checked;
          const kinds = new Set(policy!.readers.mcp.deny_chunk_source_kinds);
          if (on) {
            kinds.delete("ambient");
            kinds.delete("ambient-ax");
          } else {
            kinds.add("ambient");
            kinds.add("ambient-ax");
          }
          policy!.readers.mcp.deny_chunk_source_kinds = [...kinds];
        }}
      />
      Allow ambient memory in MCP search (off by default)
    </label>
    <button type="button" class="btn btn-primary" disabled={saving} onclick={() => void save()}>{saving ? "…" : "Save consent"}</button>
    {#if msg}<p class="muted">{msg}</p>{/if}
  </div>
{/if}

<div class="card">
  <h2>Ambient capture (macOS)</h2>
  <p class="muted">
    Signals append to <code>ambient/stream.jsonl</code> and ingest into vault-local
    <code>ambient_events</code>. MCP export stays gated by consent.
  </p>
  {#if appSettings}
    <label style="display:flex;gap:0.5rem;align-items:center;margin:0.5rem 0;">
      <input type="checkbox" bind:checked={appSettings.ambient_sensing_enabled} />
      Ambient sensing enabled
    </label>
    <label style="display:flex;gap:0.5rem;align-items:center;margin:0.5rem 0;">
      <input
        type="checkbox"
        checked={appSettings.capture_on_empty_ax !== false}
        onchange={(e) => {
          appSettings!.capture_on_empty_ax = (e.currentTarget as HTMLInputElement).checked;
        }}
      />
      Capture screenshots when Accessibility text is empty (Screen Recording)
    </label>
    {#each Object.entries(collectorLabels) as [key, label]}
      <label style="display:flex;gap:0.5rem;align-items:center;margin:0.35rem 0;">
        <input
          type="checkbox"
          checked={(appSettings.ambient_collectors as Record<string, boolean> | undefined)?.[key] !== false}
          onchange={(e) => {
            const on = (e.currentTarget as HTMLInputElement).checked;
            appSettings!.ambient_collectors = {
              ...(appSettings!.ambient_collectors ?? {}),
              [key]: on,
            };
          }}
        />
        {label}
      </label>
    {/each}
    <button type="button" class="btn btn-primary" disabled={settingsSaving} onclick={() => void saveAmbient()}>
      {settingsSaving ? "…" : "Save ambient collectors"}
    </button>
    {#if settingsMsg}<p class="muted">{settingsMsg}</p>{/if}
  {/if}
  <h3 style="margin:1rem 0 0.35rem;font-size:0.95rem;">Full listening</h3>
  <p class="muted">
    Continuous mic capture into your vault only (<code>ambient/listening/</code>). Transcripts stay local;
    MCP export remains gated by consent. Wake phrase: say <strong>minion</strong> (matched in transcript text).
  </p>
  {#if appSettings}
    <label style="display:flex;gap:0.5rem;align-items:center;margin:0.5rem 0;">
      <input
        type="checkbox"
        checked={appSettings.full_listening_enabled === true}
        onchange={(e) => {
          const on = (e.currentTarget as HTMLInputElement).checked;
          appSettings!.full_listening_enabled = on;
        }}
      />
      Full listening (stores transcripts locally)
    </label>
    <p class="muted" style="font-size:0.8rem;margin:0.25rem 0 0.5rem;">
      Requires Microphone permission in System Settings → Privacy. Restarts capture on launch while enabled.
    </p>
    <button type="button" class="btn btn-primary" disabled={settingsSaving} onclick={() => void saveFullListening()}>
      {settingsSaving ? "…" : "Apply full listening"}
    </button>
    <p class="muted" style="margin-top:0.5rem;">
      Status:
      {#if fullListeningOn}
        <strong>active</strong> (continuous)
      {:else if micActive}
        <strong>manual session</strong>
      {:else}
        paused
      {/if}
      {#if lastWakeTs}
        · last wake {new Date(lastWakeTs * 1000).toLocaleString()}
        {#if lastWakeExcerpt}
          — “{lastWakeExcerpt.slice(0, 60)}{lastWakeExcerpt.length > 60 ? "…" : ""}”
        {/if}
      {/if}
    </p>
  {/if}
  <h3 style="margin:1rem 0 0.35rem;font-size:0.95rem;">Manual mic session</h3>
  <p class="muted">30s WAV segments without full-time mode. Disabled while full listening is on.</p>
  <button
    type="button"
    class="btn btn-primary"
    disabled={micBusy || fullListeningOn}
    onclick={() => void toggleMic()}
  >
    {micBusy ? "…" : micActive ? "Stop listening" : "Start listening"}
  </button>
  {#if screenWatch?.macos_watch}
    <ul class="capture-list muted">
      <li>Window focus — {screenWatch.macos_watch.watchers_env_disabled ? "off (env)" : "on"}</li>
      <li>Accessibility text — {screenWatch.macos_watch.ax_text_sample_enabled ? "on" : "off"}</li>
      <li>Screenshots — {screenWatch.macos_watch.pixel_capture_requested ? "on" : "off"}</li>
      <li>Poll interval — {screenWatch.macos_watch.poll_interval_sec}s</li>
    </ul>
  {/if}
  <h3 style="margin:1rem 0 0.35rem;font-size:0.95rem;">Contacts &amp; calendar</h3>
  <p class="muted">
    Council evidence uses local Contacts/Calendar snapshots. macOS will ask for permission the first time.
  </p>
  <button
    type="button"
    class="btn btn-primary"
    disabled={lifeSyncing}
    onclick={() => {
      lifeSyncing = true;
      lifeMsg = "";
      void snapshotLifeEvidence()
        .then((r) => {
          lifeMsg = `Synced ${r.contacts} contact(s), ${r.events} calendar row(s).`;
        })
        .catch((e) => {
          lifeMsg = e instanceof Error ? e.message : String(e);
        })
        .finally(() => {
          lifeSyncing = false;
        });
    }}
  >
    {lifeSyncing ? "Syncing…" : "Sync contacts & calendar now"}
  </button>
  {#if lifeMsg}<p class="muted">{lifeMsg}</p>{/if}
</div>

<div class="card">
  <h2>Keys (macOS Keychain)</h2>
  <p class="muted">
    Secrets stay in the system Keychain. Minion only stores a reference (e.g. <code>keychain:service:account</code>)
    for council skills like payments.
  </p>
  {#if keyCaps.length}
    <p class="muted">Linked: {keyCaps.map((k) => k.label).join(", ")}</p>
  {/if}
  <label class="field">
    Capability
    <select bind:value={keyCapKey}>
      <option value="payment_method">payment_method</option>
    </select>
  </label>
  <label class="field">
    Search Keychain
    <input type="search" bind:value={keyQuery} placeholder="service or account" />
  </label>
  <button type="button" class="btn" disabled={keyBusy} onclick={() => void searchKeys()}>
    {keyBusy ? "…" : "Search"}
  </button>
  {#if keyResults.length}
    <ul class="key-results">
      {#each keyResults.slice(0, 20) as item (item.vault_ref)}
        <li>
          <span>{item.label}</span>
          <button type="button" class="btn btn-sm" disabled={keyBusy} onclick={() => void linkExisting(item)}>
            Link
          </button>
        </li>
      {/each}
    </ul>
  {/if}
  <h3 style="margin:1rem 0 0.35rem;font-size:0.95rem;">Add generic password</h3>
  <label class="field">Service <input bind:value={keyService} /></label>
  <label class="field">Account <input bind:value={keyAccount} /></label>
  <label class="field">Secret <input type="password" bind:value={keySecret} autocomplete="off" /></label>
  <button type="button" class="btn btn-primary" disabled={keyBusy} onclick={() => void addKey()}>
    {keyBusy ? "…" : "Save & link"}
  </button>
  {#if keyMsg}<p class="muted">{keyMsg}</p>{/if}
</div>

<div class="card">
  <h2>Support</h2>
  <p class="muted">Diagnostics and advanced options remain in the legacy module if needed during transition.</p>
</div>

<style>
  .capture-list {
    margin: 0.5rem 0 0;
    padding-left: 1.1rem;
    font-size: 0.85rem;
  }
  .btn-primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  .field {
    display: block;
    margin: 0.5rem 0;
    font-size: 0.85rem;
  }
  .field input,
  .field select {
    display: block;
    width: 100%;
    margin-top: 0.25rem;
    padding: 0.35rem 0.5rem;
    border-radius: 6px;
    border: 1px solid var(--border);
  }
  .key-results {
    list-style: none;
    padding: 0;
    margin: 0.75rem 0 0;
    font-size: 0.85rem;
  }
  .key-results li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0;
    border-bottom: 1px solid var(--border);
  }
  .btn-sm {
    font-size: 0.75rem;
    padding: 0.2rem 0.5rem;
  }
</style>
