# Profile System Design

## Overview

The profile system allows users to separate their Minion vault into distinct namespaces (e.g., Work vs Personal) with different consent scopes per profile. This enables:

- **Data isolation**: Work and personal data kept separate
- **Different consent policies**: Work profile may allow broader MCP access than personal
- **Profile switching**: Easy switching between contexts
- **Default profile**: One profile marked as default for initial use

## Data Model Changes

### New Table: profiles

```sql
CREATE TABLE IF NOT EXISTS profiles (
    profile_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,  -- 'work', 'personal', 'custom'
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
```

### Schema Migration

Add `profile_id` to existing tables:

```sql
-- Add profile_id to sources
ALTER TABLE sources ADD COLUMN profile_id TEXT REFERENCES profiles(profile_id);
CREATE INDEX IF NOT EXISTS idx_sources_profile ON sources(profile_id);

-- Add profile_id to chunks
ALTER TABLE chunks ADD COLUMN profile_id TEXT REFERENCES profiles(profile_id);
CREATE INDEX IF NOT EXISTS idx_chunks_profile ON chunks(profile_id);
```

### Active Profile Tracking

Store current active profile in meta table:

```sql
INSERT OR REPLACE INTO meta (key, value) VALUES ('active_profile_id', 'profile-id');
```

## Consent Policy Extension

Extend `consent_policy.json` to support per-profile reader scopes:

```json
{
  "schema_version": 2,
  "profiles": {
    "default": {
      "readers": {
        "mcp": {
          "allowed_strata": ["summaries", "graph_facts", "work_context", "preferences", "projections"],
          "max_release_level": 3
        }
      }
    },
    "work": {
      "readers": {
        "mcp": {
          "allowed_strata": ["summaries", "graph_facts", "work_context", "preferences", "projections"],
          "max_release_level": 4  // Higher release level for work
        }
      }
    },
    "personal": {
      "readers": {
        "mcp": {
          "allowed_strata": ["graph_facts", "preferences", "projections"],  // No work_context
          "max_release_level": 2  // Lower release level for personal
        }
      }
    }
  }
}
```

## API Endpoints

### Profile Management

```bash
# List all profiles
GET /profiles

# Create a new profile
POST /profiles
{
  "name": "Work",
  "kind": "work"
}

# Update a profile
PUT /profiles/{profile_id}
{
  "name": "Work Projects",
  "is_default": true
}

# Delete a profile (and associated data)
DELETE /profiles/{profile_id}

# Get active profile
GET /profiles/active

# Switch active profile
PUT /profiles/active
{
  "profile_id": "work-profile-id"
}
```

### Profile-Specific Consent

```bash
# Get consent policy for active profile
GET /settings/consent?profile_id={profile_id}

# Update consent policy for a profile
PUT /settings/consent
{
  "profile_id": "work",
  "readers": {
    "mcp": {
      "allowed_strata": [...],
      "max_release_level": 4
    }
  }
}
```

## Behavior Changes

### Ingestion

- When ingesting sources, associate with the currently active profile
- If no profile is active, use the default profile
- Profile association stored in `sources.profile_id` and propagated to `chunks.profile_id`

### Search

- Default search returns results from active profile only
- Add optional `profile_id` parameter to search endpoints for cross-profile queries
- Local UI (reader_id="local_ui") can search all profiles with explicit opt-in

### MCP Tools

- `ask_minion` and other MCP tools respect the active profile's consent policy
- Profile-specific consent strata and release levels applied
- No cross-profile data leakage via MCP

### Export

- Export bundles respect active profile by default
- Add option to export specific profiles or all profiles

## Migration Strategy

1. **Schema migration**: Add profiles table and profile_id columns
2. **Default profile**: Create a "default" profile and migrate existing data to it
3. **Consent policy**: Extend consent_policy.json schema with profiles section
4. **Backward compatibility**: If no profile is specified, use "default" profile
5. **UI update**: Add profile switcher to desktop app

## Default Profiles

On first run, create two default profiles:

1. **Default**: General purpose, inherits from existing consent policy
2. **Personal**: Stricter MCP consent (no work_context, lower release level)

Users can create additional profiles (e.g., "Work", "Side Project") as needed.

## Implementation Order

1. Schema migration (profiles table, profile_id columns)
2. Profile management API endpoints
3. Consent policy extension for per-profile settings
4. Active profile tracking and switching
5. Ingestion profile association
6. Search profile filtering
7. MCP profile isolation
8. UI profile switcher
9. Tests for profile system
10. Documentation

## Testing Considerations

- Test profile isolation: data from one profile shouldn't leak to another
- Test consent policy per-profile: different profiles should have different MCP access
- Test profile switching: switching should update active profile immediately
- Test backward compatibility: existing data should migrate to default profile
- Test deletion: deleting a profile should cascade delete associated data

## Future Enhancements

- Profile templates (pre-configured consent policies for common use cases)
- Profile sharing (export/import profile configurations)
- Profile-specific data directories (separate physical storage)
- Profile encryption (per-profile encryption keys)
- Profile sync (sync specific profiles across devices)
