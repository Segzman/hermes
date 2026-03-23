"""
Persistent memory system for Hermes.

Memories are stored as markdown files in ~/.hermes/memory/ with lightweight
frontmatter so the agent can search, filter, and inject the most useful context
back into its prompt.

Architecture notes:
  - Each memory is a separate .md file with YAML-like frontmatter containing
    metadata (name, description, type, tags, saved_at).
  - The MEMORY.md index file is auto-rebuilt after every save/delete and
    provides a quick overview of all stored memories.
  - Memory types (user, feedback, routine, contact, project, note) allow
    the agent to categorise information and filter during recall.
  - The scoring system in recall() weights exact name matches highest,
    followed by partial name matches, description matches, tag matches,
    and body matches. This ensures the most relevant memories surface first.
  - get_index_for_prompt() is called during system prompt construction to
    inject a compact summary of all memories, prioritised by type (user
    preferences first, notes last).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

# All memory files are stored in this directory
MEMORY_DIR = Path(os.path.expanduser("~/.hermes/memory"))
# Auto-generated index file listing all memories
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def _slugify(text: str) -> str:
    """
    Convert text to a filesystem-safe slug for use in filenames.

    Replaces non-alphanumeric characters with underscores and truncates
    to 50 characters.
    """
    s = re.sub(r"[^a-z0-9_-]", "_", text.lower().strip())
    return s[:50]


def _normalize_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    """
    Clean and normalise a list of tags for storage.

    Tags are lowercased, spaces are converted to hyphens, special characters
    are removed, duplicates are eliminated, and the list is capped at 12 tags.
    """
    cleaned = []
    for tag in tags or []:
        value = re.sub(r"\s+", "-", (tag or "").strip().lower())
        value = re.sub(r"[^a-z0-9_.-]", "", value)
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned[:12]


def _parse_memory_file(path: Path) -> dict:
    """
    Parse a memory markdown file into a dict.

    The file format uses YAML-like frontmatter between --- delimiters:
        ---
        name: my_preference
        description: User prefers dark mode
        type: user
        tags: ui, preference
        saved_at: 2026-01-15T10:30:00+00:00
        ---

        The actual memory content goes here.

    Returns a dict with keys: path, filename, name, description, type,
    saved_at (datetime), tags (list), body (str).
    """
    raw = path.read_text()
    # Split on --- delimiters to extract frontmatter
    parts = raw.split("---", 2)
    frontmatter = parts[1] if len(parts) >= 3 else ""
    body = parts[2].strip() if len(parts) >= 3 else raw.strip()

    def _field(name: str, default: str = "") -> str:
        """Extract a single field value from the frontmatter text."""
        match = re.search(rf"^{re.escape(name)}:\s*(.+)$", frontmatter, re.MULTILINE)
        return match.group(1).strip() if match else default

    tags_raw = _field("tags", "")
    tags = [tag.strip() for tag in tags_raw.split(",") if tag.strip()]
    saved_at = _field("saved_at", "")
    # Parse saved_at as ISO datetime, falling back to file modification time
    try:
        saved_dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00")) if saved_at else datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except ValueError:
        saved_dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

    return {
        "path": path,
        "filename": path.name,
        "name": _field("name", path.stem),
        "description": _field("description", ""),
        "type": _field("type", "note"),
        "saved_at": saved_dt,
        "tags": tags,
        "body": body,
    }


def _load_memories() -> list[dict]:
    """
    Load all memory files from MEMORY_DIR.

    Skips the MEMORY.md index file and silently ignores malformed files.
    """
    if not MEMORY_DIR.exists():
        return []
    items = []
    for path in MEMORY_DIR.glob("*.md"):
        if path.name == "MEMORY.md":
            continue
        try:
            items.append(_parse_memory_file(path))
        except Exception:
            continue
    return items


def save(
    name: str,
    content: str,
    memory_type: str = "note",
    description: str = "",
    tags: list[str] | tuple[str, ...] | None = None,
) -> str:
    """
    Save a new memory or overwrite an existing one with the same name/type.

    The filename is derived from the type and slugified name, so saving
    with the same name and type will update the existing memory.

    Returns a confirmation string.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(name)
    # Filename includes type prefix to avoid collisions across types
    filename = f"{memory_type}_{slug}.md"
    filepath = MEMORY_DIR / filename
    # Use the first 120 chars of content as description if none provided
    desc = (description or content[:120].replace("\n", " ")).strip()
    saved_at = datetime.now(tz=timezone.utc).isoformat()
    clean_tags = _normalize_tags(tags)

    text = (
        f"---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        f"type: {memory_type}\n"
        f"tags: {', '.join(clean_tags)}\n"
        f"saved_at: {saved_at}\n"
        f"---\n\n"
        f"{content.strip()}\n"
    )
    filepath.write_text(text)
    _rebuild_index()
    tag_part = f" tags={', '.join(clean_tags)}" if clean_tags else ""
    return f'Memory saved: "{name}"{tag_part}'


def _rebuild_index() -> None:
    """
    Regenerate the MEMORY.md index file.

    Called after every save/delete to keep the index in sync.
    The index is a markdown list with links to individual memory files.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Memory Index", ""]
    entries = sorted(
        _load_memories(),
        key=lambda item: (item["type"], item["name"].lower(), item["saved_at"]),
    )
    for item in entries:
        tag_text = f" [tags: {', '.join(item['tags'])}]" if item["tags"] else ""
        lines.append(
            f"- [{item['name']}]({item['filename']}): [{item['type']}] {item['description']}{tag_text}"
        )
    MEMORY_INDEX.write_text("\n".join(lines) + "\n")


def _score_memory(item: dict, query: str) -> int:
    """
    Score a memory item against a search query for ranking.

    Scoring weights (higher = more relevant):
      120 - Exact name match
       70 - Partial name match (query is substring of name)
       35 - Query found in description
       25 - Query found in any tag
       10 - Query found in body text
        1 - Default score when query is empty (show everything)
    """
    q = query.lower().strip()
    haystack_name = item["name"].lower()
    haystack_desc = item["description"].lower()
    haystack_body = item["body"].lower()
    haystack_tags = [tag.lower() for tag in item["tags"]]
    score = 0
    if not q:
        return 1  # Empty query: include everything with minimal score
    if q == haystack_name:
        score += 120
    elif q in haystack_name:
        score += 70
    if q in haystack_desc:
        score += 35
    if any(q in tag for tag in haystack_tags):
        score += 25
    if q in haystack_body:
        score += 10
    return score


def recall(query: str, memory_type: str = "", limit: int = 5) -> str:
    """
    Search memories by keyword query, optionally filtered by type.

    Returns a formatted text listing of matching memories, ranked by
    relevance score (highest first, then newest first for ties).
    """
    items = _load_memories()
    if not items:
        return "No memories stored yet."
    type_filter = (memory_type or "").strip().lower()
    if type_filter:
        items = [item for item in items if item["type"].lower() == type_filter]
    scored = [
        (item, _score_memory(item, query))
        for item in items
    ]
    # Only include items with a positive score
    scored = [pair for pair in scored if pair[1] > 0]
    # Sort by score descending, then by recency descending
    scored.sort(key=lambda pair: (-pair[1], -pair[0]["saved_at"].timestamp()))
    if not scored:
        suffix = f' in type "{memory_type}"' if memory_type else ""
        return f'No memories found matching "{query}"{suffix}.'

    lines = [f"Found {min(len(scored), limit)} memory/memories:\n"]
    for item, _ in scored[: max(1, limit)]:
        tags = f"\nTags: {', '.join(item['tags'])}" if item["tags"] else ""
        desc = f"{item['description']}\n" if item["description"] else ""
        lines.append(
            f"**{item['name']}** [{item['type']}]\n"
            f"{desc}{item['body'][:400]}{tags}"
        )
    return "\n\n---\n\n".join(lines)


def list_all(memory_type: str = "", limit: int = 30) -> str:
    """
    List all stored memories, optionally filtered by type.

    Returns a compact bulleted list sorted by recency (newest first).
    """
    items = _load_memories()
    if not items:
        return "No memories stored yet."
    type_filter = (memory_type or "").strip().lower()
    if type_filter:
        items = [item for item in items if item["type"].lower() == type_filter]
    if not items:
        return f'No memories stored for type "{memory_type}".'

    items.sort(key=lambda item: item["saved_at"], reverse=True)
    lines = []
    for item in items[: max(1, limit)]:
        tags = f" [tags: {', '.join(item['tags'])}]" if item["tags"] else ""
        saved = item["saved_at"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"• [{item['type']}] {item['name']} ({saved}): {item['description']}{tags}")
    return "Stored memories:\n" + "\n".join(lines)


def delete(name: str) -> str:
    """
    Delete a memory by name (partial match supported).

    Raises an error message if multiple memories match to prevent
    accidental deletion of the wrong item.
    """
    items = _load_memories()
    if not items:
        return "No memories stored."
    needle = (name or "").strip().lower()
    matches = [item for item in items if needle in item["name"].lower()]
    if not matches:
        return f'No memory found matching "{name}".'
    if len(matches) > 1:
        names = ", ".join(item["name"] for item in matches[:5])
        return f'Multiple memories matched "{name}": {names}'
    matches[0]["path"].unlink()
    _rebuild_index()
    return f'Deleted memory: "{matches[0]["name"]}"'


def get_index_for_prompt(limit_chars: int = 3000) -> str:
    """
    Build a compact memory summary for injection into the system prompt.

    Memories are prioritised by type (user preferences first, then
    feedback, routine, project, contact, and notes last) so the most
    actionable context appears first within the character limit.

    Limited to 25 items and 3000 characters to avoid bloating the prompt.
    """
    items = _load_memories()
    if not items:
        return ""
    # Priority ordering: user preferences are most important for the LLM
    priority = {"user": 0, "feedback": 1, "routine": 2, "project": 3, "contact": 4, "note": 5}
    items.sort(
        key=lambda item: (
            priority.get(item["type"], 9),
            -item["saved_at"].timestamp(),  # Within each type, newest first
        )
    )
    lines = []
    for item in items[:25]:
        tags = f" [tags: {', '.join(item['tags'])}]" if item["tags"] else ""
        line = f"- [{item['type']}] {item['name']}: {item['description']}{tags}"
        lines.append(line)
    return "\n".join(lines)[:limit_chars]
