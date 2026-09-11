# Releases

Releases are manual, from `main`, with upcoming features assembled on `develop`. We support
one stable release line and do not maintain additional release branches. There are no
release bots, GitHub Actions, or required Git hooks.
[Contributing](../CONTRIBUTING.md#branches) owns the everyday change flow and
[Development](development.md) owns local setup and checks. Creating or pushing a tag, pushing
commits, and publishing artifacts each require an explicit user request; this checklist alone
does not authorize them.

## Versions and notes

Use plain `major.minor.patch` versions. During 0.x, patch releases contain fixes; minor releases
contain features or intentional compatibility breaks. Explain breaks and any user action in
the owning docs and release notes, and never silently lose user data. `0.1.0` is a suitable
initial development release under [SemVer](https://semver.org/spec/v2.0.0.html). Describe the
product as **beta** in prose; the managed builder and update feed do not accept prerelease or
development suffixes.

[CHANGELOG.md](../CHANGELOG.md) is the source of release notes, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Keep a short `Unreleased` section for
notable user-facing changes, grouped under Added, Changed, Fixed, Removed, or Security as needed;
omit empty groups, internal chores, and commit dumps. At release time move those entries under
`## [X.Y.Z] - YYYY-MM-DD` and leave an empty `Unreleased` section for subsequent changes. The
first public release needs only a concise initial-release entry. Reuse the dated section in the
GitHub Release, crediting contributors there when relevant. Published code and artifacts are
immutable: fix a faulty release with a new version, never by moving its tag or replacing assets.

## Choosing the release contents

Use a separate worktree for `main`; keep the regular checkout on `develop` so task jobs
continue to target development. Fetch first and bring both local branches up to date
without discarding local commits. A merge or push to either branch does not publish code.

- **Production patch** (`0.1.2` → `0.1.3`): branch the fix from current `main`, validate it,
  and merge it into `main`. Prepare and publish the patch there using the checklist below.
  Merge the resulting `main`, including its release commit, into `develop`, resolve
  changelog/lockfile conflicts, and run the checks before pushing the synchronized branch.
- **Feature release** (`0.1.x` → `0.2.0`): stop adding features to `develop` while preparing
  the release. Merge current `main` into it and validate the combined tree. With merge
  authorization, promote `develop` to `main`, preferably by fast-forward, and complete
  the checklist on `main`. Merge the release preparation commit back into `develop`
  before starting the next batch of features.

Preserve history when merging between `main` and `develop`; do not squash those merges.
If publication fails, leave the tested release work available and finish or repair that
release before promoting another feature batch. Keep production patches free of unrelated
features. Older version support needs an explicit decision to extend this workflow.

`Unreleased` on `develop` holds the next feature release. A production patch on `main`
gets its own dated changelog section. When merging the patch back, retain both that dated
section and the still-unreleased feature entries. Version metadata is bumped as part of
release preparation, not for every feature merge. Installing unreleased code on a machine
does not publish it or authorize a tag; record the commit and installation mode separately.

## Maintainer checklist

1. Choose the version and review what is shipping. Update `pyproject.toml`, run `uv lock`, and
   review the lockfile. Sweep affected docs and examples, and date the changelog entry.
2. Commit that coherent preparation as `chore: release X.Y.Z`. Start verification from a clean
   tree and record the commit ID. Run the complete
   [development checks](development.md#setup-and-checks).
   If any source change is needed, commit it and repeat verification; tag only the tested commit.
3. Build the bundle below. Inspect its four artifacts and smoke-test those exact wheel and
   constraint bytes in a fresh home and bin directory. Run the
   [isolated upgrade acceptance checks](upgrade-testing.md), including recovery and relevant
   native service/transport checks. Record platform evidence and anything not tested; do not
   silently treat a skipped required check as passed. Do not touch the operator's active home.
4. With explicit release authority, create an annotated `vX.Y.Z` tag at the tested commit and
   push that commit and tag. Create a **draft** GitHub Release using the dated changelog notes;
   attach all four artifacts before publishing. Verify the tag, manifest commit/version, asset
   names, and checksums. Use `--verify-tag` with `gh release create` so it cannot silently create
   a tag at an unintended commit.
5. Publish the complete draft, mark it as latest, and verify the version-pinned manifest and
   artifact downloads. Test the README's one-line download with explicit scratch `--home` and
   `--bin-dir` options, and confirm `enso update check` reads the embedded moving feed.
   Record the released tag, checks, and any remaining limitations in the task outcome.
6. Merge the release commit from `main` into `develop`, preserving any newer unreleased
   entries. Validate the result and push `develop` when that push is authorized. Clean up
   merged short-lived branches/worktrees within the authorized scope; retain both `main`
   and `develop` and leave the regular checkout on `develop`.

When using GitHub's `releases/latest/download/release.json` as the feed, publish an ordinary
release, **not** a GitHub prerelease: [GitHub's latest release](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)
excludes drafts and prereleases. The title can still say `v0.1.0 (beta)`. This keeps one update
channel without adding preview-channel machinery. GitHub also recommends
[drafting and attaching assets before publication](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

## Build and smoke-test

Native installs and Enso Cloud consume the same bundle: a wheel, exact dependency versions
exported from `uv.lock`, a manifest, and a self-contained shell installer. Cloud base images
supply operating-system tools; installing a pinned bundle keeps application releases separate.

The following values are examples, not live deployment configuration. Set the version to match
package metadata and choose the real release repository only when preparing a publication:

```bash
release_version=0.1.0
release_repo=OWNER/REPO
release_output=$(mktemp -d /tmp/enso-release.XXXXXX)
uv run python scripts/build-release.py --output "$release_output" \
  --artifact-base-url "https://github.com/$release_repo/releases/download/v$release_version" \
  --feed-url "https://github.com/$release_repo/releases/latest/download/release.json" \
  --release-notes-url "https://github.com/$release_repo/releases/tag/v$release_version"
```

The command builds a wheel, exports all runtime extras from the locked dependency graph, and
creates these artifacts together:

```text
<release output>/
├── release.json
├── enso-0.1.0-py3-none-any.whl
├── constraints.txt
└── install.sh
```

It refuses a dirty checkout, stale lockfile, or nonempty output directory. `--allow-dirty` is
only for local smoke tests; it does not make that checkout a publishable release. The manifest's
version and Python requirement come from package metadata, and its commit identifies the source.
The installer is generated from the same release-verification code shipped inside Enso, so
there is no separately maintained download or checksum implementation.

`--artifact-base-url` emits absolute URLs and requires an HTTPS directory whose final component
is the exact package version or `v<version>`. Credentials, queries, fragments, escaped paths,
and dot segments are refused. A moving feed must use these version-pinned artifact URLs so
concurrent upgrades cannot accidentally download a newer release's constraints. The option
cannot enforce server-side immutability; never replace the published directory's contents.
Omit it to build a self-contained local bundle using relative URLs.

With `--artifact-base-url`, the generated installer defaults to `release.json` in that
version directory. `--feed-url` embeds the stable HTTPS feed for later update checks and
requires `--artifact-base-url`. Explicit installer arguments override the embedded defaults.
Without a feed, installs save the manifest source; without an artifact base, the installer
still requires `--manifest`. Build the public bundle for `geekforbrains/enso` with both URL
options so users can install and check future releases without supplying either URL.

Before upload, the remote URLs will not exist. Make a temporary local manifest for the exact
published artifacts, preserving their hashes and leaving the original manifest untouched:

```bash
release_smoke=$(mktemp -d /tmp/enso-release-smoke.XXXXXX)
uv run python - "$release_output" "$release_smoke/release.json" <<'PY'
import json, sys
from pathlib import Path
from urllib.parse import urlsplit
bundle = Path(sys.argv[1]).resolve()
manifest = json.loads((bundle / "release.json").read_text())
for name in ("wheel", "constraints"):
    manifest[name]["url"] = str(bundle / Path(urlsplit(manifest[name]["url"]).path).name)
Path(sys.argv[2]).write_text(json.dumps(manifest))
PY
sh "$release_output/install.sh" --manifest "$release_smoke/release.json" \
  --home "$release_smoke/home" --bin-dir "$release_smoke/bin"
"$release_smoke/bin/enso" --version
"$release_smoke/bin/enso" --help
```

Check that the reported version matches the bundle. Also exercise a base-only install with
`--extras ''` in a second scratch home/bin pair. If running setup, decline background service
installation: service units live outside `ENSO_HOME`. Temporary smoke manifests are not release
assets; upload only the four files in the original output directory.

## Manifest contract

`release.json` uses this versioned format; all shown fields are required except
`release_notes_url`:

```json
{
  "schema_version": 1,
  "version": "0.1.0",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "requires_python": ">=3.14",
  "wheel": {
    "url": "enso-0.1.0-py3-none-any.whl",
    "sha256": "<64 lowercase hexadecimal characters>"
  },
  "constraints": {
    "url": "constraints.txt",
    "sha256": "<64 lowercase hexadecimal characters>"
  },
  "release_notes_url": "https://github.com/OWNER/REPO/releases/tag/v0.1.0"
}
```

Relative artifact URLs resolve beside the manifest and cannot traverse to a parent directory.
Absolute HTTPS URLs support separate artifact storage. Explicit local manifests can also name
absolute local files, which allows a pinned update operation to keep its original artifacts.
HTTP is accepted only for numeric loopback addresses during isolated tests. Manifests reject
unknown fields, embedded URL credentials, unsupported schemas, and malformed values together.
Manifest and saved feed URLs also reject query strings; use token-file authentication.
Artifact URLs may contain a signed download query, but never receive a bearer token from
a different origin. Local feed paths are saved as absolute paths, independent of later
working directories.

Both artifacts are SHA256-checked before installation. Wheel metadata must match the declared
package name, version, and Python requirement. Constraints contain only exact package-version
pins; source builds and dependency-file directives are refused. The installer creates the
environment at its permanent path because console-script paths inside a Python environment
must remain valid after an update.

## Publish and install

Only after the checklist passes and the tag/push/publication actions are explicitly requested,
use the chosen `release_repo`, `release_version`, and original `release_output` from above.
Copy the dated changelog section to a temporary `release_notes` file outside that output directory.
Check that the manifest commit is the exact commit you tested and intend to publish on `main`:

```bash
release_commit=$(uv run python -c 'import json, sys; print(json.load(sys.stdin)["commit"])' \
  < "$release_output/release.json")
release_notes=/absolute/path/to/dated-release-notes.md
git show --stat "$release_commit"
git tag -a "v$release_version" "$release_commit" -m "Release $release_version"
git push --atomic "git@github.com:$release_repo.git" \
  "${release_commit}:refs/heads/main" "refs/tags/v$release_version"
gh release create "v$release_version" --repo "$release_repo" --draft --verify-tag \
  --title "v$release_version (beta)" --notes-file "$release_notes" \
  "$release_output/release.json" "$release_output/enso-$release_version-py3-none-any.whl" \
  "$release_output/constraints.txt" "$release_output/install.sh"
gh release view "v$release_version" --repo "$release_repo"
```

Stop and inspect the draft and uploaded assets before the final publication step; if a command
fails, resolve it before continuing. Do not force-push tags or overwrite published assets.

```bash
gh release edit "v$release_version" --repo "$release_repo" --draft=false --latest
```

The [GitHub CLI release commands](https://cli.github.com/manual/gh_release_create) keep draft
creation separate from publication; no automation runs this sequence on your behalf.

Publish the complete bundle in an immutable version directory. Update a stable HTTPS feed only
after every artifact is available. For GitHub Releases, the publication layout is:

- Pinned manifest: `https://github.com/OWNER/REPO/releases/download/vX.Y.Z/release.json`
- Pinned installer: `https://github.com/OWNER/REPO/releases/download/vX.Y.Z/install.sh`
- Latest installer: `https://github.com/OWNER/REPO/releases/latest/download/install.sh`
- Moving feed: `https://github.com/OWNER/REPO/releases/latest/download/release.json`

The [latest asset URL](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)
provides the public one-line install command. Its installer uses the embedded version-pinned
manifest, even if a newer release becomes latest during installation, and saves the embedded
moving feed for later approved updates. Use a version-pinned installer URL when selecting an
exact release. These are URL patterns, not a claim that the public endpoints are already
deployed. The configured feed is a trusted software source:
checksums detect partial or modified downloads, but do not replace the HTTPS origin's authority
to publish releases.

Serve the generated `install.sh` from the trusted release host. The shell installer requires
`curl` only when uv is missing, provisions uv without modifying shell profiles, and lets uv
install Python 3.14. uv tools, Python, caches, and environments live inside the selected Enso
home. A separately selected bin directory receives the stable `enso` launcher.

```bash
curl -fsSL https://github.com/geekforbrains/enso/releases/latest/download/install.sh | sh
```

For publication verification, replace `| sh` with `| sh -s -- --home "$release_smoke/home"
--bin-dir "$release_smoke/bin"` using fresh scratch paths. Verify that the runtime receipt
records the intended version and moving feed. A local bundle with relative URLs also works
without a release host:

```bash
sh install.sh --manifest /tmp/enso-release-0.1.0/release.json \
  --home /tmp/enso-install-smoke --bin-dir /tmp/enso-install-bin
```

The source checkout's root `install.sh` is a local wrapper; the bundle's generated `install.sh`
is the standalone file to host for curl installs. `--extras slack,telegram,web` selects installed
features; pass `--extras ''` for the base CLI. See [Install](install.md) for adoption and updates.

For private beta downloads, pass `--token-file /path/to/private-token` rather than putting a
credential in a URL or command argument. The file contains one bearer token. Enso sends it
only to the manifest's origin, including its port; it is removed permanently when a redirect
crosses origins, and never forwarded to an artifact CDN. Downloads and subprocess output are
bounded, failures omit URLs and installer diagnostics that might contain credentials, and a
failed preparation removes only the newly created candidate environment.

Cloud can install from a local bundle while saving a separate stable feed with `--feed URL`.
Give each instance a scoped feed token; guests need no GitHub credentials. Cloud should publish
the application bundle before deploying the control plane that selects that release for new
guests. Existing guests retain their selected version until their owner approves an update.
