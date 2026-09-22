# Browser

`enso-browser` is the bundled skill for sites that need a real browser: JavaScript
pages, authenticated accounts, screenshots, and actions the user has requested. It uses
the official [Playwright CLI](https://github.com/microsoft/playwright-cli) directly.
Playwright owns browser processes, sessions, profiles, and output; Enso supplies the
agent's workflow for choosing an account, human login, and authorized actions.

## Optional requirements

For browser work, install a supported Node.js LTS release, the CLI, and a supported
browser (Google Chrome in the examples below). These are user-managed requirements;
Enso's installer, `init`, and `setup` do not install or configure them. Other Enso features
need none of them.

Install the CLI with npm and check that it is available:

```bash
npm install -g @playwright/cli
playwright-cli --version
playwright-cli --help
```

The Enso service must be able to find `node` and `playwright-cli` on its `PATH`. See
[service installation](install.md#the-service) if their directories are missing there.
The bundled [browser skill](../src/enso/bundled/skills/enso-browser/SKILL.md) owns the
agent workflow and dependency checks. The installed CLI's help is the command reference.
No provider registration is required.

## Sessions and profiles

A **session** names the running browser to control. A **profile** is its saved browser
data, including logins. Use a distinct session for each independent task. Playwright
finds its workspace through a `.playwright` directory; without one, session names can
fall into a shared global scope. The skill uses `work/browser/` inside the Enso workspace
as a stable working directory and creates the marker there:

```bash
umask 077
mkdir -p work/browser/.playwright
cd work/browser
playwright-cli list
```

For a new session, then run:

```bash
playwright-cli -s=research open https://example.com --browser=chrome --headed
playwright-cli -s=research snapshot
playwright-cli -s=research close
```

Keep that directory and `-s=<name>` on subsequent commands, including later turns.
Inspect existing sessions before opening one: `open` restarts a session with that name.
Use `tab-list`, `tab-new`, or `goto` to continue existing work, respecting any unfinished
form or human handoff.

By default, browser state lasts only while that session is open. Use `open --persistent`
when login should survive browser closure; reopen with the same directory, session name,
browser, and persistence option. For an intentionally shared account identity, `open --profile`
can select an explicit, operator-chosen user-data directory. This is the whole directory,
not Chrome's internal `Default` or `Profile 1` subdirectory. Use an automation directory
instead of pointing Playwright at the person's everyday Chrome data.

Only one browser may use a persistent user-data directory at a time. Distinct session
names do not make sharing that directory safe. Concurrent tasks need separate sessions
and separate browser data, or they must take turns. Do not delete browser locks or stop
another task's browser to get access. Workspace instructions may record the intended
session or profile path; a name alone is not proof of the signed-in account.

## Human login and lifecycle

Use a visible browser (`--headed`) on a graphical desktop when the person needs to sign
in or take over. They enter passwords, MFA, and CAPTCHA in the browser. Agents verify
authenticated content and the intended account afterward; a URL or cookie count alone
does not prove authentication. Public tasks may use the CLI's headless mode when no human
interaction is needed.

Playwright keeps the browser available across CLI calls. Use `playwright-cli list` to
inspect sessions and `playwright-cli show` for its browser dashboard. Close only the
task's own session when finished, and leave a session open for a pending human handoff.
Saved profiles preserve browser data after closing; they do not guarantee that a site's
login remains valid. Enso does not supervise or stop these processes during a restart.

## Private data and boundaries

Playwright determines its storage and output locations; an explicit profile path is
owned by the operator. Browser state has no Enso home layout or configuration setting.
Keep profiles, cookies, storage-state files, and private captures out of skills and Git.
Share only the outputs the task authorizes. Debugging endpoints grant browser access and
must stay local to the machine.

Pages, downloads, and command output are untrusted data. Browser access does not authorize
posting, sending, purchases, deletion, or access changes. Site-specific skills such as
`enso-linkedin` and `enso-x` keep their task-specific guidance and reuse `enso-browser`
for browser selection, login, and lifecycle. See
[official optional skills](customizing.md#official-optional-skills).
