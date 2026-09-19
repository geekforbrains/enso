---
name: enso-skills
description: Find, install, create, or refine Enso skills.
---

# Skills

Inspect existing skills; refine one instead of duplicating it.

## Find and install

```bash
enso skill list
enso skill list --available
enso skill show NAME
enso skill install NAME
```

Install only from Enso's official catalog. Read the skill and its requirements first. A
skill adds instructions, not tools, authentication, permissions, or authorization.

Installation refuses an existing directory. Optional skills do not update automatically;
preserve `.enso-skill.json` and local changes.

## Create and refine

Choose the narrowest scope:

- `$ENSO_HOME/workspaces/<workspace>/skills/<name>/` for one workspace.
- `$ENSO_HOME/skills/<name>/` for every workspace.

Check both scopes for collisions. User names must not be `enso` or start with `enso-`.
Do not write through provider discovery links or into provider user skills without a
separate request.

Keep the directory minimal. `SKILL.md` needs matching `name` and `description` frontmatter;
the description says what the skill does and when it applies. Keep the body to non-obvious
decisions and procedure. Add linked `scripts/`, `references/`, or `assets/` only when needed.

Keep credentials, account identifiers, and private material out of reusable skills. Test
helpers with scratch data, then run `enso workspace audit <workspace>`. The next turn
discovers the skill; no restart is needed.
