"""Pre-generated life graph scaffold — node types, relations, and seed tree."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Canonical node types (do not start from an empty untyped store).
NODE_TYPES: tuple[str, ...] = (
    "person",
    "household",
    "family",
    "place",
    "organization",
    "group",
    "project",
    "role",
    "job",
    "hobby",
    "asset",
    "document",
    "event",
    "task",
    "decision",
    "conversation",
    "preference",
    "obligation",
    "memory",
    "scaffold",  # folder buckets in the stub tree
)

RELATION_TYPES: tuple[str, ...] = (
    "knows",
    "related_to",
    "married_to",
    "parent_of",
    "child_of",
    "lives_at",
    "works_at",
    "belongs_to",
    "owns",
    "manages",
    "participates_in",
    "responsible_for",
    "mentioned_in",
    "decided",
    "prefers",
    "owes",
    "waiting_on",
    "scheduled_for",
)

# (node_id, parent_id, node_type, title, hint)
ScaffoldSpec = Tuple[str, str | None, str, str, str]

LIFE_GRAPH_SCAFFOLD: tuple[ScaffoldSpec, ...] = (
    ("scaffold-me", None, "person", "Me", "You"),
    ("scaffold-people", "scaffold-me", "scaffold", "People", "People you know"),
    ("scaffold-people-family", "scaffold-people", "family", "Family", "Family members"),
    ("scaffold-people-friends", "scaffold-people", "person", "Friends", "Friends"),
    ("scaffold-people-work", "scaffold-people", "person", "Work", "Work contacts"),
    ("scaffold-people-unknown", "scaffold-people", "person", "Unknown People", "Unresolved contacts"),
    ("scaffold-places", "scaffold-me", "scaffold", "Places", "Where you live and go"),
    ("scaffold-places-home", "scaffold-places", "place", "Home", "Primary residence"),
    ("scaffold-places-work", "scaffold-places", "place", "Workplaces", "Offices and sites"),
    ("scaffold-places-frequent", "scaffold-places", "place", "Frequent Places", "Regular spots"),
    ("scaffold-groups", "scaffold-me", "scaffold", "Groups", "Teams and communities"),
    ("scaffold-groups-family", "scaffold-groups", "family", "Family", "Family groups"),
    ("scaffold-groups-church", "scaffold-groups", "group", "Church", "Faith community"),
    ("scaffold-groups-teams", "scaffold-groups", "group", "Teams", "Teams you are on"),
    ("scaffold-groups-companies", "scaffold-groups", "organization", "Companies", "Employers and orgs"),
    ("scaffold-projects", "scaffold-me", "scaffold", "Projects", "Things you work on"),
    ("scaffold-projects-active", "scaffold-projects", "project", "Active", "In flight"),
    ("scaffold-projects-paused", "scaffold-projects", "project", "Paused", "On hold"),
    ("scaffold-projects-archived", "scaffold-projects", "project", "Archived", "Done or shelved"),
    ("scaffold-work", "scaffold-me", "scaffold", "Work", "Roles and obligations"),
    ("scaffold-work-roles", "scaffold-work", "role", "Roles", "Named roles"),
    ("scaffold-work-companies", "scaffold-work", "organization", "Companies", "Where you work"),
    ("scaffold-work-obligations", "scaffold-work", "obligation", "Obligations", "Commitments"),
    ("scaffold-hobbies", "scaffold-me", "hobby", "Hobbies", "Interests"),
    ("scaffold-assets", "scaffold-me", "asset", "Assets", "Things you own"),
    ("scaffold-tasks", "scaffold-me", "task", "Tasks", "Action items"),
    ("scaffold-decisions", "scaffold-me", "decision", "Decisions", "Recorded choices"),
    ("scaffold-preferences", "scaffold-me", "preference", "Preferences", "Stable prefs"),
)

WIKI_PAGE_TYPE_TO_NODE: Dict[str, str] = {
    "person": "person",
    "place": "place",
    "family": "family",
    "household": "household",
    "group": "group",
    "team": "group",
    "organization": "organization",
    "company": "organization",
    "project": "project",
    "role": "role",
    "job": "job",
    "hobby": "hobby",
    "asset": "asset",
    "thing": "asset",
    "object": "asset",
    "document": "document",
    "event": "event",
    "task": "task",
    "decision": "decision",
    "conversation": "conversation",
    "preference": "preference",
    "obligation": "obligation",
    "memory": "memory",
}


def scaffold_as_tree(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build nested children from flat scaffold rows (parent_node_id links)."""
    order = {n["node_id"]: i for i, n in enumerate(nodes)}
    by_id = {n["node_id"]: {**n, "children": []} for n in nodes}
    roots: List[Dict[str, Any]] = []
    for n in nodes:
        pid = n.get("parent_node_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(by_id[n["node_id"]])
        elif not pid:
            roots.append(by_id[n["node_id"]])

    def _sort_branch(node: Dict[str, Any]) -> None:
        node["children"].sort(key=lambda c: order.get(c["node_id"], 0))
        for ch in node["children"]:
            _sort_branch(ch)

    for r in roots:
        _sort_branch(r)
    return roots
