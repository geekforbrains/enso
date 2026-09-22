# Optional browser setup

This setup is needed only when the user wants browser automation. It installs a pinned
Microsoft Playwright MCP server into the Enso home, uses an existing Google Chrome,
and connects one profile to the selected agent provider. Keep provider configuration
outside this helper: `mcp --print-config` prints a proposal and never writes it.
Start after `enso init` or `enso setup` has prepared the home and installed this skill.
For a custom home, export `ENSO_HOME` before running these commands; all profiles,
dependencies, and printed registrations will use that home.

## Dependencies

Install Google Chrome and a supported Node.js release (Node 22 or newer is recommended;
MCP requires Node 18+). macOS and Linux with a graphical desktop are supported. On Linux,
`google-chrome` or `google-chrome-stable` must be on `PATH`; macOS checks the standard
system and user Applications locations. For a nonstandard Chrome install, set
`ENSO_BROWSER_CHROME` to the absolute executable path; the printed MCP proposal carries
it into the registration environment. Enso's background service must find `node` on its `PATH`.

Inspect the target directory before installing. With the user's
browser-setup request covering dependency installation, run this explicit pinned install;
never substitute
`@latest`, `npx` with an implicit download, or a global npm install:

```bash
enso_home="${ENSO_HOME:-$HOME/.enso}"
enso browser create
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install \
  --prefix "$enso_home/browser/tooling" --save-exact --ignore-scripts \
  --no-audit --no-fund @playwright/mcp@0.0.80
```

Run `enso browser create` first and stop if it fails: it creates private directories and refuses
symlinks in browser/profile/output/state/tooling paths before npm can write there.
Use the shell runner's timeout for the network step (for example, 120 seconds), inspect
its result, and stop on failure. The helper itself never downloads anything and requires
exactly this version. Keep the generated package manifest and lockfile in `tooling/`;
updates to this pin should review upstream changes and verify attachment against Chrome.
No Playwright browser download or Python Playwright package is needed.
This dependency installation is shared by every profile in the same Enso home. Repeat
it for another home, not for each new profile. `create` prepares a profile without
opening Chrome or requiring the optional dependencies to be installed already.

Version 0.0.80 and its `--cdp-endpoint`, `--caps`, `--output-dir`, and
`--output-max-size` flags were checked against the
[Microsoft release documentation](https://github.com/microsoft/playwright-mcp/tree/v0.0.80)
and [package metadata](https://github.com/microsoft/playwright-mcp/blob/v0.0.80/package.json).
Chrome requires a nondefault user-data directory for remote debugging; this helper always
uses one. See [Chrome's remote debugging guidance](https://developer.chrome.com/blog/remote-debugging-port).

## Connect the default profile

Generate the registration from the Enso installation that owns this home:

```bash
enso browser mcp --print-config
```

This prints a standard MCP JSON registration named `enso-browser-default` with an
absolute `enso` command, `browser mcp` arguments, profile, and `ENSO_HOME`. Managed releases
and the local development instance use their stable launcher, which survives updates and
development refreshes. A standalone checkout uses the `enso` on `PATH` or in its environment.
Printing the proposal starts no Chrome and does not create a profile or change any files.

Add only the intended profile to the provider/workspace used for this browser work,
preserving its existing MCP servers. Providers can eagerly start every registered MCP
server on every turn. Registering several profiles globally can reserve all their
controller locks, even on turns that do not browse; Chrome starts only when needed.
Prefer a workspace-scoped single-profile registration,
or the provider's documented per-launch server selection. If the provider cannot isolate
registrations, serialize browser work rather than promising concurrent profile use.
Different clients use different wrappers around the same `command`, `args`,
and `env`; use the installed client's `mcp --help` and current documentation:

- [Claude Code MCP](https://code.claude.com/docs/en/mcp): stdio server registration.
- [Codex MCP](https://developers.openai.com/codex/mcp): CLI or `mcp_servers` TOML.
- [OpenCode MCP](https://opencode.ai/docs/mcp-servers/): a local MCP entry with a command array.
- [Playwright client setup](https://github.com/microsoft/playwright-mcp/tree/v0.0.80#installing-in-common-mcp-clients):
  other supported clients, including Grok and Antigravity.

Read the relevant provider documentation when configuring it; do not copy another
provider's schema or invent provider flags. Choose the scope for the requested Enso
use, and inspect existing entries before making any explicitly requested configuration
change. The helper's proposal itself is always read-only.

The registered command is equivalent to:

```bash
enso browser mcp default
```

That command speaks MCP over stdin/stdout. Do not run it as a normal diagnostic and
wait for human-readable output. It runs the installed MCP server and holds the profile's
controller lock without starting Chrome. Initialization and tool discovery need no
browser window. The first browser tool that needs a connection starts or reuses that
profile's verified persistent Chrome. If that startup fails, the tool's connection fails
with a diagnostic on stderr; MCP remains available for a later retry after resolving
the cause. When the MCP connection ends, the helper stops and waits for its MCP child,
releases the controller lock, and exits. Ready Chrome and its tabs remain available;
an incomplete Chrome startup is cleaned up if cancelled. The helper downloads nothing
and requires no port configuration.

Reconnect/restart the provider client to load the registration, then verify the setup:

1. Discover the actual tool namespace and schemas. For a newly created profile, `status`
   should still report that Chrome is stopped and no window should have opened.
2. Use a browser tool to open a harmless page, read a snapshot, and verify its content.
   This first browser connection starts Chrome automatically.
3. Open the intended site's login page for the person, either through the browser tools
   or `open --url`. Let them sign in in the window, then verify authenticated content
   and the intended account. `status` does not verify a site's login.
4. End the provider turn and reconnect. Browser work should reuse the same profile,
   logins, and tabs. Keep Chrome open for pending human work; use `stop` when the entire
   session is finished and safe to close.

## Add a named profile

Choose a separate profile for a different account or concurrent work. Reuse the installed
dependencies and the same helper:

```bash
enso browser create work
enso browser mcp work --print-config
```

This prints the `enso-browser-work` registration. Add it only to the provider/workspace
selection intended to use that profile, then repeat discovery and the first-use check
above. Creation and registration do not open Chrome. To open it explicitly for human
login before an agent browses:

```bash
enso browser open work --url https://example.com
```

Record the intended profile in that workspace's `AGENTS.md` when useful; the profile
name is a routing choice, not proof of the signed-in account. Each extra profile needs
its own selected registration. Concurrent agents need different profiles and different
workspace/provider server selections; merely giving profiles different names does not
isolate MCP clients. Serialize browser tasks within one shared selection.

## Existing installations

Registrations that launch `skills/enso-browser/scripts/browser.py` must be replaced once
with the output of `enso browser mcp [profile] --print-config`, then the provider reconnected.
Preserve each registration's profile, custom home, Chrome override, and provider-specific
settings. Saved profiles, tabs, and logins stay in place; do not recreate them.
The implementation now ships in the Python package and updates with Enso's code. Managed
updates retire an untouched old helper and refresh untouched instructions, preserving edits
and historical copies. Merge customized instructions manually to use the public CLI.
