# Project definitions

Inspect with `enso project list`; create with `enso project add`. Keys are unique across
the installation; each project belongs to one workspace and may omit a repository.

Before editing `$ENSO_HOME/workspaces/<workspace>/projects/<KEY>/PROJECT.md`, inspect its
tasks, stage jobs, and repository instructions. Preserve custom scripts and instructions.
Stage jobs belong to the project's workspace.

Project commands start beside `PROJECT.md`; scripts working on task code must enter
`ENSO_TASK_DIR` themselves. Invoke scripts explicitly or make them executable.

Use `workflow: 2` and start with `enabled: false`. `stages` declares the order; named
`paths` select applicable subsets. A single `work` stage is valid. Each stage declares
accepted inputs (default `request`), output expectations, instructions and its executor kind.
Agent configuration belongs to each bound stage job. Validate, then explicitly activate with
`enso workflow enable KEY`. Old definitions and tasks remain preserved as paused legacy work.
