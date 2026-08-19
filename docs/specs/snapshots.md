# Scoped local snapshots

Enso keeps local Git history for a narrow set of human-authored content under
`~/.enso`. The public snapshot command records one coherent content change without
capturing credentials, runtime state, or unrelated work:

```bash
enso snapshot create --message "<summary>" -- <paths...>
```

This is a local content journal, not a configuration backup or a general Git
frontend. Enso exposes no snapshot history, restore, reset, or delete commands.

## CLI contract

`--message` and at least one path after `--` are required. Each path is explicit:
there is no implicit current directory and no equivalent of `git add -A`.

Relative paths resolve from the shell's current working directory. Absolute paths
are also accepted, so the command works both inside and outside `~/.enso`:

```bash
cd ~/.enso/workspaces/company
enso snapshot create --message "docs: update onboarding" -- \
  AGENTS.md knowledge/onboarding.md

enso snapshot create --message "docs: update network reference" -- \
  ~/.enso/docs/network.md
```

A directory is an explicit recursive scope. Literal path arguments preserve spaces,
and deleted files inside a requested scope are recorded. Shell expansion still occurs
before Enso receives an argument, so callers should pass only the exact paths they have
reviewed rather than globs.

The command prints one of these outcomes:

- `Snapshot created: <message>` and exit status `0` when it created a commit.
- `No changes to snapshot.` and exit status `0` when the requested paths have no diff.
- `Could not create Enso snapshot: <diagnostic>` and exit status `1` for a safety or
  repository failure.

Unrelated unstaged changes remain untouched. Pre-existing staged changes are not
unrelated work that Enso can safely distinguish, so they block the command and remain
staged.

## Path boundary

Every requested path is checked before snapshot assembly. Enso rejects the repository root
itself, lexical traversal components such as `..`, paths outside `~/.enso`, and paths
whose resolved symlink target escapes it. It then applies a closed allowlist; an unknown
path is unsupported rather than assumed safe.

| Disposition | Content |
| --- | --- |
| Versionable | Root `.gitignore`, `AGENTS.md`, and `CLAUDE.md`; root skill-discovery links; global `docs/` and canonical `skills/`; workspace root instructions and discovery links; workspace `knowledge/` and canonical `skills/`; and `JOB.md`, `prerun.sh`, or `prerun.py` directly below one job directory |
| Protected | Configuration and lock files, including `.snapshot.lock`; snapshot transaction state (the root `.snapshot.transaction.json` marker, root `.snapshot-transaction-<32-lowercase-hex>.tmp` crash-safe marker-write files, and `.snapshot-index-<32-lowercase-hex>` alternate indexes inside the resolved Git directory); credentials and authentication files; databases and sidecars; all other Git metadata; native policy homes; state, messages, audits, runs, caches, logs, uploads, drafts, updater state, job locks, generated output, and temporary job data |
| Unsupported | Every path not matched by the versionable allowlist, even when it happens to live below `~/.enso` |

Protection wins inside a normally versionable tree. For example, an `.env`, database,
authentication file, log, or directory named `secrets`, `uploads`, or `drafts` below a
doc, skill, or knowledge directory is still rejected. Protective `.gitignore` rules
provide defense in depth; they do not make an already tracked protected file safe.
Enso blocks snapshots until the operator repairs any such tracked path deliberately.

An entry admitted by the path allowlist must still be a regular file or symlink owned by
the current user with no additional hard links. Enso preserves regular-file executable
mode and symlink target bytes, but rejects hard links, special files, symlink escapes, and
a nested `.git` entry
inside a requested directory. An explicitly requested untracked path ignored by Git is
an error; ignored untracked descendants of a requested directory are skipped. Previously
tracked content remains subject to the protected-path audit rather than becoming safe
because an ignore rule now matches it.

## Repository transaction

The command requires Git 2.28 or newer and an existing, valid Git worktree rooted exactly
at `~/.enso` with Enso's current protective ignore block. Effective repository-local and
worktree `extensions.partialClone` or `remote.*.promisor=true` settings are rejected
because even a nominally local object read could otherwise trigger a lazy fetch. The
command does not initialize or repair a repository; `enso setup` owns that operation.

One owner-only `~/.enso/.snapshot.lock` serializes complete Enso snapshot transactions
across processes. While holding it, Enso revalidates the repository, protective rules,
tracked-path boundary, native Git lock, clean native staging area (including the
intent-to-add representation), and `HEAD`. An existing `.git/index.lock` that predates the
transaction, whether active or stale, fails clearly and is never guessed away or deleted.

Enso never uses the native index to assemble a snapshot. It first reserves a safe
`.snapshot-index-<32-lowercase-hex>` basename inside the resolved Git directory. Version
1 of the owner-only root marker at `~/.enso/.snapshot.transaction.json` initially records
the old `HEAD` (or the unborn/null state), its immutable tree, the exact native-index
checksum, and that basename. Every marker update is first written and fsynced as an
owner-only root `.snapshot-transaction-<32-lowercase-hex>.tmp`, then atomically replaces
the marker and fsyncs the root directory. A later call removes only a residue with that
exact name shape whose owner, type, mode, link count, and descriptor identity prove it is
an Enso marker temporary; unsafe residue is preserved and fails closed.

Enso creates the alternate index as a complete owner-only `0600` file. Requested regular
files and symlinks are read with filter-free descriptor reads anchored beneath the locked
repository root. Each exact byte sequence is stored with `git hash-object --no-filters`,
and the associated reviewed mode, object ID, and path enter the alternate index through
`git update-index --add --cacheinfo`. Specifically, the blob write is
`git hash-object -w --no-filters --stdin`. This deliberately bypasses worktree attributes
and clean filters, including repository-local attribute rules; Git cannot substitute
executable code or different bytes for the descriptor-read content. The resulting full tree is
audited against both the allowlist and the literal requested scopes. If the requested
diff is empty, Enso removes the marker and alternate index and returns the successful
no-op.

For a change, `git commit-tree` creates the commit object without moving a ref. Enso then
atomically updates the marker with the new commit OID, tree, and new-index SHA. To exclude
raw Git from the remaining index/ref critical section, Enso atomically hard-links the
complete alternate index to the native Git `index.lock` with exclusive, no-replace
semantics. With that lock held, Enso rechecks the native index against the recorded old
checksum, then atomically compare-and-swaps `HEAD` from the recorded old
value to the new commit with `git update-ref`. It atomically replaces the native index
with the exact Enso-created lock inode and fsyncs the Git directory. No checkout or
worktree mutation occurs.

The marker makes an interruption after commit-object creation or ref update recoverable.
On the next snapshot, Enso compares the recorded old/new transaction identities with the
actual `HEAD` and exact native-index checksum. Only these exact pairs are safe:

| `HEAD` / native index | Recovery |
| --- | --- |
| `old/old` | The ref never advanced; remove an exact Enso-created native lock if present, delete the recorded alternate index and marker, then retry the requested snapshot. |
| `new/old` | The ref advanced; acquire the native lock from the recorded alternate index if absent, or require it to be the exact Enso-created lock if present; atomically install, fsync and verify, then clean up. |
| `new/new` | The ref and native index both advanced; verify and delete the recorded alternate index and marker. |

Any other state means Git diverged from the recorded transaction and fails closed for
operator inspection. In particular, an interruption after the ref compare-and-swap may
leave the new `HEAD` with the old native index until recovery; Enso does not falsely
promise that the previous `HEAD` always remains current. The worktree stays untouched in
all three recoverable states, and native staged work is neither merged nor discarded.
Recovery removes or finalizes `index.lock` only when its inode and checksum match the
Enso alternate index and new-index SHA named by the marker. A pre-existing, substituted,
or otherwise mismatched native lock and every divergent state are preserved for operator
inspection rather than guessed away.

Recovery always runs under `.snapshot.lock`. The next snapshot performs it before a new
transaction; repository `ensure()` also recovers when a marker exists. Read-only
repository validation never performs recovery or any other mutation.

Snapshot operations never fetch, pull, push, or otherwise contact a network. Every Git
child disables lazy fetching and all transport protocols as a second boundary behind the
partial/promisor rejection. Enso does not create, change, or remove Git remotes; a remote
an operator configured remains untouched.

## Mutation ownership

Fresh setup creates one initial snapshot through the same repository service before it
marks setup complete. Other content workflows stop at the point where their mutation is
actually coherent:

- `enso doc create` scaffolds incomplete frontmatter and does not snapshot it.
- `enso job create` scaffolds a disabled job for follow-up editing and does not snapshot
  it.
- Dashboard, filesystem, and agent edits do not silently create a snapshot.

After completing the related content edit, create one scoped snapshot naming every
reviewed versionable path. Do not use raw broad Git staging, include protected/runtime
paths, or invent restore, reset, or delete operations. This keeps one meaningful change
in one commit without turning placeholder creation into misleading history.

## Implementation map

| Area | Responsibility |
| --- | --- |
| `repository.py` | Path classification, repository validation, locking, Git-dir alternate-index audit, transaction recovery, commit creation, native-lock acquisition, ref compare-and-swap, atomic native-index replacement, and cleanup |
| `cli.py` | The `enso snapshot create` command, caller-working-directory handoff, and stable user-facing results |
| `prompts/AGENTS.md` | Default instruction to snapshot coherent versionable content changes safely |
| `skills/*/SKILL.md` | Content-specific snapshot guidance and protected/runtime exclusions |
| `scaffolding.py` | Fresh content creation whose setup transaction is recorded through the repository service |
