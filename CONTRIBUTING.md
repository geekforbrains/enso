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

## Pull requests

We use one long-lived branch, `main`, and short-lived branches for changes. Fork the
repository if needed and open your PR against `main`; there is no separate development
or release branch process.

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
the maintainer reviews the change and squash-merges it into one commit using the PR title.

## Maintainer workflow

Use the same focused-change and local-check expectations for solo work. Small, obvious
changes may be committed directly to `main`; use a short-lived branch and PR when a
separate review is useful. Commit messages follow the same Conventional Commit format.
CLI agents must still respect the explicit Git permissions in
[Development](docs/development.md#agent-and-maintainer-work).

Releases are manual and follow [the release checklist](docs/releasing.md). Merging or
committing a change does not publish a release.
