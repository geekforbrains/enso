# Projects

Use `enso project list` to inspect projects and `enso project add` to create one. A project
belongs to one workspace, its key is unique across the installation, and a repository is
optional.

Each project is defined by `projects/<KEY>/PROJECT.md` in its workspace. Before changing
one, inspect that file, its existing tasks and stage jobs, and any repository instructions.
Preserve custom instructions and scripts.

Project commands and scripts start beside `PROJECT.md`. A script that operates on task code
must enter `ENSO_TASK_DIR` itself. Make directly invoked scripts executable. Stage jobs
belong to the same workspace as the project.

Project and task commands use the selected workspace. Use `--workspace` for another one
and `--all-workspaces` only for a deliberate installation-wide listing.
