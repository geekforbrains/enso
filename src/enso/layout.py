"""What belongs in an Enso home, and who owns it.

One table per root — the home and a workspace — naming every top-level entry Enso itself
writes or expects, the provider files it preserves without reading, and who owns each one.
Setup's preflight, the scaffolding in :mod:`enso.workspaces`, and the audit all read these
tables, so a new directory is declared once instead of in three independent allowlists.
``SHARED`` names what the home's ``shared/`` holds; only the audit reads it, and the
scaffold creates ``shared/knowledge/`` directly.

The tables describe top-level entries only. Nothing here invites a recursive scan: a
core-managed root such as ``runtime/`` or ``cache/`` is private operating state, and what
is inside it is Enso's business, never a layout finding. The same holds for a user root
such as ``shared/knowledge/``, whose contents belong to the operator.
"""

from __future__ import annotations

from dataclasses import dataclass

# Categories, most owned first. ``unexpected`` is a classification result, not a table entry.
REQUIRED = "required"  # core-managed, and the audit reports an error when it is missing
MANAGED = "managed"  # core-managed, written on demand; absence is normal
USER = "user"  # Enso may create the root; what is inside it is the operator's
EXTENSION = "extension"  # a provider or tool's own file, preserved and never read
UNEXPECTED = "unexpected"  # nothing in the table claims this name

CATEGORIES = (REQUIRED, MANAGED, USER, EXTENSION, UNEXPECTED)
# OS noise: reported by nothing, whatever root it turns up in.
IGNORED = frozenset({".DS_Store"})
# What a ``private`` entry is created with; repairs preserve its owner bits.
PRIVATE_DIR = 0o700
SHARED_BITS = 0o077  # any group or other access at all


@dataclass(frozen=True)
class Entry:
    """One top-level name a root may hold."""

    name: str
    category: str
    what: str  # a short phrase, shown when a finding has to name the entry
    private: bool = False  # Enso owns its permissions: no group or other access
    real_directory: bool = False  # when present, a file or symlink cannot replace this root

    @property
    def required(self) -> bool:
        return self.category == REQUIRED


# The documented workspace layout (docs/workspaces.md § Layout). Link targets are relative
# to the link's own directory; the provider CLIs find the skill links by walking up from
# the workspace to the Git root.
WORKSPACE_DIRS = ("skills", "knowledge", "memory", "jobs", "projects", "drafts", "uploads")
LINKS = (
    ("CLAUDE.md", "AGENTS.md"),
    (".claude/skills", "../skills"),
    (".agents/skills", "../skills"),
)

WORKSPACE: tuple[Entry, ...] = (
    Entry("AGENTS.md", REQUIRED, "the workspace instructions"),
    Entry("CLAUDE.md", REQUIRED, "a symlink to AGENTS.md"),
    Entry(".claude", REQUIRED, "the Claude Code and Grok skill link"),
    Entry(".agents", REQUIRED, "the Codex, Antigravity, and OpenCode skill link"),
    Entry("skills", REQUIRED, "skills only this workspace needs"),
    Entry("knowledge", USER, "optional workspace reference material"),
    Entry("memory", REQUIRED, "dated Markdown memories"),
    Entry("jobs", REQUIRED, "scheduled and stage jobs"),
    Entry("projects", REQUIRED, "project definitions and scripts"),
    Entry("drafts", USER, "optional work product using the original folder name"),
    Entry("work", USER, "task files and retained work product"),
    Entry("uploads", REQUIRED, "chat attachments, one directory per turn"),
    Entry("WORKSPACE.md", USER, "optional agent triple and provider arguments"),
    Entry("heartbeat", USER, "optional gate scripts and their helpers"),
    Entry(".codex", EXTENSION, "Codex's own policy file"),
    Entry(".grok", EXTENSION, "Grok's own policy file"),
    Entry("opencode.json", EXTENSION, "OpenCode's own policy file"),
)

HOME: tuple[Entry, ...] = (
    Entry("AGENTS.md", REQUIRED, "the home instructions"),
    Entry("CLAUDE.md", REQUIRED, "a symlink to AGENTS.md"),
    Entry(".claude", REQUIRED, "the Claude Code and Grok skill link"),
    Entry(".agents", REQUIRED, "the Codex, Antigravity, and OpenCode skill link"),
    Entry(".git", REQUIRED, "the Git root the provider CLIs stop their walk at"),
    Entry("skills", REQUIRED, "the installed and hand-written skills"),
    Entry("shared", REQUIRED, "what every workspace shares"),
    Entry("workspaces", REQUIRED, "one directory per workspace"),
    Entry("config.json", MANAGED, "the configuration", private=True),
    Entry("config.example.json", MANAGED, "the editable configuration template"),
    Entry("enso.db", MANAGED, "captures, runs, tasks, and beats"),
    Entry("enso.db-wal", MANAGED, "SQLite's write-ahead log"),
    Entry("enso.db-shm", MANAGED, "SQLite's shared-memory index"),
    Entry("enso.log", MANAGED, "the service log"),
    Entry("launchd.log", MANAGED, "the service manager's own log"),
    Entry("launchd-web.log", MANAGED, "the viewer service manager's own log"),
    Entry("web.log", MANAGED, "the viewer log"),
    Entry("web.pid", MANAGED, "the viewer's pidfile"),
    Entry("cache", MANAGED, "expiring lookups and private pairing state", real_directory=True),
    Entry(
        "runtime",
        MANAGED,
        "managed releases, update receipts, and lock files",
        private=True,
        real_directory=True,
    ),
    Entry("browser", MANAGED, "the enso-browser skill's profiles"),
    # Older homes may keep an edited copy after the packaged manifest stops being seeded.
    Entry("slack", USER, "legacy Slack app files"),
    Entry(".bundles.json", MANAGED, "the record of what Enso installed"),
    Entry(".migrations.json", MANAGED, "the last completed home migration", private=True),
    Entry(
        "secrets",
        USER,
        "*.env files loaded into the service environment",
        private=True,
        real_directory=True,
    ),
    # Enso runs ``git init`` here and never commits, but the repository it made is the
    # operator's to use, and a repository's ignore file belongs beside it.
    Entry(".gitignore", USER, "what the operator keeps out of the home's own history"),
)

# What ``shared/`` holds; later sharing features add their entries here.
SHARED: tuple[Entry, ...] = (
    Entry("knowledge", REQUIRED, "shared reference that belongs across workspaces"),
)


def classify(table: tuple[Entry, ...], name: str) -> Entry | None:
    """The entry ``name`` belongs to in ``table``, or None when nothing claims it."""
    return next((entry for entry in table if entry.name == name), None)


def private(table: tuple[Entry, ...]) -> tuple[Entry, ...]:
    """The entries whose permissions Enso owns: credentials and private operating state,
    readable by their owner and by nobody else."""
    return tuple(entry for entry in table if entry.private)
