```
 █████╗  ██████╗ ███████╗███╗   ██╗███████╗██╗   ██╗██╗████████╗███████╗
██╔══██╗██╔════╝ ██╔════╝████╗  ██║██╔════╝██║   ██║██║╚══██╔══╝██╔════╝
███████║██║  ███╗█████╗  ██╔██╗ ██║███████╗██║   ██║██║   ██║   █████╗
██╔══██║██║   ██║██╔══╝  ██║╚██╗██║╚════██║██║   ██║██║   ██║   ██╔══╝
██║  ██║╚██████╔╝███████╗██║ ╚████║███████║╚██████╔╝██║   ██║   ███████╗
╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝╚══════╝ ╚═════╝ ╚═╝   ╚═╝   ╚══════╝
```

A **universal multi-agent C-suite template** for executing structured,
evolutionary sprints inside any generic software startup. A coding agent
(Claude Code, Codex, Cursor, …) reads `AGENTS.md` and then *acts out* a
C-suite simulation: it spawns one subagent per role (CEO, CPO, CTO, CDO,
CCO), drives a turn-based debate via pull requests, and produces real git
artifacts in a sandboxed inner repository.

The framework is **idea-agnostic**. You supply your product thesis once
(`--idea`), and the C-suite reasons about it; nothing in the template
assumes a domain. The CEO subagent extends the project itself by authoring
the next sprint blueprint after each ADR, so the loop keeps running without
a hand-curated roadmap.

There is **no LLM client inside this codebase**. The orchestrator is the
coding agent itself; the Python code is plumbing — schemas, a git
subprocess wrapper, JSON state files, and a thin CLI that owns every state
mutation.

## Mental model: you don't run *this* repo

This repository is the **`agensuite` tool**, not your project. You don't
clone it and work inside it. Instead you install the tool once, then use
its `init` command to **scaffold a single project folder** that contains
*your* startup's `AGENTS.md`, role playbooks, and first sprint — with your
idea already baked in. You then open *that one folder* in your coding agent.

It's **one folder, not two.** `init` creates `my-startup/`; `bootstrap` then
adds two subfolders *inside* it — `workspace/` and `state/`. You open
`my-startup/` and never touch those two by hand; the agent drives them.

```
agensuite (the installed tool)  ──init──►  my-startup/   ◄── you open THIS in your agent
                                           ├── AGENTS.md           (the contract)
                                           ├── .claude/agents/*.md (CEO/CPO/CTO/CDO/CCO personas)
                                           ├── sprints/sprint-1.md (your idea, substituted in)
                                           ├── workspace/          ← added by bootstrap
                                           └── state/              ← added by bootstrap
```

**What is `workspace/`?** The simulation produces *real git history* —
branches, PRs, merges, an ADR per sprint. That all happens inside
`workspace/`, a **nested git repo kept separate from `my-startup/`'s own
git**, so the simulated company's churn never pollutes your project's
history. `state/` holds the JSON the CLI uses to track PRs and the debate.
Both are managed entirely by the agent through the `agensuite` CLI — you
just read the results.

## Quickstart (no clone)

`agensuite` is on PyPI — install it like any CLI, nothing to clone.

**1. Install the `agensuite` tool once** (puts `agensuite` on your PATH):

```bash
uv tool install agensuite
# or:  pipx install agensuite
# or:  pip install agensuite
```

> One-shot alternative: `uvx agensuite init my-startup --idea "…"`. Note
> `uvx` is *ephemeral* — the binary disappears after the command, so you'd
> have to prefix every later `agensuite` call the same way. The persistent
> install above is simpler because the workflow runs many `agensuite`
> commands. Omitting `--idea` on a terminal launches the guided setup wizard.

**2. Scaffold your project** (substitutes your idea into every template).
Pick a folder name that *doesn't exist yet* — `init` creates it for you.
Don't `mkdir` it first; `init` refuses a non-empty folder.

```bash
agensuite init my-startup        # guided setup wizard (idea, persona biases, sprint config)
# non-interactive alternative:
agensuite init my-startup --idea "A marketplace for renting camera gear between creators"
```

Then enter the folder `init` just created:

```bash
cd my-startup
```

**3. Bootstrap the sandbox** (adds the nested `workspace/` git repo + `state/` *inside* `my-startup/`):

```bash
agensuite bootstrap
```

**4. Open `my-startup/` in your coding agent** and prompt it with:

> Read AGENTS.md and execute sprint-1.

That's it. Optionally tweak `.claude/agents/<role>.md` first, or use
`agensuite chief customize <role> --focus "…"` to bias a persona without
opening files.

### What happens when you open it in the agent

Following the `AGENTS.md` contract, the agent plays the **CEO** and:

1. Spawns one subagent per spoke role (CPO, CTO, CDO, CCO), each opening a
   PR with its domain's view of the MVP — all via the `agensuite` CLI.
2. Drives a **turn-based debate** across the PRs (review → rebuttal →
   follow-up), enforced by the CLI's schedule and reviewer-author guards.
3. **Pauses at a human gate** so you can resolve deadlocks ([m]erge /
   [r]eject / [a]dr-options / [s]kip).
4. Merges approved PRs and writes an **ADR** into the inner repo's
   `governance/`.
5. Asks the CEO subagent to author the next blueprint into
   `sprints/sprint-2.md` — then the loop repeats.

## Layout (inside a scaffolded project)

| Path | Purpose |
|------|---------|
| `AGENTS.md` | Contract read by any coding-agent platform on setup. |
| `.claude/agents/{role}.md` | Native subagent playbooks — one per C-suite role (YAML frontmatter + persona body). |
| `sprints/sprint-*.md` | Sprint definitions (YAML frontmatter + body). Ships with `sprint-1.md` only; subsequent sprints are authored by the CEO at runtime. |
| `workspace/` (gitignored) | The inner simulated git repo. |
| `state/` (gitignored) | JSON registries for PRs and debate transcripts. |

## CLI

Run `agensuite --help` to see every subcommand, or jump to the reference
section at the bottom of a scaffolded `AGENTS.md`.

## Developing the framework itself

Only needed if you want to change the tool (CLI, schemas, templates) — not
to use it. Clone and install editable:

```bash
git clone https://github.com/omri-am/agensuite
cd agensuite
pip install -e ".[dev]"
pytest
```

The templates the `init` command ships live under
`src/agensuite/templates/`. Edit those to change what every scaffolded
project starts with.

## License

MIT — see `LICENSE`.
