# Customizing

Enso's managed customization surface is **instructions** (`AGENTS.md`) and **skills** at
home and workspace scope. A provider CLI may also load your user-level instructions and
skills outside Enso; those remain yours. Everything else is wiring in `config.json`.

This document is written for both readers. If you are the operator, it says where to put
things. If you are an agent asked to "add a skill" or "update the workspace instructions",
it says exactly which file to touch.

## Instructions: `AGENTS.md`

Two Enso-managed levels, both read on every turn:

| File | What belongs in it |
| --- | --- |
| `~/.enso/AGENTS.md` | How to behave anywhere: tone, safety, the environment, the CLI |
| `~/.enso/workspaces/<name>/AGENTS.md` | What *this* workspace is for and its specific rules |

`CLAUDE.md` sits beside each as a symlink, so both CLI families read one document. Never
replace the symlink with a copy — they will drift.

Your provider CLI's user-level instructions are a third, user-managed scope. Examples are
`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/AGENTS.md`, and OpenCode's
`~/.config/opencode/AGENTS.md` (below `$XDG_CONFIG_HOME` when that is set); the relevant CLI
loads them by its own rules, and Enso never writes them.
[Workspaces](workspaces.md#how-they-reach-the-agent) owns the provider-by-provider discovery
details.

Every provider CLI finds the home-level file by walking up to the nearest Git root, which
is why `~/.enso` is initialised as an empty Git repository it never commits to. Codex,
Grok, Antigravity, and OpenCode read `AGENTS.md` directly; Claude Code reads the
`CLAUDE.md` link.

Neither level has to say which chat platform a turn came from. Enso states that itself, in
the origin block at the top of every chat prompt; see
[Concepts § Chat origin](concepts.md#chat-origin). Name a platform in your instructions when
you are teaching something genuinely platform-specific — a command, a formatting contract, a
field — not to describe the turn in hand.

### The home-level file

The home file keeps only guidance needed on every turn: voice, authorization and input
trust, workspace/file context, knowledge conventions, skill discovery, and reply delivery.
Read the relevant skill for procedures and use `enso --help` to discover commands.
Detailed CLI inventories and onboarding questionnaires do not belong in always-loaded context.

Edit it freely: setup preserves an existing file, and managed updates preserve your edits.
Only a copy matching its recorded bundled baseline can refresh automatically. Keep confirmed
shared preferences here; detailed background belongs in knowledge, referenced by path.
Workspace-specific purpose and rules belong in that workspace's instructions. Do not turn
one person's preference into a rule for everyone or put secrets in these files.

### The workspace file

A new workspace starts with:

```markdown
## Workspace

<!-- What is this workspace for? One or two sentences. -->
```

The required `default` workspace instead receives its operator purpose and a reminder to
preserve it. Add rules or references only when they help this workspace; do not repeat the
home instructions. `enso workspace audit` warns while an ordinary workspace's template is
untouched. Existing workspace instructions are never refreshed.

Keep facts and detailed procedures in their owning note, with a path telling the agent
when to read it. A workspace filing rule can override the shared default; see
[Knowledge](knowledge.md#where-new-notes-go).

## Knowledge formatting

Fresh installations receive an editable `$ENSO_HOME/shared/knowledge/Meta/Guide.md`.
It owns organization and writing conventions; the bundled `enso-knowledge` skill owns
CLI procedures. [Knowledge](knowledge.md#starter-collection) describes the starter and
how existing homes are preserved.

The skill's `references/formatting.md` is a fallback for homes without a guide.
Its `scripts/lint.py` checks mechanical style without rewriting notes. When a user changes
a convention, update the guide (or the existing fallback) and any affected checks together.
Do not relax a check simply to make a note pass. The viewer never runs user scripts;
metadata, identity, path safety, and link validation remain application contracts.

## Skills

A skill is a directory with a `SKILL.md`: frontmatter naming it and saying when to use it,
then the instructions.

```markdown
---
name: research
description: Research a topic across the sources this workspace tracks and write a brief into shared knowledge. Use when asked to look into, investigate, or summarise a subject.
---

# Research

Steps, commands, conventions, and what a good result looks like.
```

The directory name and the frontmatter `name` must match. The `description` is what the
agent sees when deciding whether to load it, so write it as *what it does and when to use
it*, not as a title. Long reference material goes in `references/` beside `SKILL.md`.

For new skills, follow the [Agent Skills specification](https://agentskills.io/specification):
names are 1–64 lowercase letters, digits, and hyphens, with no leading/trailing hyphen or
`--`; descriptions are 1–1024 characters. Keep instructions focused (preferably under 500
lines), with tested helpers in `scripts/` and reusable templates in `assets/` when useful.
The bundled `enso-skills` skill guides authoring, scope, validation, and safe extraction.

The frontmatter must be valid YAML, one `key: value` mapping, with no key answered twice.
A description reading `Lays it out: like this` is not, because the unquoted colon starts a
nested mapping; quote the whole value. Enso reads `name` and `description` and ignores any
other field, so a `SKILL.md` your CLI gives extra fields keeps working. `enso workspace
audit` and `enso doctor` report a frontmatter fault against the file, with its line and
column.

### Where to put one

| Put it in | When |
| --- | --- |
| `<workspace>/skills/` | It only makes sense in this workspace |
| `~/.enso/skills/` | Every workspace should have it |
| Your CLI's user directory, such as `~/.claude/skills/` | You want it outside Enso too, in your own terminal sessions |

Names must be unique across the workspace and enso scopes. A workspace cannot override a
enso-wide skill by reusing its name; `enso workspace audit` reports that as an error,
because the provider CLIs disagree about which copy would win.

Adding or editing a skill needs no Enso restart; the next turn can load the changed files
through the existing links. Run
`enso workspace audit` after adding one to confirm its `SKILL.md` is valid and its name
is free. See [Workspaces](workspaces.md).

For changes to bundled skills in the source repository, use the opt-in
[skill evaluation workflow](development.md#skill-evaluations) to compare the original and
revised package through installed provider CLIs. It checks synthetic task results and reports
tokens and tool calls for an explicit provider, model, and reasoning effort. Review correctness
before accepting reduced effort; add scenarios as the skill's responsibilities need them.

### The bundled skills

Enso installs these into `~/.enso/skills/`:

| Skill | Covers |
| --- | --- |
| `enso-config` | Configuration edits, agent/provider settings, diagnostics, and restart requirements |
| `enso-messages` | Sending messages and attachments across transports, with workspace and destination context |
| `enso-browser` | Playwright CLI sessions and profiles, human login, and authorized browser actions; see [Browser](browser.md) |
| `enso-heartbeat` | Finite future actions and temporary watches, script gates, event history, action receipts, and completion |
| `enso-jobs` | Scheduled agent/command jobs, stage jobs, gates, postrun checks and explicit wait/skip groups |
| `enso-knowledge` | Finding and maintaining Markdown notes (in shared knowledge by default), links, imports, and user-defined formatting |
| `enso-security` | Installation trust, provider permissions, credential handling, and untrusted input |
| `enso-skills` | Finding official optional skills and writing or refining a skill manually |
| `enso-slack` | Looking people and channels up, reading history, sending, tables and charts |
| `enso-tables` | Creating, registering, and querying structured data in `enso.db` |
| `enso-projects` | Creating, organizing, managing, and troubleshooting projects, tasks, and workflows |
| `enso-update` | Checking releases, requesting an authorized self-update, and inspecting recovery |
| `enso-workspace` | Inspecting workspaces, the layout, the audit, and where skills go |

Skills contain the decisions and procedures specific to their task, with conditional detail
in linked references. `enso init` and `enso setup` write a bundled skill or home-level
`AGENTS.md` only when missing. A workspace `AGENTS.md` is created from its minimal template
and never refreshed.
A bundled maintenance job is installed in `workspaces/default/jobs/` and stamped with
the default agent during setup or config apply; those commands
leave an existing job directory alone, including missing scripts.

Managed upgrades use `.bundles.json`, which records hashes when Enso seeds files. A bundled
home instruction, skill, job file, or Slack manifest refreshes only when its current bytes
still match that recorded baseline. Edited files and previously tracked deletions are
preserved; removing an entire tracked bundle keeps it removed during an upgrade. Existing
jobs keep their original agent triple, and changing a prompt, schedule, or `enabled` flag
preserves that edited `JOB.md`. Newly introduced files can be added inside a tracked bundle,
and entirely new bundle names are seeded.

When a release stops shipping a skill, job, or support file, the upgrade removes its old copy
only if it still matches the recorded baseline. Empty directories inside the retired bundle
are removed too. Edited or untracked files, nonempty directories, and symlinks are preserved.
The small baseline receipts remain so a deleted file stays deleted if a later release
reintroduces it; retired code and extra backup copies do not accumulate in the home.

Historical files without baseline receipts are user-owned: upgrades do not replace them or
restore missing scripts inside their existing bundle directories. Merge new guidance into
those files yourself when needed. This upgrade behavior is separate from setup and config
apply, which can seed a missing bundle again when explicitly run. See
[Jobs § Bundled jobs](jobs.md#bundled-jobs) and [Upgrading](install.md#upgrading).

The generic `enso` skill is retired. Configuration, messaging, credentials, and workspace
operations have focused owners above; `enso --help` remains the command-discovery entrypoint.
Untouched recorded copies of the retired skill are removed during managed upgrades;
customized copies remain yours. Existing customized instructions may still refer to that
skill: update those references when adopting the focused skills, preserving local rules.

### Official optional skills

The separate [geekforbrains/enso-skills](https://github.com/geekforbrains/enso-skills)
repository holds reviewed, opt-in skills. It is the only source the installer accepts:

```bash
enso skill list                     # installed Enso-wide skills; no network
enso skill list --available         # the official catalog, with installation status
enso skill show enso-linkedin       # metadata, requirements, files, and source commit
enso skill install enso-linkedin    # install into $ENSO_HOME/skills/enso-linkedin/
```

These commands do not need a working transport configuration or database. Local listing
covers the home only; use the workspace audit or viewer to inspect workspace and external
user scopes. See [CLI § Skills](cli.md#skills) for JSON results.

Each remote operation resolves the catalog's `main` branch to one Git commit and reads
from that commit throughout. Install downloads only the catalog's explicitly named regular
files, validates the skill, and publishes a complete directory. URLs, arbitrary repositories,
branches, symlinks, and install hooks are not accepted. Network errors leave no installed
partial skill. Requirements name bundled skills and must already be present; dependencies
are not installed automatically. Chrome, browser tools, accounts, and other prerequisites
still need their own setup.

An install never overwrites an existing path, including a manually written skill or a
symlink. `.enso-skill.json` inside the directory records the official source, commit, and
original file hashes; it is installation provenance, not a claim that later edits were
reviewed. The audit recognizes that receipt for the reserved `enso-` prefix. Keep it with
the skill, and do not fabricate receipts for manually created content.

Optional skills are independent of Enso's release and are not refreshed by application
updates. This initial CLI intentionally has no update, force, or remove operation. To
replace a copy, first review and preserve any local changes, then explicitly move the old
directory aside before installing again. Agents must not do that to bypass a collision
without the user's agreement. Manual skill creation remains fully supported in either
managed scope, using a non-reserved name.

Reviewed skills still run with the provider's access; this is source restriction, not a
sandbox. Inspect a skill before use, retain existing action approvals, and never publish
credentials, browser data, personal account configuration, or private company material in
the catalog. Site-specific skills reuse `enso-browser` instead of copying profile and login
handling. The catalog README owns its short contribution and validation workflow.

### The reserved prefix

Anything Enso installs says so in its name: `enso`, and everything beginning `enso-`,
is Enso's. That keeps Enso-installed content distinct from yours and stops a bundled name
from colliding with a CLI's built-in command. The same prefix is reserved for the jobs
Enso installs. Give your own skills and jobs other names; `enso workspace audit` warns
about a `enso-*` skill or job that Enso did not put there.

## What Enso will not add

No docs system, no notes feature, no policy engine, no template library. Those are all the
same thing wearing different hats: a place for text that an agent reads. You already have
one — a Markdown file in `shared/knowledge/`, and a skill that says when to read it.

Keeping that out of the core is what makes the workspace layout worth being strict about.

## Wiring

Everything that is not instructions or skills is configuration:

| What | Where |
| --- | --- |
| Which chat locations use which workspace | `bindings` in [`config.json`](configuration.md) |
| Which agent a workspace or job uses | `defaults`, workspace `workspace.json`, `JOB.md` |
| Provider permissions | [Provider-native controls](configuration.md#provider-permissions-and-installation-trust) and `providers.<name>.args` |
| Credentials for agents and jobs | [Encrypted secrets](configuration.md#secrets), supplied through the CLI or job declarations |
| Scheduled work | [`JOB.md`](jobs.md), its agent prompt or command, and optional gate/postrun scripts |
| Projects and their stages | `enso workflow init` and workspace [`PROJECT.md`](configuration.md#projects); acceptance checks in stage definitions, agent instructions in the bound job's `JOB.md` |
