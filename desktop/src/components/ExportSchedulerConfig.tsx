import { useState, useEffect } from "react";
import {
  Clock,
  FolderOpen,
  Play,
  Settings,
  Loader2,
  CheckCircle2,
  AlertCircle,
  User,
} from "lucide-react";
import {
  fetchExportSchedulerStatus,
  fetchProfiles,
  triggerExportExport,
  updateExportSchedulerConfig,
  type ExportSchedulerStatus,
  type Profile,
} from "../lib/api";

export function ExportSchedulerConfig() {
  const [status, setStatus] = useState<ExportSchedulerStatus | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isTriggering, setIsTriggering] = useState(false);
  const [watchPath, setWatchPath] = useState("");
  const [intervalSec, setIntervalSec] = useState(3600);
  const [exportProfileId, setExportProfileId] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);

  useEffect(() => {
    void loadStatus();
  }, []);

  const loadStatus = async () => {
    setIsLoading(true);
    try {
      const [data, profilesRes] = await Promise.all([
        fetchExportSchedulerStatus(),
        fetchProfiles().catch(() => ({ profiles: [] as Profile[] })),
      ]);
      setStatus(data);
      setProfiles(profilesRes.profiles);
      setWatchPath(data.watch_path);
      setIntervalSec(data.interval_sec);
      setEnabled(data.enabled);
      setExportProfileId(
        data.export_profile_id ??
          data.active_profile_id ??
          profilesRes.profiles.find((p) => p.is_default)?.profile_id ??
          "default",
      );
    } catch (error) {
      console.error("Failed to load export scheduler status:", error);
      setMessage({ type: "error", text: "Failed to load scheduler status" });
    } finally {
      setIsLoading(false);
    }
  };

  const handleSave = async () => {
    setIsSaving(true);
    setMessage(null);
    try {
      await updateExportSchedulerConfig({
        export_watch_path: watchPath,
        export_interval_sec: intervalSec,
        export_profile_id: exportProfileId,
        enabled,
      });
      await loadStatus();
      setMessage({ type: "success", text: "Configuration saved" });
    } catch (error) {
      console.error("Failed to save configuration:", error);
      setMessage({ type: "error", text: "Failed to save configuration" });
    } finally {
      setIsSaving(false);
    }
  };

  const handleTrigger = async () => {
    setIsTriggering(true);
    setMessage(null);
    try {
      const result = await triggerExportExport();
      setMessage({
        type: "success",
        text: `Triggered export scan: ${result.ingested} files ingested`,
      });
      await loadStatus();
    } catch (error) {
      console.error("Failed to trigger export scan:", error);
      setMessage({ type: "error", text: "Failed to trigger export scan" });
    } finally {
      setIsTriggering(false);
    }
  };

  const formatTime = (timestamp: number | null) => {
    if (!timestamp) return "Never";
    const date = new Date(timestamp * 1000);
    return date.toLocaleString();
  };

  const formatInterval = (seconds: number) => {
    if (seconds < 60) return `${seconds} seconds`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes`;
    return `${Math.floor(seconds / 3600)} hours`;
  };

  const profileLabel =
    profiles.find((p) => p.profile_id === exportProfileId)?.name ?? exportProfileId;

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="w-4 h-4 animate-spin" />
        Loading export scheduler...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold flex items-center gap-2">
          <Clock className="w-4 h-4" />
          Export Scheduler
        </h3>
        <div className="flex items-center gap-2">
          <button
            onClick={handleTrigger}
            disabled={isTriggering}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          >
            {isTriggering ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-4 h-4" />
            )}
            Trigger Scan
          </button>
        </div>
      </div>

      {status && (
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div className="bg-muted/50 rounded-lg p-3">
            <div className="text-muted-foreground text-xs mb-1">Status</div>
            <div className={`font-medium ${status.enabled ? "text-green-600" : "text-muted-foreground"}`}>
              {status.enabled ? "Enabled" : "Disabled"}
            </div>
          </div>
          <div className="bg-muted/50 rounded-lg p-3">
            <div className="text-muted-foreground text-xs mb-1">Target Profile</div>
            <div className="font-medium">{profileLabel}</div>
          </div>
          <div className="bg-muted/50 rounded-lg p-3">
            <div className="text-muted-foreground text-xs mb-1">Total Ingested</div>
            <div className="font-medium">{status.total_ingested}</div>
          </div>
          <div className="bg-muted/50 rounded-lg p-3">
            <div className="text-muted-foreground text-xs mb-1">Last Check</div>
            <div className="font-medium text-xs">{formatTime(status.last_check_at)}</div>
          </div>
        </div>
      )}

      <div className="space-y-3 border-t border-border pt-4">
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="rounded border-border"
            />
            <span>Enable export scheduler</span>
          </label>
        </div>

        <div className="space-y-1">
          <label className="text-sm font-medium flex items-center gap-2">
            <User className="w-4 h-4" />
            Ingest Into Profile
          </label>
          <select
            value={exportProfileId}
            onChange={(e) => setExportProfileId(e.target.value)}
            className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background"
          >
            {profiles.map((profile) => (
              <option key={profile.profile_id} value={profile.profile_id}>
                {profile.name}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            Exports from the watch folder are indexed under this profile namespace.
          </p>
        </div>

        <div className="space-y-1">
          <label className="text-sm font-medium flex items-center gap-2">
            <FolderOpen className="w-4 h-4" />
            Watch Path
          </label>
          <input
            type="text"
            value={watchPath}
            onChange={(e) => setWatchPath(e.target.value)}
            placeholder="/path/to/exports"
            className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background"
          />
          <p className="text-xs text-muted-foreground">
            Folder to monitor for AI assistant export files (.json, .zip)
          </p>
        </div>

        <div className="space-y-1">
          <label className="text-sm font-medium flex items-center gap-2">
            <Clock className="w-4 h-4" />
            Check Interval
          </label>
          <div className="flex items-center gap-2">
            <input
              type="number"
              value={intervalSec}
              onChange={(e) => setIntervalSec(Math.max(300, parseInt(e.target.value) || 300))}
              min={300}
              step={60}
              className="w-24 px-3 py-2 text-sm border border-border rounded-lg bg-background"
            />
            <span className="text-sm text-muted-foreground">
              ({formatInterval(intervalSec)})
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            How often to check for new exports (minimum 5 minutes)
          </p>
        </div>

        <div className="flex items-center gap-2 pt-2">
          <button
            onClick={handleSave}
            disabled={isSaving}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary text-primary-foreground px-4 py-2 text-sm hover:bg-primary/90 disabled:opacity-50"
          >
            {isSaving ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Settings className="w-4 h-4" />
            )}
            Save Configuration
          </button>
        </div>
      </div>

      {message && (
        <div
          className={`flex items-center gap-2 text-sm p-3 rounded-lg ${
            message.type === "success"
              ? "bg-green-50 text-green-700 border border-green-200"
              : "bg-red-50 text-red-700 border border-red-200"
          }`}
        >
          {message.type === "success" ? (
            <CheckCircle2 className="w-4 h-4" />
          ) : (
            <AlertCircle className="w-4 h-4" />
          )}
          {message.text}
        </div>
      )}
    </div>
  );
}
