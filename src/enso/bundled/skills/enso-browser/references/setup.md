# Browser setup

Browser work needs a supported Node.js LTS, Microsoft's `@playwright/cli`, and a supported
browser. Google Chrome works for headed login. These are optional, user-managed dependencies:

```bash
npm install -g @playwright/cli
playwright-cli --version
playwright-cli --help
```

Follow the [official installation instructions](https://github.com/microsoft/playwright-cli)
and installed CLI help. For a missing browser, inspect `playwright-cli install-browser --help`.
Install tools or change service configuration only when setup is within the user's request;
otherwise report the missing requirement.

Both `node` and `playwright-cli` must be on the Enso service's PATH; a shell version manager
may expose a different environment. Use the operator's service installation workflow to
refresh it when needed. Enso neither installs nor pins these tools; its Python environment
and provider registration are unrelated.

Human handoff requires a graphical desktop. Do not silently replace headed login with a
headless session. Playwright's optional upstream skill is not required.
