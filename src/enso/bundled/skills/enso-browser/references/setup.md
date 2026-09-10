# Optional browser setup

This setup is needed only when the user wants browser automation. It installs a pinned
Microsoft Playwright MCP server into the Enso home, uses an existing Google Chrome,
and connects one profile to the selected agent provider. Keep provider configuration
outside this helper: `mcp --print-config` prints a proposal and never writes it.

## Dependencies

Install Google Chrome and a supported Node.js release (Node 22 or newer is recommended;
MCP requires Node 18+). macOS and Linux with a graphical desktop are supported. On Linux,
`google-chrome` or `google-chrome-stable` must be on `PATH`; macOS checks the standard
system and user Applications locations. For a nonstandard Chrome install, set
`ENSO_BROWSER_CHROME` to the absolute executable path; the printed MCP proposal carries
it into the registration environment. Enso's background service must find `node` on its `PATH`.

Inspect the target directory before installing. Resolve the helper and Enso's Python as
described in `SKILL.md` (the commands below use the managed install). With the user's
browser-setup request covering dependency installation, run this explicit pinned install;
never substitute
`@latest`, `npx` with an implicit download, or a global npm install:

```bash
enso_home="${ENSO_HOME:-$HOME/.enso}"
enso_python="$enso_home/runtime/current/bin/python"
enso_browser="$enso_home/skills/enso-browser/scripts/browser.py"
"$enso_python" "$enso_browser" create
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install \
  --prefix "$enso_home/browser/tooling" --save-exact --ignore-scripts \
  --no-audit --no-fund @playwright/mcp@0.0.80
```

Run the helper first and stop if it fails: it creates private directories and refuses
symlinks in browser/profile/output/state/tooling paths before npm can write there.
Use the shell runner's timeout for the network step (for example, 120 seconds), inspect
its result, and stop on failure. The helper itself never downloads anything and requires
exactly this version. Keep the generated package manifest and lockfile in `tooling/`;
updates to this pin should review upstream changes and verify attachment against Chrome.
No Playwright browser download or Python Playwright package is needed.

Version 0.0.80 and its `--cdp-endpoint`, `--caps`, `--output-dir`, and
`--output-max-size` flags were checked against the
[Microsoft release documentation](https://github.com/microsoft/playwright-mcp/tree/v0.0.80)
and [package metadata](https://github.com/microsoft/playwright-mcp/blob/v0.0.80/package.json).
Chrome requires a nondefault user-data directory for remote debugging; this helper always
uses one. See [Chrome's remote debugging guidance](https://developer.chrome.com/blog/remote-debugging-port).

## Connect a profile

Choose the Python that runs Enso and this installed skill's path. For managed installs:

```bash
enso_home="${ENSO_HOME:-$HOME/.enso}"
enso_python="$enso_home/runtime/current/bin/python"
enso_browser="$enso_home/skills/enso-browser/scripts/browser.py"
"$enso_python" "$enso_browser" mcp --print-config
"$enso_python" "$enso_browser" mcp work --print-config
```

The first prints a standard MCP JSON registration named `enso-browser-default`; the
second prints `enso-browser-work`. Both contain an absolute Python command, helper path,
profile argument, and `ENSO_HOME`. For an unmanaged/source install, invoke the helper
with its environment's Python; the proposal uses that Python when no managed runtime exists.

Add only the intended profile to the provider/workspace used for this browser work,
preserving its existing MCP servers. Providers can eagerly start every registered MCP
server on every turn. Registering several profiles globally can launch and lock them all,
even on turns that do not browse. Prefer a workspace-scoped single-profile registration,
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
"$enso_python" "$enso_browser" mcp default
```

That command speaks MCP over stdin/stdout. Do not run it as a normal diagnostic and
wait for human-readable output. It starts or reuses that profile's persistent Chrome,
then replaces itself with the installed MCP server attached to Chrome. It downloads
nothing. `mcp --print-config` is the inspectable alternative and starts no browser.

Reconnect/restart the provider client to load the registration. Discover its actual tool
namespace and schemas, open a harmless page, read a snapshot, and verify its content.
Only after tools work, open the intended site's login page for the person. Each extra
profile needs its own selected registration. Concurrent agents need different profiles
and different workspace/provider server selections; merely giving profiles different names
does not isolate MCP clients. Serialize browser tasks within one shared selection.
