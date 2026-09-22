# Project definitions

Inspect with `enso project list`; create with `enso project add`. Keys are unique across
the installation; each project belongs to one workspace and may omit a repository.

Before editing `$ENSO_HOME/workspaces/<workspace>/projects/<KEY>/PROJECT.md`, inspect its
tasks, stage jobs, and repository instructions. Preserve custom scripts and instructions.
Stage jobs belong to the project's workspace.

Project commands start beside `PROJECT.md`; scripts working on task code must enter
`ENSO_TASK_DIR` themselves. Invoke scripts explicitly or make them executable.
