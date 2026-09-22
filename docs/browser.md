# Browser

`enso-browser` is the bundled workflow for sites that need a real browser: JavaScript
pages, authenticated accounts, screenshots, and actions the user has requested. It keeps
one persistent Google Chrome process per named profile and attaches a locally installed
Microsoft Playwright MCP server when browser work begins. Starting the MCP server or
discovering its tools does not start Chrome. MCP is the protocol a provider uses to
expose browser tools to the agent.

The skill and `enso browser` commands ship with Enso. Chrome, Node.js, the pinned MCP package, and the
provider's MCP registration are optional setup, not part of `enso init` or `enso setup`.
Other Enso features need none of them. Browser automation supports macOS and Linux with a
graphical desktop; it does not offer a headless or Cloud login workflow.

## Setup and profiles

Load `enso-browser` and follow its
[setup reference](../src/enso/bundled/skills/enso-browser/references/setup.md). It uses an
existing Chrome and an explicit local installation of `@playwright/mcp@0.0.80` under the
home; no global install, implicit package download, or provider configuration write is
performed by the helper. Changing the pin requires reviewing and testing the new package.

After completing the optional dependency setup, use the same commands on an installed release
or the local development instance:

```bash
enso browser create
enso browser mcp --print-config
enso browser open --url https://example.com
enso browser status
enso browser list
```

The `enso` launcher selects its Python environment. From a checkout, use `uv run enso browser`.
`mcp --print-config` prints an absolute `enso` command, `browser mcp` arguments, and environment
without starting Chrome or editing files. Managed releases and the local development instance
use the stable launcher, so the registration survives code updates;
adapt that proposal to the selected provider's documented MCP configuration and reconnect
the provider. The running `mcp` command speaks protocol on stdin/stdout, not human-readable
diagnostics. Dependencies must already be installed.

Omitting a profile selects `default`. Names start with a lowercase letter and use
lowercase letters, digits, and single hyphens, up to 48 characters. Create a separate
profile for another account or concurrent work:

```bash
enso browser create work
enso browser mcp work --print-config
enso browser open work --url https://example.com
enso browser stop work
```

Each profile needs its own provider registration. Workspace `AGENTS.md` may say which
profile to use; without such guidance the default is shared across workspaces. A profile
name is not proof of the signed-in identity. Dependency setup is shared by all profiles
within the same Enso home; a new profile needs no separate npm install or port selection.
One MCP controller may use a profile at a time. The controller lock is taken when its
MCP server starts, even before Chrome starts; a second controller fails promptly instead
of competing for tabs. Some providers eagerly start every registered server on every
turn, including turns that never browse. Those turns do not open Chrome. Use a
workspace-scoped registration or the provider's supported server selection so concurrent
providers each load only their own profile. Registering every profile globally makes them
compete for every lock. Concurrent turns in one workspace need distinct selected profiles
or serialized browser work; profile names alone do not schedule access.

`create` is idempotent. `open` and `mcp` create a missing profile. `open` immediately
starts or reuses its Chrome; `mcp` waits until a browser tool needs a connection, then
starts or reuses the same verified Chrome. Both preserve tabs. `open --url` opens a new
tab; it does not navigate an existing form. `create`, `list`, `status`,
`mcp --print-config`, MCP initialization, and tool discovery do not launch Chrome.
Browser commands return JSON. Expected command or MCP startup failures produce
a diagnostic on stderr and exit 1; invalid syntax exits 2. If Chrome startup fails during
a browser tool, that tool's connection fails and stderr explains why. MCP remains
available for a later retry after the cause is resolved.

The implementation is part of the Enso package, so upgrading Enso or refreshing the development
checkout updates the browser code. It works without home initialization, active configuration,
or a database. Its long-lived MCP connection does not hold the updater's home-access lock.
Managed upgrades refresh untouched skill instructions according to
[the bundled-file rules](customizing.md#the-bundled-skills).

Registrations that invoke the old `skills/enso-browser/scripts/browser.py` need a one-time
replacement using `enso browser mcp [profile] --print-config`. Preserve the selected profile,
home, Chrome override, and provider-specific settings, then reconnect the provider. Managed
updates remove the old helper only if untouched; edited and historical copies are preserved
but no longer updated. Existing profiles, tabs, and logins remain usable. Review and merge
customized instructions to use `enso browser`.

## Human login and lifecycle

The person signs in through the profile's Chrome window and handles passwords, MFA, or
CAPTCHA there. Agents do not collect credentials in chat or bypass challenges. Verify
authenticated content and the intended account after login; a URL or cookie count alone
does not prove authentication. `status` checks the process and debugging endpoint, not
whether a site is signed in.

Once ready, Chrome outlives a provider turn so a person can inspect or continue a page
between messages. When MCP ends, the helper stops and waits for its MCP child, releases
the controller lock, and exits. It preserves ready Chrome and its tabs; an incomplete
Chrome startup is cleaned up if cancelled. Do not close unrelated tabs, forms, or a
pending handoff. `stop [profile]`
gracefully terminates only the recorded process after checking its start identity and
profile arguments; it never kills by a loose process-name match. It preserves the profile
on disk. Stale or mismatched ownership fails safely; do not delete browser lock files to
override a running Chrome. A reboot or manual quit ends the process, and a later `open`
or browser connection reuses its saved profile.

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
While MCP runs, the helper also serves Chrome's verified address through a temporary
loopback listener. Playwright connects directly to Chrome after that discovery; the
helper does not forward browser traffic or install a background service. Both ports are
assigned automatically and remain local to the machine.
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
