# Working on Enso

This is the entrypoint for people and CLI agents changing Enso's source. Files under
`src/enso/bundled/` are content shipped to an Enso home, not instructions for this checkout.

## Read before changing files

- [Contributing](CONTRIBUTING.md) owns the issue, branch, PR, and commit conventions.
- [Development](docs/development.md) owns source layout, setup/check commands, coding,
  testing, safety, task-board use, parallel work, and completion requirements. Read it
  before implementation; follow its Git authorization limits.
- [Concepts](docs/concepts.md) defines the product. Read the owning topic page linked
  from there or the [README documentation map](README.md#documentation) for the change.
- [Releases](docs/releasing.md) owns versioning, changelog entries, and the manual release
  checklist. Read it for release work; a code change alone does not authorize publication.

## Authority and scope

The user-approved task defines the intended change; the owning product docs define
behaviour. Runtime CLI help defines accepted command syntax. Tests are evidence, not a
substitute specification. Resolve disagreements explicitly and update the owning docs
with the change.

Preserve unrelated work and choose the smallest coherent solution. Each fact has one
owning page: update that page and link to it rather than copying conventions here.
Do not introduce Git hooks, GitHub Actions, or release automation without an explicit ask.

## Handoff

Follow [Development § Completion](docs/development.md#completion). In a stage job, the
prompt's Task block identifies the task and worktree; follow the scoped
[agent workflow](docs/development.md#agent-and-maintainer-work).

`CLAUDE.md` beside this file must remain a relative symlink to `AGENTS.md`.
