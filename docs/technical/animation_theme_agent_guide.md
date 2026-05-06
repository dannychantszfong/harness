# Animation Theme Agent Guide

This guide is for coding agents invoked by `harness animation-theme`.

## Goal

Customize the quiet terminal animation vocabulary to match the user's requested theme while keeping the harness tasteful and readable.

## Edit Target

Primary file:

- `harness/ui/spinner.py`

Primary structure:

- `PHRASES["playful"]`

You may update tests or docs only if the changed vocabulary requires it.

## Style Rules

- Use short single verbs only.
- Prefer Title Case verbs, for example `Scrying`, `Warding`, `Inscribing`.
- Keep it restrained: fantasy, ritual, arcane, elemental, cyber-mystic, or whatever theme the user asked for, but not loud.
- Do not add suffixes such as `with Claude Code`, `with Codex`, or `the repo`.
- Avoid joke words like `Cooking`, `Vibing`, or memes.
- Do not add full phrases unless the user explicitly asks for phrase-based animation.
- Keep every verb easy to scan in a terminal.

## Technical Rules

- Preserve the existing `PHRASES` shape:
  - `planning`
  - `coding`
  - `evaluating`
  - `waiting`
- Keep `PHRASES["steady"]` plain and utilitarian unless the user specifically requests otherwise.
- Keep each `PHRASES["playful"][phase]` tuple non-empty.
- Keep text output log-safe: no ANSI escape tricks, no multiline animation text, no destructive terminal control.
- Do not modify generated `output/`, logs, cache directories, or user planning notes.
- Run at least `python -m pytest tests/test_spinner.py`.

## Final Response

Summarize:

- the theme interpreted
- the verb pools changed
- tests run
