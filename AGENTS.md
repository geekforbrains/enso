# Working on Enso

This is the entrypoint for people and CLI agents changing Enso's source. Files under
`src/enso/bundled/` are content shipped to an Enso home, not instructions for this checkout.

## Read before changing files

- [Contributing](CONTRIBUTING.md#branches) owns the `main`/`develop` workflow, branch
  targets, PRs, and commits. Read it before choosing a base or merging work.
- [Development](docs/development.md) owns source layout, setup/check commands, coding,
  testing, safety, task-board use, parallel work, and completion requirements. Read it
  before implementation; follow its Git authorization limits.
- [Concepts](docs/concepts.md) defines the product. Read the owning topic page linked
  from there or the [README documentation map](README.md#documentation) for the change.
- [Releases](docs/releasing.md) owns versioning, changelog entries, and the manual release
  checklist. Read it for release work; a code change alone does not authorize publication.
- For bundled-skill refinement, read [Skill evaluations](docs/development.md#skill-evaluations)
  before editing: preserve a baseline, use the local CLI runner when live evaluation is
  requested, and report correctness alongside tokens and tool calls.

## Authority and scope

The user-approved task defines the intended change; the owning product docs define
behaviour. Runtime CLI help defines accepted command syntax. Tests are evidence, not a
substitute specification. Resolve disagreements explicitly and update the owning docs
with the change.

Preserve unrelated work and choose the smallest coherent solution. Each fact has one
owning page: update that page and link to it rather than copying conventions here.
Do not introduce Git hooks, GitHub Actions, or release automation without an explicit ask.

## Local development instance

Gavin uses this checkout's `develop` branch to run his local Enso. After completing,
testing, and committing a requested change, refresh and verify that instance using
[Development § Local development loop](docs/development.md#local-development-loop).
This is standing authorization for that routine code refresh; do not ask again.
Preserve `~/.enso` content, including installed jobs and skills. Required home migrations
use the separate, explicit migration workflow documented there; never reset or reseed the home.
An Enso stage job hands this step to an external maintainer session instead of restarting
the service that is running it.

## Handoff

Follow [Development § Completion](docs/development.md#completion). In a stage job, the
prompt's Task block identifies the task and worktree; follow the scoped
[agent workflow](docs/development.md#agent-and-maintainer-work).

`CLAUDE.md` beside this file must remain a relative symlink to `AGENTS.md`.
