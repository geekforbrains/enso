# {{workspace_name}}

## Purpose

<!-- What is this workspace for? One or two sentences. -->

## Scope

<!-- What is in bounds, and what is explicitly not. -->

## Terms

<!-- Words that mean something specific here. Delete if none. -->

## Rules

<!-- Anything that must be true on every single turn: approvals, tone,
     people to check with, things never to touch. Delete if none. -->

## Files

- `$ENSO_HOME/shared/knowledge/` — current facts and reference material shared across workspaces; load `enso-knowledge` and use `enso knowledge` to find or maintain it
- `memory/` — dated conversations and experiences; load `enso-memory`, search with `enso memory search`, and inspect sources before recalling earlier work
- `work/` — task files and generated or editable output, grouped by task
- `uploads/` — chat attachments, written by Enso

Knowledge commands use shared knowledge by default. Keep work in its established repository or destination when one exists; otherwise use `work/`. Memory belongs to this workspace. People sharing it share maintained memory; separate workspaces do not promise confidentiality within this installation.
