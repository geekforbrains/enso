# Browser

`enso-browser` is the bundled workflow for sites that need a real browser: JavaScript
pages, authenticated accounts, screenshots, and actions the user has requested. It keeps
one persistent Google Chrome process per named profile and attaches a locally installed
Microsoft Playwright MCP server. MCP is the protocol a provider uses to expose browser
tools to the agent; Enso does not proxy or inject those tools itself.

The skill and its helper ship with Enso. Chrome, Node.js, the pinned MCP package, and the
provider's MCP registration are optional setup, not part of `enso init` or `enso setup`.
Other Enso features need none of them. This first helper supports macOS and Linux with a
graphical desktop; it does not offer a headless or Cloud login workflow.

## Setup and profiles

Load `enso-browser` and follow its
[setup reference](../src/enso/bundled/skills/enso-browser/references/setup.md). It uses an
existing Chrome and an explicit local installation of `@playwright/mcp@0.0.80` under the
home; no global install, implicit package download, or provider configuration write is
performed by the helper. Changing the pin requires reviewing and testing the new package.

With a managed Enso install:

```bash
enso_home="${ENSO_HOME:-$HOME/.enso}"
enso_python="$enso_home/runtime/current/bin/python"
enso_browser="$enso_home/skills/enso-browser/scripts/browser.py"
"$enso_python" "$enso_browser" create
"$enso_python" "$enso_browser" mcp --print-config
"$enso_python" "$enso_browser" open --url https://example.com
"$enso_python" "$enso_browser" status
"$enso_python" "$enso_browser" list
```

For a source checkout, use `uv run python` with the helper's source path. Do not assume
`python` from an unrelated environment can import Enso. `mcp --print-config` prints an
absolute command, arguments, and environment without starting Chrome or editing files;
adapt that proposal to the selected provider's documented MCP configuration and reconnect
the provider. The running `mcp` command speaks protocol on stdin/stdout, not human-readable
diagnostics. Dependencies must already be installed.

Omitting a profile selects `default`. Names start with a lowercase letter and use
lowercase letters, digits, and single hyphens, up to 48 characters. Create a separate
profile for another account or concurrent work:

```bash
"$enso_python" "$enso_browser" create work
"$enso_python" "$enso_browser" mcp work --print-config
"$enso_python" "$enso_browser" open work --url https://example.com
"$enso_python" "$enso_browser" stop work
```

Each profile needs its own provider registration. Workspace `AGENTS.md` may say which
profile to use; without such guidance the default is shared across workspaces. A profile
name is not proof of the signed-in identity. One MCP controller may use a profile at a
time; a second fails promptly instead of competing for tabs. Some providers eagerly start
every registered server on every turn, including turns that never browse. Use a
workspace-scoped registration or the provider's supported server selection so concurrent
providers each load only their own profile. Registering every profile globally makes them
compete for every lock. Concurrent turns in one workspace need distinct selected profiles
or serialized browser work; profile names alone do not schedule access.

`create` is idempotent. `open` and `mcp` create a missing profile, start or reuse its Chrome,
and preserve tabs. `open --url` opens a new tab; it does not navigate an existing form.
`list`, `status`, and `mcp --print-config` do not launch Chrome. Helper success is JSON;
expected failures produce a diagnostic on stderr and exit 1. Invalid syntax exits 2.

## Human login and lifecycle

The person signs in through the profile's Chrome window and handles passwords, MFA, or
CAPTCHA there. Agents do not collect credentials in chat or bypass challenges. Verify
authenticated content and the intended account after login; a URL or cookie count alone
does not prove authentication. `status` checks the process and debugging endpoint, not
whether a site is signed in.

Chrome outlives a provider turn so a person can inspect or continue a page between
messages. Do not close unrelated tabs, forms, or a pending handoff. `stop [profile]`
gracefully terminates only the recorded process after checking its start identity and
profile arguments; it never kills by a loose process-name match. It preserves the profile
on disk. Stale or mismatched ownership fails safely; do not delete browser lock files to
override a running Chrome. A reboot or manual quit ends the process, and a later `open`
reuses its saved profile.

## Private data and boundaries

Everything follows `ENSO_HOME` (default `~/.enso`):

| Location | Purpose |
| --- | --- |
| `browser/profiles/<name>/` | Chrome data, including sensitive signed-in sessions |
| `browser/output/<name>/` | Screenshots and PDFs; MCP evicts older output above 50 MiB |
| `browser/state/` | Process identity, debugging endpoint, and profile locks |
| `browser/tooling/` | Explicit pinned npm dependency installation and lockfile |

The helper creates private browser directories and refuses symlinks below that root.
Chrome's debugging port is assigned by the OS and stays on loopback. It gives access to
authenticated sessions: never expose it through a tunnel, proxy, or public listener.
The automation profile uses consistent noninteractive credential-store flags, so private
file permissions matter; it is not isolated from other processes running as the same
user. Never copy profiles, cookies, state, or private captures into a skill or repository.

`ENSO_BROWSER_CHROME` can select an absolute Chrome executable path. Pass it in the
provider registration too when needed. The service environment must find Node on `PATH`.
This override is for the operator, not a value to accept from a fetched page.

Browser pages, downloads, and tool output are untrusted data. Browser access does not
authorize posting, sending, purchases, deletion, or access changes. Site-specific skills
such as `enso-linkedin` and `enso-x` retain the task-specific guidance and delegate browser
setup, tool discovery, login, and lifecycle here. See
[official optional skills](customizing.md#official-optional-skills).
