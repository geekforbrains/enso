---
name: Enso audit
schedule: "0 3 * * *"
provider: "{{provider}}"
model: "{{model}}"
effort: "{{effort}}"
enabled: true
prerun: prerun.sh
catch_up: true
---

`enso doctor --json` found problems with this Enso installation. The report:

```json
{{prerun_output}}
```

Write a short summary for the operator, who is probably reading it on a phone:

- One line per problem, in plain words: what is wrong and what it stops (a chat turn, a job, a beat, an alert).
- Say which problems `enso workspace audit --fix` would repair; the doctor marks those with "(repairable with `enso workspace audit --fix`)". Everything else needs a hand.
- A job problem names its `JOB.md` in brackets. Quote that path and say which frontmatter field to edit. Nothing repairs a job for the operator, and a schedule is never guessed or rewritten: it is five cron fields, `minute hour day-of-month month day-of-week`.
- A heartbeat problem names an `HB-…` reference. Read `enso heartbeat show REF --json` and only the relevant history if needed, then explain what is waiting or failing. Do not retry its actions, advance a checkpoint, or change its state.
- If there are warnings, one closing line covering them.

Fix nothing yourself: do not run `--fix`, and do not edit `config.json` or any other file. The operator decides what to do.

Send the summary with `enso message send -` (the text on stdin; without `--to` it goes to the transport's notify target). Then print the same summary as your final output, so run history has it too.
