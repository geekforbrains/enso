# Contributing

This guide covers the local development workflow and the complete release process.
Product behavior belongs in the relevant guide and specification; shipped behavior also
needs an entry in [`CHANGELOG.md`](../CHANGELOG.md).

## Development environment

```bash
git clone https://github.com/geekforbrains/enso.git
cd enso
pip install -e ".[dev,telegram,slack,web]"
```

Run the standard checks before committing:

```bash
ruff check .
mypy
pytest
```

Tests use temporary Enso homes and mock provider/transport boundaries. Add focused tests
for behavior changes, then run the full suite before merging or releasing.

## Branches and commits

| Branch | Purpose |
| --- | --- |
| `main` | Latest stable release; release commits are merged and tagged here |
| `dev` | Integrated work for the next release |
| `feat/*`, `fix/*` | Short-lived work based on `main` or `dev` as appropriate |

Use Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, and so on).
Merge completed work into `dev`; do not publish a release directly from an unintegrated
feature branch.

## Documentation and changelog

Guides under `docs/` explain operator workflows. Specifications under `docs/specs/` own
the detailed contracts and implementation status. Update both when a behavior change
affects both layers, and keep the root README focused on discovery and first use.

`CHANGELOG.md` follows Keep a Changelog. Add notable user-visible entries under
`[Unreleased]` as changes merge. Breaking changes need a prominent migration section and
a tested guide under `docs/migrations/`; do not leave migration work implicit in a long
Added or Changed list.

## Version metadata

`pyproject.toml` is the only version written by hand. `enso.__version__`, `enso --version`,
startup logs, and update confirmations read installed distribution metadata through
`importlib.metadata`.

An editable checkout can therefore retain stale installed metadata after the source
version changes. Refresh it without changing dependencies:

```bash
pip install -e . --no-deps
enso --version
```

The updater compares Git revisions rather than version strings, so stale editable
metadata misreports the version but does not cause an incorrect update.

## Release checklist

1. Confirm the intended release scope on `dev`, including any deliberately excluded
   branches, and choose the version from compatibility impact rather than commit count.
2. Resolve every release-note, guide, CLI-help, and link discrepancy. For a breaking
   release, finish and test the migration guide before changing the version.
3. Rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a concise summary, and update
   `pyproject.toml` to the same version.
4. Run release gates from the repository root:

   ```bash
   git diff --check
   ruff check .
   mypy
   pytest
   ```

5. Build both distributions and smoke-test the wheel in an isolated environment. Confirm
   the installed `enso --version`, top-level help, and packaged prompts, skills, starter
   docs, templates, and static assets.
6. Review the complete staged diff and commit it as `chore(release): X.Y.Z`.
7. Fetch `origin`, verify that local `main` and `dev` still match their expected remote
   tips, merge `dev` into `main` with a release merge commit, and create an annotated
   `vX.Y.Z` tag on that merge.
8. Fast-forward `dev` to the tagged `main` commit. This keeps the latest release tag in
   `dev`'s ancestry, so `git describe` and future release ranges start at the right tag.
9. Push `main`, `dev`, and the tag atomically where supported.
10. Create a non-draft, non-prerelease GitHub Release from the existing tag. Give breaking
    migrations first-class placement, link guides to the immutable tag, include concise
    highlights, and mark it as the latest release.
11. Verify the remote branch SHAs, annotated tag, GitHub Release URL/body/latest status,
    and the tagged README and changelog. Do not infer success from the push command alone.
12. Refresh editable installs and restart long-running `enso serve` or `enso web`
    processes where the release should be deployed.

The release commit, merge, tag, push, and GitHub Release are separate state changes.
Perform only the ones explicitly authorized for that release.
