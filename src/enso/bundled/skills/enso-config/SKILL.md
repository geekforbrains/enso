---
name: enso-config
description: Inspect or change Enso configuration, models, provider arguments, and service settings; diagnose installation health, logs, and reload requirements.
---

# Configuration

```bash
enso config show                 # tokens redacted
enso config check --json         # validation and current revision hash
enso providers --json            # bundled provider/model/effort choices, offline
enso doctor
enso logs --turn TURN_ID         # or --job WORKSPACE:JOB
enso service status
enso web status
```

Use `enso config set PATH VALUE` or `unset PATH` for installation keys. Values parse as
JSON, otherwise text. `enso config apply --file FILE` replaces the complete document;
never use redacted `config show` output as that document. Pass `--expected-hash HASH`
from the prior check when a write depends on that read. Inspect the result and preserve
unrelated settings.

Settings have separate owners:

- `$ENSO_HOME/config.json`: transports, bindings, default agent, providers, service options.
- `workspaces/<name>/workspace.json`: optional complete `agent` triple (`provider`, `model`,
  `effort`) and `providers.<provider>.args`. Edit directly, then `enso config check`.
  An argument list replaces the global list; `[]` passes no extra arguments. Omission inherits.
- `projects/<KEY>/PROJECT.md` and `jobs/<job>/JOB.md` inside a workspace: load
  `enso-projects` or `enso-jobs`. Jobs and beats retain their own saved agent choices.

Load `enso-workspace` for bindings and `enso-security` before changing provider permissions
or handling credentials. Use `enso models` for OpenRouter model IDs when configuring OpenCode.
The [configuration reference](https://github.com/geekforbrains/enso/blob/main/docs/configuration.md)
owns accepted settings; CLI help owns command syntax.

Changes normally apply on the next turn or scheduler tick; running work keeps its snapshot.
`transports` and `logging` need a service restart; `web` needs a viewer restart. Config writes
report `restart_required`; report the specific requirement. A provider directory newly added
to the service's PATH requires operator service installation, not just a config edit.

Do not run setup, serve, service lifecycle, or viewer lifecycle commands from an Enso turn
or job. Leave those to an external operator session; read-only status is safe. Use
`enso-update` for release changes. Runtime receipts, maintenance gates, and locks are
Enso-owned state, not repairable configuration.
