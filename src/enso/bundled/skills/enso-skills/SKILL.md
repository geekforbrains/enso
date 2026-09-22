---
name: enso-skills
description: Find, install, create, or refine Enso skills. Use when asked to add, edit, split, or shorten a skill or choose its scope and supporting files.
---

# Skills

## Find and install

```bash
enso skill list
enso skill list --available
enso skill show NAME
enso skill install NAME
```

Install from Enso's official catalog after reading the skill and requirements. Skills add
instructions, not tools, credentials, or authorization. Installation refuses existing
paths; preserve local changes and `.enso-skill.json`. Optional skills do not auto-update.

## Create and refine

Search installed skills first; refine the existing owner before creating another. Choose
`$ENSO_HOME/workspaces/<workspace>/skills/<name>/` for one workspace or
`$ENSO_HOME/skills/<name>/` for all. Check both scopes for collisions. Write in `skills/`,
not provider discovery links; provider user skills outside Enso need a separate request.

`SKILL.md` needs valid YAML frontmatter with `name` and `description`. Match the directory
and `name`: 1–64 lowercase letters, digits, or single hyphens, with no leading or trailing
hyphen. User names must not be `enso` or start with `enso-`. Quote values containing `: `.

The description says what the skill does and when to use it, in words a request would use.
The body contains the procedure and decisions the agent would get wrong without it.
One skill owns one task; refer to another owner instead of copying its steps.

Start with only `SKILL.md`. Add supporting files only when useful:

- `references/`: conditional procedures, schemas, or tables. Link each directly from
  `SKILL.md` and say when to read it. Keep essential gotchas in the entrypoint.
- `scripts/`: repeated parsing, checks, or transformations. Give the exact command; accept
  flags or stdin without prompting, with results on stdout and diagnostics on stderr.
  Keep a single existing CLI command inline. Use public CLIs, not private `enso.*` imports.
- `assets/`: substantial templates or data to copy; short templates stay inline.

Cut sentences that do not change a decision: ordinary tool definitions, duplicated facts,
encouragement, and empty sections. Keep the entrypoint to what every activation needs;
500 lines is a ceiling, not a target.

Keep credentials and private material out of reusable skills. Test helpers on scratch data
and run `enso workspace audit <workspace>`. The next turn discovers edits without a restart.
