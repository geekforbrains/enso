---
name: Enso audit
schedule: "0 3 * * *"
agent:
  provider: "{{provider}}"
  model: "{{model}}"
  effort: "{{effort}}"
enabled: true
gate:
  command: bash prerun.sh
catch_up: true
---

`enso doctor --json --attention` found something worth telling the operator about this Enso
installation. `ok` says whether it is healthy; a report with `"ok": true` carries no health
problem at all, only findings worth a mention. The report:

```json
{{gate_output}}
```

Write a short summary for the operator, who is probably reading it on a phone:

- One line per problem, in plain words: what is wrong and what it stops (a chat turn, a job, a beat, an alert).
- Say which problems `enso workspace audit --fix` would repair; the doctor marks those with "(repairable with `enso workspace audit --fix`)". Everything else needs a hand.
- Layout findings (`unexpected`, `link`, `permissions`, `stale`) stop nothing on their own; say what the path is and what the operator would do about it. An unexpected entry is a file Enso does not place and will not touch. A `permissions` finding means other users on this machine can read credentials. A `stale` file is safe to delete, and nothing deletes it for them. Do not guess what an unexpected file is for.
- A `script` finding names a project command whose `./script` beside `PROJECT.md` is missing or not executable; say which file to write or make executable. Nothing creates it for the operator.
- A job problem names its `JOB.md` in brackets. Quote that path and say which frontmatter field to edit. Nothing repairs a job for the operator, and a schedule is never guessed or rewritten: it is five cron fields, `minute hour day-of-month month day-of-week`.
- A heartbeat problem names an `HB-…` reference. Read `enso heartbeat show REF --json` and only the relevant history if needed, then explain what is waiting or failing. Do not retry its actions, advance a checkpoint, or change its state.
- Knowledge findings name note paths and give counts. If the summary is truncated, use `enso knowledge audit --json` for shared knowledge or `enso knowledge audit --workspace NAME --json` for retained workspace notes. These are structural checks, not proof that a note is true or correctly filed.
- If there are warnings, one closing line covering them. When the whole report is warnings, say
  plainly that nothing is broken.

Fix nothing yourself: do not run `--fix`, and do not edit `config.json` or any other file. The operator decides what to do.

Send the summary with `enso message send -` (the text on stdin; without `--to` it goes to the transport's notify target). Then print the same summary as your final output, so run history has it too.
