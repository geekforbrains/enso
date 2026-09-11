# Contributing to Enso

Bug reports, documentation improvements, and focused pull requests are welcome. Enso is
maintained by one person, so keep the process small and the change easy to review.

## Before changing code

Read [AGENTS.md](AGENTS.md) for the project map, then the relevant product documentation.
[Development](docs/development.md) owns source layout, setup, checks, and coding conventions.

For a bug report, include your Enso version, operating system, what you expected, what
happened, and the smallest reproduction you can provide. Remove credentials, private
messages, and personal data from logs and examples.

Discuss substantial features or design changes in an issue before implementing them.
Small fixes and documentation improvements can go straight to a pull request. Public
contributors do not need access to the maintainer's Enso task board; use the issue and PR
for context, decisions, and progress.

## Branches

We keep two long-lived branches:

| Branch | Purpose |
| --- | --- |
| `main` | The current stable release line and production patches |
| `develop` | Completed changes being assembled for the next feature release |

Use a short-lived branch for each coherent change. New features and fixes that only
affect development branch from `develop` and return there; use names such as
`feat/en-018-viewer-service` or `fix/viewer-startup`. A production fix branches from
`main`, returns to `main`, and is also merged into `develop` so the next release keeps
the fix. Before starting work, fetch and check that the chosen base includes its remote
counterpart. Never merge unfinished feature work into `main` to obtain a production patch.

Merge completed, tested feature branches into `develop`; leave unfinished changes on
their own branches. When the feature release is ready, merge `main` into `develop`,
resolve any conflicts and test the combined result, then promote `develop` to `main`.
Keep merges between these two branches as normal merges or fast-forwards, never squash
merges or cherry-picked copies of the whole branch. Bring release preparation and patches
on `main` back into `develop`. [Releases](docs/releasing.md) owns the exact release sequence.

The regular maintainer checkout stays on `develop`. Use a separate Git worktree for
production patches and release work, leaving the regular checkout's branch unchanged;
Enso's task worktrees use that branch as their base and landing target. See
[Development](docs/development.md#agent-and-maintainer-work) before running stage jobs.

After a change has been merged and verified, remove its short-lived branch when cleanup
is authorized. Preserve unmerged branches and both long-lived branches. Merging and
pushing code do not publish a release; live installations normally follow tagged artifacts.

## Pull requests

Fork the repository if needed. Target `develop` for normal development and `main` for a
production patch, following the [branch rules](#branches).

Keep each PR focused on one coherent change. Include relevant tests and update the owning
docs and examples in the same PR. For a notable user-facing change, add a concise entry
under `Unreleased` in [CHANGELOG.md](CHANGELOG.md); the
[release conventions](docs/releasing.md) explain what belongs there.

Run the [development checks](docs/development.md#setup-and-checks) locally. In the PR description,
explain the change, link any issue, and state which checks you ran, anything skipped, and
any remaining risk. Checks are manual for now; there are no required repository hooks or
GitHub Actions workflows.

Use a [Conventional Commit](https://www.conventionalcommits.org/en/v1.0.0/) PR title, such
as `fix: preserve edited skills during updates`, `feat: add a transport`, or
`docs: clarify setup`. Mark an intentional compatibility break with `!`, explain it in
the PR, and document its effect on users. Intermediate commits need not be polished:
the maintainer may squash a short-lived change branch into one commit using the PR title.
Merges between `main` and `develop` preserve ancestry as described above.

## Maintainer workflow

Use the same focused-change and local-check expectations for solo work. Small, obvious
changes may be committed directly to `develop`; use a short-lived branch and PR when a
separate review is useful. Production fixes still follow the `main` patch path. Commit
messages follow the same Conventional Commit format.
CLI agents must still respect the explicit Git permissions in
[Development](docs/development.md#agent-and-maintainer-work).

Releases are manual and follow [the release checklist](docs/releasing.md). Merging or
committing a change does not publish a release.
