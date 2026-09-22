# Optional browser requirements

Browser work requires Microsoft's **Playwright CLI** (`@playwright/cli`), a supported
Node.js release, and a Playwright-supported browser. Google Chrome is a convenient choice
for headed login and review. Use a current Node.js LTS release compatible with the installed
CLI. The ordinary Enso installation does not install or update these tools.

The operator installs the CLI separately, following the
[official instructions](https://github.com/microsoft/playwright-cli):

```bash
npm install -g @playwright/cli
playwright-cli --version
playwright-cli --help
```

Check the environment that runs the agent: both `node` and `playwright-cli` must be on
its `PATH`, including Enso's background service. An interactive shell's version manager
may supply a different PATH. After installing tools, the operator may need to refresh
the service's environment using Enso's documented service installation workflow.

If the command is absent, explain the missing requirement. Do not install packages,
download browsers, or change service configuration as an incidental browsing step;
a user request to set up browser tooling covers the relevant installation. Enso does
not pin or manage the CLI version, and its Python environment is not used for browser work.

The installed CLI's `--help` is the command reference. Its optional upstream skill is
not required by `enso-browser`. If a selected browser is absent, consult
`playwright-cli install-browser --help` during the authorized setup. A graphical desktop
is required for `--headed`; do not silently switch a human-login handoff to headless mode.
