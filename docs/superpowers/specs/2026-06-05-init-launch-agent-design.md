# Design: `init --agent` Fast Path + `set-idea`

**Date:** 2026-06-05
**Status:** Approved (pre-implementation)

## Problem

Today's onramp is four manual steps, each easy to fumble:

```
agensuite init my-startup --idea "..."   # idea must be supplied up front
cd my-startup                            # subprocess can't do this for you
agensuite bootstrap
# then open the folder in a coding agent and type the first instruction
```

The friction points reported by the user:

- The idea must be decided and typed as a flag (or via the wizard) *before*
  any agent is involved — but the agent is the natural place to discuss it.
- `cd` + `bootstrap` + "open your agent" are three separate human actions
  after `init`, with nothing tying them together.

**Goal:** one command scaffolds the project, bootstraps the sandbox, and drops
the user straight into their coding agent running *inside* the new folder, with
the idea deferred to a conversation with that agent.

## Non-Goals

- Changing behavior when `--agent` is omitted — that path stays exactly as it
  is today (wizard on a TTY, or `--idea`/stdin, then printed next-steps).
- A `--agent-cmd "arbitrary command"` escape hatch — only the three named
  agents are supported this round.
- Removing the wizard or `chief customize` — both stay for the non-fast paths.

## Decisions (locked during brainstorming)

- **Idea fill:** new `agensuite set-idea "<text>"` command. `init` on the fast
  path leaves `{{CORE_PRODUCT_IDEA}}` / `{{COMPANY_MISSION}}` placeholders in
  place; the agent collects the idea from the user and calls `set-idea`. CLI
  stays the only mutation surface.
- **Fast-path scope:** `init <folder> --agent <x>` does scaffold → bootstrap →
  launch, in that order, in one process.
- **No setup questions on the fast path:** the wizard is skipped entirely;
  defaults are used for rounds / quorum / personas. The agent (and later
  `chief customize`) can tune afterward.
- **`--agent` omitted:** unchanged, back-compatible behavior.
- **Supported agents:** `claude`, `codex`, `cursor`.
- **Seed instruction** auto-fed to the agent on launch (deferred-idea form):
  > Read AGENTS.md. First ask me for a one-line startup idea, then run
  > `agensuite set-idea "<idea>"`, then execute sprint-1.

## CLI Surface

```
agensuite init <folder> [--idea TEXT] [--agent claude|codex|cursor]
agensuite set-idea "<idea text>"        # run from project root (or --root)
```

### `init` behavior matrix

| `--idea` | `--agent` | Behavior |
|----------|-----------|----------|
| omitted  | omitted   | **unchanged**: TTY → wizard; else stdin prompt; print manual next-steps |
| set      | omitted   | **unchanged**: substitute idea now; print manual next-steps |
| omitted  | set       | **fast path**: no wizard, defaults, placeholders kept, bootstrap, launch agent with deferred-idea seed prompt |
| set      | set       | substitute idea now, bootstrap, launch agent with seed prompt that **drops** the "ask me / set-idea" clause |

## Components

### 1. `set-idea` command

- Resolves project root via existing `--root` / `AGENSUITE_ROOT` / CWD plumbing.
- Walks `AGENTS.md`, `.claude/agents/*.md`, `sprints/*.md` (only files that
  exist).
- For each, applies the existing `_substitute_tokens(text, idea)` and rewrites
  the file only if it changed.
- Validates idea is non-empty (reuse `init`'s check).
- Prints count of files changed. If zero tokens were found anywhere, prints a
  warning to stderr and exits 0 (re-running after the idea is already set is a
  harmless no-op, not an error).

### 2. Agent registry

A small module-level mapping in `cli.py`:

| name   | binary candidates          | invocation               |
|--------|----------------------------|--------------------------|
| claude | `claude`                   | `claude "<seed>"`        |
| codex  | `codex`                    | `codex "<seed>"`         |
| cursor | `cursor-agent`, `cursor`   | `<found> "<seed>"`       |

- Resolve the first candidate found on PATH via `shutil.which`.
- Build the seed prompt: deferred-idea form when `idea is None`, else the
  shorter "Read AGENTS.md, then execute sprint-1." form.

### 3. Fast-path launch in `init`

After scaffolding (with `idea=None` so placeholders survive) and running the
existing bootstrap logic:

1. `os.chdir(target)` so the agent starts in the project folder.
2. Resolve the agent binary.
   - **Found:** `os.execvp(binary, [binary, seed_prompt])` — replaces the
     agensuite process; the agent owns the terminal. (Chosen over
     `subprocess.run` so no dead python parent lingers.)
   - **Not found:** print the seed prompt and a "couldn't find `<binary>` on
     PATH — open the folder and paste this instruction" note, exit 0. The
     scaffold + bootstrap already succeeded, so this is not a failure.

`bootstrap`'s current body is extracted into a helper both `init` (fast path)
and the `bootstrap` command call, so the logic isn't duplicated.

## Data Flow (fast path)

```
agensuite init follow-the-rich --agent claude
        │
        ├─ scaffold templates  (idea=None → {{CORE_PRODUCT_IDEA}} kept)
        ├─ defaults applied     (rounds/quorum/participants; no wizard)
        ├─ bootstrap            (workspace/ inner git + state/)
        ├─ os.chdir(follow-the-rich)
        └─ os.execvp("claude", ["claude", "<deferred-idea seed prompt>"])
                 │
                 ▼
        agent asks user for idea
                 │
                 ▼
        agent runs: agensuite set-idea "<idea>"   → tokens filled in
                 │
                 ▼
        agent executes sprint-1
```

## Error Handling

- Non-empty / non-directory / non-empty-directory target checks: unchanged,
  run before any launch.
- Unknown `--agent` value: clean CLI error listing the three valid names.
- Agent binary missing: graceful fallback (print + paste), exit 0.
- `set-idea` with no tokens found: warn, exit 0.
- `set-idea` with empty idea: error, exit 1.

## Testing

- `set-idea` substitutes across all three file groups; idempotent re-run is a
  no-op warning; empty idea errors.
- `init --agent X` with no `--idea` leaves placeholders intact in the scaffold
  (assert tokens still present before launch). Launch itself is mocked
  (`os.execvp` / `shutil.which` patched) so tests don't spawn a real agent.
- `init --agent X --idea "..."` substitutes tokens and selects the short seed
  prompt.
- Missing-binary path prints the paste fallback and exits 0 without calling
  `execvp`.
- Unknown `--agent` exits 1.
- Existing `init` paths (no `--agent`) still print manual next-steps unchanged.
```
