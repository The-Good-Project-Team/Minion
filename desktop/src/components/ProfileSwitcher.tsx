import { useState, useEffect, useCallback } from "react";
import {
  User,
  Plus,
  Trash2,
  Edit2,
  Check,
  X,
  ChevronDown,
  Loader2,
} from "lucide-react";
import {
  fetchProfiles,
  fetchActiveProfile,
  setActiveProfile,
  createProfile,
  updateProfile,
  deleteProfile,
  type Profile,
} from "../lib/api";

export function ProfileSwitcher({
  apiReady = false,
  onProfileChange,
}: {
  apiReady?: boolean;
  onProfileChange?: (profile: Profile) => void;
}) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [activeProfile, setActiveProfileState] = useState<Profile | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);
  const [newProfileName, setNewProfileName] = useState("");
  const [editingProfile, setEditingProfile] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  const loadProfiles = useCallback(async (retryCount = 0) => {
    setIsLoading(true);
    try {
      const profilesRes = await fetchProfiles();
      setProfiles(profilesRes.profiles);

      let activeRes: Profile | null = null;
      try {
        activeRes = await fetchActiveProfile();
      } catch {
        const fallback =
          profilesRes.profiles.find((p) => p.is_default) ??
          profilesRes.profiles.find((p) => p.profile_id === "default") ??
          profilesRes.profiles[0];
        if (fallback) {
          await setActiveProfile({ profile_id: fallback.profile_id });
          activeRes = fallback;
        }
      }
      setActiveProfileState(activeRes);
      if (activeRes) {
        onProfileChange?.(activeRes);
      }
      setIsLoading(false);
    } catch (error) {
      console.error("Failed to load profiles:", error);
      if (retryCount < 5) {
        window.setTimeout(() => {
          void loadProfiles(retryCount + 1);
        }, 1000 * (retryCount + 1));
        return;
      }
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (apiReady) {
      void loadProfiles();
    }
  }, [apiReady, loadProfiles]);

  useEffect(() => {
    if (isOpen && !isLoading && profiles.length === 0 && apiReady) {
      void loadProfiles();
    }
  }, [isOpen, isLoading, profiles.length, apiReady, loadProfiles]);

  const displayProfile =
    activeProfile ??
    profiles.find((p) => p.is_default) ??
    profiles.find((p) => p.profile_id === "default") ??
    profiles[0] ??
    null;

  const handleSwitchProfile = async (profileId: string) => {
    try {
      await setActiveProfile({ profile_id: profileId });
      const updated = await fetchActiveProfile();
      setActiveProfileState(updated);
      setIsOpen(false);
      onProfileChange?.(updated);
    } catch (error) {
      console.error("Failed to switch profile:", error);
    }
  };

  const handleCreateProfile = async () => {
    if (!newProfileName.trim()) return;
    setIsCreating(true);
    try {
      const profileId = newProfileName.toLowerCase().replace(/\s+/g, "-") + "-" + Date.now();
      await createProfile({
        profile_id: profileId,
        name: newProfileName,
        kind: "custom",
      });
      setNewProfileName("");
      await loadProfiles();
    } catch (error) {
      console.error("Failed to create profile:", error);
    } finally {
      setIsCreating(false);
    }
  };

  const handleUpdateProfile = async (profileId: string) => {
    if (!editName.trim()) return;
    try {
      await updateProfile(profileId, { name: editName });
      setEditingProfile(null);
      setEditName("");
      await loadProfiles();
    } catch (error) {
      console.error("Failed to update profile:", error);
    }
  };

  const handleDeleteProfile = async (profileId: string) => {
    if (!confirm("Are you sure you want to delete this profile? All associated data will be removed.")) {
      return;
    }
    try {
      await deleteProfile(profileId);
      await loadProfiles();
    } catch (error) {
      console.error("Failed to delete profile:", error);
      alert(error instanceof Error ? error.message : "Failed to delete profile");
    }
  };

  const startEditing = (profile: Profile) => {
    setEditingProfile(profile.profile_id);
    setEditName(profile.name);
  };

  const cancelEditing = () => {
    setEditingProfile(null);
    setEditName("");
  };

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-muted px-3 py-2">
        <Loader2 className="size-4 animate-spin" />
        <span className="text-sm text-muted-foreground">Loading profiles...</span>
      </div>
    );
  }

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 transition-colors hover:bg-accent"
      >
        <User className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">
          {displayProfile?.name || "Default"}
        </span>
        <ChevronDown className="size-4 text-muted-foreground" />
      </button>

      {isOpen && (
        <>
          <div
            className="fixed inset-0 z-10"
            onClick={() => setIsOpen(false)}
          />
          <div className="absolute right-0 top-full z-20 mt-2 w-72 rounded-lg border border-border bg-card shadow-lg">
            <div className="border-b border-border p-3">
              <h3 className="text-sm font-semibold">Profiles</h3>
            </div>

            <div className="max-h-64 overflow-y-auto">
              {profiles.length === 0 ? (
                <p className="px-3 py-4 text-sm text-muted-foreground">No profiles loaded yet.</p>
              ) : (
                profiles.map((profile) => (
                  <div
                    key={profile.profile_id}
                    className={`flex items-center justify-between px-3 py-2 hover:bg-accent ${
                      activeProfile?.profile_id === profile.profile_id
                        ? "bg-accent/70"
                        : ""
                    }`}
                  >
                    {editingProfile === profile.profile_id ? (
                      <div className="flex flex-1 items-center gap-2">
                        <input
                          type="text"
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          className="flex-1 rounded border px-2 py-1 text-sm"
                          onKeyDown={(e) => {
                            if (e.key === "Enter") handleUpdateProfile(profile.profile_id);
                            if (e.key === "Escape") cancelEditing();
                          }}
                          autoFocus
                        />
                        <button
                          onClick={() => handleUpdateProfile(profile.profile_id)}
                          className="rounded p-1 text-green-600 hover:bg-green-100"
                        >
                          <Check className="size-4" />
                        </button>
                        <button
                          onClick={cancelEditing}
                          className="rounded p-1 text-red-600 hover:bg-red-100"
                        >
                          <X className="size-4" />
                        </button>
                      </div>
                    ) : (
                      <>
                        <button
                          onClick={() => handleSwitchProfile(profile.profile_id)}
                          className="flex flex-1 items-center gap-2 text-left"
                        >
                          <span className="text-sm font-medium">
                            {profile.name}
                          </span>
                          {profile.is_default && (
                            <span className="text-xs text-muted-foreground">(default)</span>
                          )}
                          {activeProfile?.profile_id === profile.profile_id && (
                            <Check className="size-4 text-primary" />
                          )}
                        </button>
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => startEditing(profile)}
                            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                            title="Rename"
                          >
                            <Edit2 className="size-3" />
                          </button>
                          {!profile.is_default && (
                            <button
                              onClick={() => handleDeleteProfile(profile.profile_id)}
                              className="rounded p-1 text-muted-foreground hover:bg-red-50 hover:text-red-600"
                              title="Delete"
                            >
                              <Trash2 className="size-3" />
                            </button>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                ))
              )}
            </div>

            <div className="border-t border-border p-3">
              {isCreating ? (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={newProfileName}
                    onChange={(e) => setNewProfileName(e.target.value)}
                    placeholder="Profile name..."
                    className="flex-1 rounded border px-2 py-1 text-sm"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleCreateProfile();
                      if (e.key === "Escape") {
                        setIsCreating(false);
                        setNewProfileName("");
                      }
                    }}
                    autoFocus
                  />
                  <button
                    onClick={handleCreateProfile}
                    className="rounded p-1 text-green-600 hover:bg-green-100"
                  >
                    <Check className="size-4" />
                  </button>
                  <button
                    onClick={() => {
                      setIsCreating(false);
                      setNewProfileName("");
                    }}
                    className="rounded p-1 text-red-600 hover:bg-red-100"
                  >
                    <X className="size-4" />
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setIsCreating(true)}
                  className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent"
                >
                  <Plus className="size-4" />
                  <span>New Profile</span>
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
