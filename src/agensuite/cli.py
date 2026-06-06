"""agensuite CLI — the only mutation surface for orchestrating coding agents.

Every state-changing operation flows through one of these subcommands so that
any coding-agent platform (Claude Code, Codex, Cursor, ...) can drive the
same workflow identically by shelling out to ``agensuite ...``.

Exit-code convention:

* ``0`` — success (also: bare ``agensuite`` invocation that prints banner +
  help, since "user asked for help and got it" is not a failure)
* ``1`` — generic / user error (unknown PR, sprint not found, schema
  validation failure, missing quorum, ...)
* ``2`` — merge conflict (so orchestrators can branch on ``$?`` and treat
  conflicts as recoverable rather than fatal)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from importlib import resources
from importlib.abc import Traversable
from pathlib import Path
from typing import Optional

import typer
import yaml

from . import digest as _digest
from .git_engine import GitCommandError, GitEngine, MergeConflict
from .models import (
    DebateState,
    DebateTurn,
    DecisionRecord,
    Message,
    MessageType,
    PRStatus,
    PullRequest,
    ReviewComment,
    SprintConfig,
    TurnPhase,
    Verdict,
)
from .sprint_loader import SprintParseError, list_sprints, load_sprint
from .state import (
    DebateStore,
    PRRegistry,
    StateLockTimeout,
    StateSchemaMismatch,
    clear_stale_lock,
    ensure_dirs,
    state_lock,
)


# ---------------------------------------------------------------------------
# Typer app graph
# ---------------------------------------------------------------------------


_BANNER = """\
 █████╗  ██████╗ ███████╗███╗   ██╗███████╗██╗   ██╗██╗████████╗███████╗
██╔══██╗██╔════╝ ██╔════╝████╗  ██║██╔════╝██║   ██║██║╚══██╔══╝██╔════╝
███████║██║  ███╗█████╗  ██╔██╗ ██║███████╗██║   ██║██║   ██║   █████╗
██╔══██║██║   ██║██╔══╝  ██║╚██╗██║╚════██║██║   ██║██║   ██║   ██╔══╝
██║  ██║╚██████╔╝███████╗██║ ╚████║███████║╚██████╔╝██║   ██║   ███████╗
╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝╚══════╝ ╚═════╝ ╚═╝   ╚═╝   ╚══════╝
              Agent-native C-suite orchestration
"""


def _print_banner() -> None:
    """Emit the banner on stderr so stdout stays the machine-parseable channel.

    Orchestrators that pipe ``agensuite bootstrap`` output (or any future
    subcommand) get a clean stdout; humans still see the banner because
    interactive shells render stderr alongside stdout.
    """
    typer.echo(_BANNER, err=True)


app = typer.Typer(
    add_completion=False,
    help="Agent-native C-suite orchestration plumbing.",
)
pr_app = typer.Typer(help="Pull-request operations.", no_args_is_help=True)
sprint_app = typer.Typer(help="Inspect sprint definitions.", no_args_is_help=True)
debate_app = typer.Typer(help="Drive the turn-based debate.", no_args_is_help=True)
adr_app = typer.Typer(help="Decision-record operations.", no_args_is_help=True)
state_app = typer.Typer(help="State-store maintenance.", no_args_is_help=True)
chief_app = typer.Typer(
    help="Customize executive personas without opening files.",
    no_args_is_help=True,
)

app.add_typer(pr_app, name="pr")
app.add_typer(sprint_app, name="sprint")
app.add_typer(debate_app, name="debate")
app.add_typer(adr_app, name="adr")
app.add_typer(state_app, name="state")
app.add_typer(chief_app, name="chief")


# ---------------------------------------------------------------------------
# Scaffolding constants
# ---------------------------------------------------------------------------


CHIEF_ROLES = ("ceo", "cpo", "cto", "cdo", "cco")

# Coding agents the ``init --agent`` fast path can launch. Each maps to an
# ordered list of binary candidates; the first one found on PATH wins. The
# invocation is uniform — ``<binary> "<seed prompt>"`` — because all three
# CLIs accept an initial instruction as a positional argument.
AGENT_REGISTRY: dict[str, list[str]] = {
    "claude": ["claude"],
    "codex": ["codex"],
    "cursor": ["cursor-agent", "cursor"],
}


def _resolve_agent_binary(agent: str) -> Optional[str]:
    """Return the absolute path of the first available binary for ``agent``."""
    for candidate in AGENT_REGISTRY[agent]:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _seed_prompt(*, idea_deferred: bool) -> str:
    """Build the first instruction handed to the launched coding agent.

    When the idea was deferred (``init --agent`` with no ``--idea``), the
    prompt tells the agent to collect it and fill the template placeholders
    via ``set-idea`` before running the sprint. When the idea was already
    substituted at init time, the shorter form skips that step.
    """
    if idea_deferred:
        return (
            "Read AGENTS.md. First ask me for a one-line startup idea, then run "
            '`agensuite set-idea "<idea>"`, then execute sprint-1.'
        )
    return "Read AGENTS.md and execute sprint-1."


def _launch_agent(agent: str, target: Path, *, idea_deferred: bool) -> None:
    """Chdir into ``target`` and replace this process with the coding agent.

    ``os.execvp`` (rather than ``subprocess``) hands the terminal entirely to
    the agent with no lingering Python parent. If the binary isn't on PATH the
    scaffold + bootstrap have already succeeded, so we don't treat it as a
    failure: print the seed prompt for the user to paste and exit 0.
    """
    prompt = _seed_prompt(idea_deferred=idea_deferred)
    os.chdir(target)
    binary = _resolve_agent_binary(agent)
    if binary is None:
        primary = AGENT_REGISTRY[agent][0]
        typer.echo(
            f"couldn't find {primary!r} on PATH — open this folder in your "
            "coding agent and paste this instruction:",
            err=True,
        )
        typer.echo(prompt)
        raise typer.Exit(0)
    os.execvp(binary, [binary, prompt])


# ---------------------------------------------------------------------------
# Globals / helpers
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def _global(
    ctx: typer.Context,
    root: Optional[Path] = typer.Option(
        None,
        "--root",
        envvar="AGENSUITE_ROOT",
        help="Project root containing sprints/, workspace/, state/. "
             "Defaults to the current working directory.",
    ),
) -> None:
    """Set the project root for every subcommand.

    Threads through ``ctx.obj`` so subcommands don't each have to parse the
    same flag. The env var fallback (``AGENSUITE_ROOT``) is what makes the
    CLI usable from inside spawned subagents that don't share the
    orchestrator's CWD.
    """
    resolved = (root or Path.cwd()).resolve()
    ctx.obj = {"root": resolved}
    if ctx.invoked_subcommand is None:
        _print_banner()
        typer.echo(ctx.get_help())
        raise typer.Exit()


def _root(ctx: typer.Context) -> Path:
    return ctx.obj["root"] if ctx.obj else Path.cwd()


def _engine(ctx: typer.Context) -> GitEngine:
    return GitEngine(_root(ctx))


def _resource_templates_root() -> Traversable:
    """Return the immutable templates bundled with the installed package."""
    return resources.files("agensuite").joinpath("templates")


def _copy_resource_tree(
    source: Traversable,
    destination: Path,
    *,
    idea: Optional[str] = None,
) -> None:
    """Copy an importlib.resources tree to a real filesystem path.

    ``Traversable`` resources may come from an editable source tree, a normal
    site-packages directory, or a zip-style importer. Reading bytes through the
    resource API keeps ``init`` independent of where Python installed us.

    When ``idea`` is supplied, UTF-8-decodable files have their templated
    tokens substituted in-flight; binary files are copied unchanged.
    """
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(source.iterdir(), key=lambda p: p.name):
            _copy_resource_tree(child, destination / child.name, idea=idea)
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = source.read_bytes()
    if idea is None:
        destination.write_bytes(raw)
        return
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        destination.write_bytes(raw)
        return
    destination.write_text(_substitute_tokens(text, idea), encoding="utf-8")


def _resolve_target_dir(target_dir: Path) -> Path:
    """Resolve a user-supplied init target relative to the active shell CWD."""
    target = target_dir.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    return target.resolve(strict=False)


def _err(msg: str, code: int = 1) -> typer.Exit:
    """Print to stderr and return a ``typer.Exit`` for ``raise _err(...)``.

    When raising inside an ``except`` block, prefer ``raise _err(...) from e``
    (or ``from None`` when suppressing the chain entirely) to keep
    ``__context__`` clean and avoid the "During handling of the above
    exception, another exception occurred" framing.
    """
    typer.echo(msg, err=True)
    return typer.Exit(code=code)


def _short_id(*parts: str) -> str:
    """Deterministic 6-hex-char id derived from ``parts``.

    SHA-1 is fine here — these ids never authenticate anything, they just
    have to be collision-resistant enough across a single project.
    """
    h = hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()
    return h[:6]


def _debate_log_path(root: Path) -> Path:
    return root / "state" / "debate-log.md"


def _emit_digest(root: Path, text: str) -> None:
    """Print ``text`` to stderr (ANSI on a TTY) and append the plain form to
    ``state/debate-log.md``. Append failure warns to stderr but never aborts
    the caller — the state write is the source of truth.
    """
    tty = sys.stderr.isatty()
    for line in text.splitlines() or [text]:
        typer.echo(_digest.colorize(line, tty=tty), err=True)
    path = _debate_log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip("\n") + "\n")
    except OSError as e:
        typer.echo(f"warning: could not append to {path}: {e}", err=True)


def _load_sprint_or_die(root: Path, sprint_id: str) -> SprintConfig:
    """Load a sprint, mapping every failure mode to a clean CLI error."""
    try:
        return load_sprint(root, sprint_id)
    except FileNotFoundError as e:
        raise _err(str(e)) from e
    except SprintParseError as e:
        raise _err(f"invalid sprint file: {e}") from e


def _rebuild_schedule(
    debate: DebateState,
    cfg: SprintConfig,
    sprint_prs: list[PullRequest],
) -> None:
    """Refresh ``debate.schedule`` to reflect the current set of sprint PRs.

    Two modes, chosen by whether the debate has started consuming turns:

    * ``cursor == 0`` — the debate hasn't begun, so a full rebuild is safe:
      :meth:`DebateState.build` is authoritative for ordering.
    * ``cursor > 0`` — at least one ``debate next-turn`` has already been
      consumed. A full rebuild would change which logical turn ``cursor``
      indexes into (because the flat schedule interleaves rounds across PRs:
      all PRs' round-0 slots come before any round-1 slot). So we *append*
      schedule slots for genuinely-new PRs at the tail and leave already-
      indexed slots untouched. Existing PRs are unchanged.

    AGENTS.md's documented sprint loop opens every PR *before* the first
    ``next-turn``, so the append-only path exists for resilience rather than
    routine use.
    """
    if debate.cursor == 0:
        fresh = DebateState.build(cfg, sprint_prs)
        debate.schedule = fresh.schedule
        debate.pr_ids = fresh.pr_ids
        return

    known_ids = set(debate.pr_ids)
    new_prs = [p for p in sprint_prs if p.id not in known_ids]
    if not new_prs:
        return
    appended = DebateState.build(cfg, new_prs).schedule
    debate.schedule.extend(appended)
    debate.pr_ids.extend(p.id for p in new_prs)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    target_dir: Path = typer.Argument(
        ...,
        help="Directory to create for a new isolated agensuite project.",
    ),
    idea: Optional[str] = typer.Option(
        None,
        "--idea",
        help="Startup idea / company mission. If omitted on a TTY, the guided setup wizard launches; otherwise read from stdin. Ignored prompting when --agent is set (the agent collects it).",
    ),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        help="claude | codex | cursor: after scaffolding, bootstrap and launch "
             "this coding agent inside the new folder. With no --idea, the agent "
             "collects it and fills placeholders via `agensuite set-idea`.",
    ),
) -> None:
    """Scaffold a clean project from packaged blueprints, with the user-provided
    startup idea substituted into the templated ``{{CORE_PRODUCT_IDEA}}`` and
    ``{{COMPANY_MISSION}}`` tokens.

    With ``--agent``, the command also bootstraps the sandbox and replaces
    itself with the named coding agent running inside the new folder. Omitting
    ``--idea`` in that mode is the fast path: the wizard is skipped, defaults
    are used, the idea placeholders are left intact, and the agent is told to
    ask for the idea and run ``set-idea`` before the sprint loop.
    """
    if agent is not None and agent not in AGENT_REGISTRY:
        raise _err(
            f"unknown --agent {agent!r} "
            f"(expected one of: {', '.join(AGENT_REGISTRY)})"
        )

    target = _resolve_target_dir(target_dir)
    if target.exists() and not target.is_dir():
        raise _err(f"target path exists and is not a directory: {target}")
    if target.exists() and any(target.iterdir()):
        raise _err(f"target directory is not empty: {target}")

    templates = _resource_templates_root()
    if not templates.is_dir():
        raise _err("packaged templates are missing; reinstall agensuite")

    # Resolve answers. Three paths:
    #   * fast path (--agent, no --idea): defer the idea to the agent. No
    #     wizard, defaults everywhere, placeholders kept (idea_deferred=True).
    #   * interactive: no --idea on a TTY → guided wizard.
    #   * non-interactive: --idea flag or a piped stdin line, everything else
    #     defaults.
    from .wizard import default_answers, run_init_wizard

    idea_deferred = idea is None and agent is not None
    if idea_deferred:
        answers = default_answers("")
        subst_idea: Optional[str] = None
    elif idea is None and sys.stdin.isatty():
        answers = run_init_wizard()
        subst_idea = answers.idea
    else:
        if idea is None:
            idea = typer.prompt("Describe your startup idea")
        idea = idea.strip()
        if not idea:
            raise _err("idea must be a non-empty string")
        answers = default_answers(idea)
        subst_idea = answers.idea

    try:
        target.mkdir(parents=True, exist_ok=True)
        _copy_resource_tree(
            templates.joinpath("AGENTS.md"), target / "AGENTS.md", idea=subst_idea
        )
        _copy_resource_tree(
            templates.joinpath(".claude", "agents"),
            target / ".claude" / "agents",
            idea=subst_idea,
        )
        _copy_resource_tree(
            templates.joinpath("sprints", "sprint-1.md"),
            target / "sprints" / "sprint-1.md",
            idea=subst_idea,
        )

        # Apply per-persona biases.
        for role, lines in answers.biases.items():
            agent_file = target / ".claude" / "agents" / f"{role}.md"
            text = agent_file.read_text(encoding="utf-8")
            for line in lines:
                text = _append_operational_bias(text, line)
            agent_file.write_text(text, encoding="utf-8")

        # Apply sprint-1 config.
        sprint_file = target / "sprints" / "sprint-1.md"
        sprint_file.write_text(
            _set_sprint_frontmatter(
                sprint_file.read_text(encoding="utf-8"),
                rounds=answers.debate_rounds,
                quorum=answers.approval_quorum,
                participants=answers.participants,
            ),
            encoding="utf-8",
        )
    except (OSError, ValueError) as e:
        raise _err(f"init failed: {e}") from e

    # Fast path: bootstrap the sandbox and hand the terminal to the agent.
    # _launch_agent does not return on success (it os.execvp's), so nothing
    # below runs in that case.
    if agent is not None:
        typer.echo(f"Initialized agensuite project at {target}", err=True)
        _do_bootstrap(target)
        typer.echo(f"launching {agent} in {target} …", err=True)
        _launch_agent(agent, target, idea_deferred=idea_deferred)
        return

    typer.echo(f"Successfully initialized agensuite project at {target}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {target}")
    typer.echo("  agensuite bootstrap")
    typer.echo("  open this folder inside your coding agent session")


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _do_bootstrap(root: Path, *, reset: bool = False) -> None:
    """Create ``workspace/`` (inner git repo) and ``state/`` under ``root``.

    Shared by the ``bootstrap`` command and ``init``'s ``--agent`` fast path
    so the two never drift. Idempotent; raises ``typer.Exit`` on git failure.
    """
    ensure_dirs(root)
    try:
        GitEngine(root).bootstrap(reset=reset)
    except GitCommandError as e:
        raise _err(f"bootstrap failed: {e}") from e


@app.command()
def bootstrap(
    ctx: typer.Context,
    reset: bool = typer.Option(False, "--reset", help="Wipe workspace/ before init."),
) -> None:
    """Initialize workspace/ (inner git repo) and state/ directories. Idempotent."""
    root = _root(ctx)
    _do_bootstrap(root, reset=reset)
    _print_banner()
    typer.echo(f"workspace ready at {root / 'workspace'}")
    typer.echo(f"state ready at {root / 'state'}")


# ---------------------------------------------------------------------------
# set-idea
# ---------------------------------------------------------------------------


def _idea_token_targets(root: Path) -> list[Path]:
    """Files that may carry idea placeholders, in a stable order.

    ``AGENTS.md`` plus every persona under ``.claude/agents/`` and every
    sprint under ``sprints/``. Only existing files are returned.
    """
    targets: list[Path] = []
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        targets.append(agents_md)
    agents_dir = root / ".claude" / "agents"
    if agents_dir.is_dir():
        targets.extend(sorted(agents_dir.glob("*.md")))
    sprints_dir = root / "sprints"
    if sprints_dir.is_dir():
        targets.extend(sorted(sprints_dir.glob("*.md")))
    return targets


@app.command("set-idea")
def set_idea(
    ctx: typer.Context,
    idea: str = typer.Argument(..., help="Startup idea / company mission text."),
) -> None:
    """Fill the templated idea placeholders across a scaffolded project.

    Substitutes ``{{CORE_PRODUCT_IDEA}}`` and ``{{COMPANY_MISSION}}`` in
    ``AGENTS.md``, every ``.claude/agents/*.md``, and every ``sprints/*.md``.
    Intended to be run by the coding agent after it collects the idea from the
    user on the ``init --agent`` fast path, but works standalone too.

    Re-running after the idea is already set finds no placeholders and is a
    harmless no-op (warns, exits 0). An empty idea is an error.
    """
    text_idea = idea.strip()
    if not text_idea:
        raise _err("idea must be a non-empty string")

    root = _root(ctx)
    changed = 0
    found_token = False
    for path in _idea_token_targets(root):
        original = path.read_text(encoding="utf-8")
        if "{{CORE_PRODUCT_IDEA}}" in original or "{{COMPANY_MISSION}}" in original:
            found_token = True
        updated = _substitute_tokens(original, text_idea)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1

    if not found_token:
        typer.echo(
            "no idea placeholders found (already set, or not an agensuite project root)",
            err=True,
        )
    typer.echo(f"{changed} files updated")


# ---------------------------------------------------------------------------
# sprint
# ---------------------------------------------------------------------------


@sprint_app.command("show")
def sprint_show(ctx: typer.Context, sprint_id: str) -> None:
    """Print the parsed SprintConfig as JSON."""
    cfg = _load_sprint_or_die(_root(ctx), sprint_id)
    typer.echo(cfg.model_dump_json(indent=2))


@sprint_app.command("list")
def sprint_list(ctx: typer.Context) -> None:
    """List all sprints in sprints/."""
    try:
        sprints = list_sprints(_root(ctx))
    except SprintParseError as e:
        raise _err(f"invalid sprint file: {e}") from e
    if not sprints:
        typer.echo("(no sprints)")
        return
    for cfg in sprints:
        typer.echo(
            f"{cfg.id}\t{cfg.title}\tparticipants={','.join(cfg.participants)}"
        )


# ---------------------------------------------------------------------------
# branch
# ---------------------------------------------------------------------------


@app.command("branch")
def branch_cmd(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="One of: create"),
    name: str = typer.Argument(..., help="Branch name, e.g. feat/cpo/core-app-prd"),
    base: str = typer.Option("main", "--base"),
) -> None:
    """Branch operations (currently: create)."""
    if action != "create":
        raise _err(f"unknown branch action: {action!r} (expected 'create')")
    try:
        path = _engine(ctx).checkout_branch(name, base=base)
    except GitCommandError as e:
        raise _err(f"branch create failed: {e}") from e
    typer.echo(str(path))


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@app.command("read")
def read_cmd(
    ctx: typer.Context,
    branch: str = typer.Option(..., "--branch"),
    path: str = typer.Option(..., "--path"),
) -> None:
    """Print file contents from a branch's worktree."""
    try:
        typer.echo(_engine(ctx).read_file(branch, path), nl=False)
    except FileNotFoundError as e:
        raise _err(f"file not found: {e}") from e


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


@app.command("commit")
def commit_cmd(
    ctx: typer.Context,
    branch: str = typer.Option(..., "--branch"),
    author: str = typer.Option(..., "--author"),
    message: str = typer.Option(..., "--message"),
    files: list[str] = typer.Option(
        ..., "--files", help="Repeat --files for each path"
    ),
) -> None:
    """Stage and commit files on a branch's worktree under the author identity."""
    try:
        sha = _engine(ctx).commit(branch, author, message, files)
    except GitCommandError as e:
        raise _err(f"commit failed: {e}") from e
    typer.echo(sha)


# ---------------------------------------------------------------------------
# pr
# ---------------------------------------------------------------------------


@pr_app.command("open")
def pr_open(
    ctx: typer.Context,
    branch: str = typer.Option(..., "--branch"),
    author: str = typer.Option(..., "--author"),
    title: str = typer.Option(..., "--title"),
    sprint: str = typer.Option(..., "--sprint"),
    files: list[str] = typer.Option([], "--files"),
    description: str = typer.Option("", "--description"),
    headline: str = typer.Option("", "--headline",
        help="One-line product claim this PR makes (feeds the decision ledger)."),
    base: str = typer.Option("main", "--base"),
) -> None:
    """Open a PR and refresh the sprint's debate schedule.

    Schedule refresh semantics live in :func:`_rebuild_schedule`: full
    rebuild before any turn has been consumed, append-only after the cursor
    advances. Opening the same ``(branch, author, title)`` triple twice is
    rejected via the ``pr_id`` collision check; PRs can't be "updated" in
    place — open a new PR with a different branch instead.
    """
    root = _root(ctx)
    cfg = _load_sprint_or_die(root, sprint)

    if author not in cfg.participants:
        raise _err(
            f"author {author!r} is not a participant of sprint {sprint!r} "
            f"(participants: {', '.join(cfg.participants)})"
        )

    pr_id = f"pr-{_short_id(branch, author, title)}"

    # The outer ``try/except StateLockTimeout`` only ever catches the
    # acquire-side failure of ``state_lock``; everything inside the ``with``
    # block raises ``typer.Exit`` (not a ``StateLockTimeout``) and propagates
    # past this handler unchanged. The nesting reads slightly odd but is
    # intentional.
    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
            if pr_id in prs:
                raise _err(f"PR already exists: {pr_id}")

            pr = PullRequest(
                id=pr_id,
                title=title,
                branch=branch,
                base=base,
                author=author,
                description=description,
                headline=headline,
                files=list(files),
                status=PRStatus.OPEN,
                sprint_id=sprint,
            )
            prs[pr_id] = pr
            PRRegistry.save(root, prs)

            sprint_prs = sorted(
                [p for p in prs.values() if p.sprint_id == sprint],
                key=lambda p: p.created_at,
            )

            debate = DebateStore.load(root, sprint) or DebateState(sprint_id=sprint)
            _rebuild_schedule(debate, cfg, sprint_prs)
            DebateStore.save(root, debate)
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    typer.echo(pr_id)


def _recompute_pr_status(pr: PullRequest) -> None:
    """Derive ``pr.status`` from current review state.

    Order of precedence (terminal states stay terminal):
      1. ``MERGED`` / ``REJECTED`` — leave untouched.
      2. Any reviewer posted a FOLLOWUP with verdict ``REQUEST_CHANGES``
         ("stand") → DEADLOCKED. They've used their follow-up beat and
         haven't yielded; no more turns are available so the debate can't
         resolve without a human.
      3. ``pr.open_change_requests`` non-empty → CHANGES_REQUESTED.
      4. At least one review exists → UNDER_REVIEW.
      5. No reviews → OPEN.
    """
    if pr.status in (PRStatus.MERGED, PRStatus.REJECTED):
        return
    if any(
        r.phase == TurnPhase.FOLLOWUP and r.verdict == Verdict.REQUEST_CHANGES
        for r in pr.reviews
    ):
        pr.status = PRStatus.DEADLOCKED
        return
    if pr.open_change_requests:
        pr.status = PRStatus.CHANGES_REQUESTED
        return
    if pr.reviews:
        pr.status = PRStatus.UNDER_REVIEW
        return
    pr.status = PRStatus.OPEN


def _rebuttal_slot_idx(debate: DebateState, pr_id: str) -> Optional[int]:
    """Return the index of the REBUTTAL slot for ``pr_id``, or None."""
    for i, t in enumerate(debate.schedule):
        if t.phase == TurnPhase.REBUTTAL and t.target_pr_id == pr_id:
            return i
    return None


@pr_app.command("comment")
def pr_comment(
    ctx: typer.Context,
    id: str = typer.Option(..., "--id"),
    reviewer: str = typer.Option(..., "--reviewer"),
    comment: str = typer.Option(..., "--comment"),
    file: Optional[str] = typer.Option(None, "--file"),
    approve: bool = typer.Option(
        False,
        "--approve",
        help="Deprecated alias for --verdict APPROVE.",
    ),
    verdict_raw: Optional[str] = typer.Option(
        None,
        "--verdict",
        help="APPROVE | REQUEST_CHANGES | COMMENT. Defaults to COMMENT.",
    ),
    phase_raw: str = typer.Option(
        "REVIEW",
        "--phase",
        help="REVIEW | REBUTTAL | FOLLOWUP. Defaults to REVIEW.",
    ),
    parent_turn_idx: Optional[int] = typer.Option(
        None,
        "--parent-turn-idx",
        help="Required for FOLLOWUP: index of the REBUTTAL slot being answered.",
    ),
    counter: str = typer.Option("", "--counter",
        help="One-line alternative you push (use with --verdict REQUEST_CHANGES); "
             "feeds the contested column of the ledger."),
) -> None:
    """Append a review to a PR and a REVIEW message to the sprint transcript.

    The phase determines which beat of the rebuttal protocol the comment
    belongs to (initial REVIEW, author REBUTTAL, or reviewer FOLLOWUP) and
    drives both the self-review guard (REBUTTAL requires
    ``reviewer == pr.author``; other phases forbid it) and schedule
    extensions (a first REQUEST_CHANGES appends a REBUTTAL slot; a posted
    REBUTTAL appends one FOLLOWUP slot per open change-requester).

    Ordering invariant: the orchestrator is expected to call ``debate
    next-turn`` *before* ``pr comment`` for the resulting review. The
    transcript's ``round_idx`` is attributed to the turn the cursor most
    recently consumed (``cursor - 1``); calling ``pr comment`` without a
    prior ``next-turn`` would record the review under round 0 by default.
    """
    if approve and verdict_raw is not None:
        raise _err("--approve and --verdict are mutually exclusive")
    if approve:
        verdict = Verdict.APPROVE
    elif verdict_raw is not None:
        try:
            verdict = Verdict(verdict_raw)
        except ValueError as e:
            raise _err(
                f"invalid --verdict {verdict_raw!r}; "
                f"expected one of: {', '.join(v.value for v in Verdict)}"
            ) from e
    else:
        verdict = Verdict.COMMENT
    try:
        phase = TurnPhase(phase_raw)
    except ValueError as e:
        raise _err(
            f"invalid --phase {phase_raw!r}; "
            f"expected one of: {', '.join(p.value for p in TurnPhase)}"
        ) from e

    root = _root(ctx)
    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
            if id not in prs:
                raise _err(f"unknown PR: {id}")
            pr = prs[id]
            cfg = _load_sprint_or_die(root, pr.sprint_id)

            _enforce_phase_preconditions(
                pr=pr,
                reviewer=reviewer,
                phase=phase,
                verdict=verdict,
                cfg=cfg,
                parent_turn_idx=parent_turn_idx,
            )

            review = ReviewComment(
                reviewer=reviewer,
                file=file,
                comment=comment,
                verdict=verdict,
                phase=phase,
                counter=counter,
            )
            pr.reviews.append(review)
            _recompute_pr_status(pr)
            prs[id] = pr
            PRRegistry.save(root, prs)

            debate = DebateStore.load(root, pr.sprint_id) or DebateState(
                sprint_id=pr.sprint_id
            )
            # round_idx best-effort: the round of whatever turn the cursor
            # most recently consumed. Falls back to 0 before the first turn.
            round_idx = 0
            if debate.schedule and debate.cursor > 0:
                round_idx = debate.schedule[
                    min(debate.cursor - 1, len(debate.schedule) - 1)
                ].round_idx
            # Derive the Message id from ``review.timestamp`` (not a fresh
            # ``datetime.now()``) so :func:`_rebuttal_msg_id` can rebuild
            # the same id from a persisted ReviewComment — without this
            # the two timestamps diverge and FOLLOWUP's rebuttal_msg_id
            # points at a non-existent transcript line.
            msg = Message(
                id=f"msg-{_short_id(id, reviewer, comment, review.timestamp.isoformat())}",
                sender=reviewer,
                recipient=pr.author,
                msg_type=(
                    MessageType.DECISION
                    if phase != TurnPhase.REVIEW
                    else MessageType.REVIEW
                ),
                content=comment,
                parent_id=id,
                round_idx=round_idx,
            )
            debate.transcript.append(msg)

            _extend_schedule_after_comment(debate, pr, phase, verdict)
            DebateStore.save(root, debate)
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    _emit_digest(
        root,
        _digest.render_verdict_line(
            pr=pr, comment=review, round_idx=round_idx, quorum=cfg.approval_quorum
        ),
    )
    typer.echo(
        json.dumps(
            {
                "pr": id,
                "reviewer": reviewer,
                "verdict": verdict.value,
                "phase": phase.value,
                "status": pr.status.value,
            }
        )
    )


def _enforce_phase_preconditions(
    *,
    pr: PullRequest,
    reviewer: str,
    phase: TurnPhase,
    verdict: Verdict,
    cfg: SprintConfig,
    parent_turn_idx: Optional[int],
) -> None:
    """Reject malformed phase/verdict/reviewer combinations before append.

    The CLI enforces these as hard guards so the playbook contract doesn't
    rely on the LLM doing the right thing — same posture as the original
    reviewer-author guard.
    """
    if pr.status in (PRStatus.MERGED, PRStatus.REJECTED):
        raise _err(f"PR {pr.id} is {pr.status.value}; no further comments accepted")

    if phase == TurnPhase.REVIEW:
        if reviewer == pr.author:
            raise _err(
                f"reviewer {reviewer!r} cannot review own PR {pr.id} "
                "(authors push back via --phase REBUTTAL)"
            )
        return

    if phase == TurnPhase.REBUTTAL:
        if reviewer != pr.author:
            raise _err(
                f"--phase REBUTTAL requires reviewer == pr.author "
                f"(got reviewer={reviewer!r}, author={pr.author!r})"
            )
        if pr.status != PRStatus.CHANGES_REQUESTED:
            raise _err(
                f"--phase REBUTTAL only valid when PR status is CHANGES_REQUESTED "
                f"(current: {pr.status.value})"
            )
        if any(r.phase == TurnPhase.REBUTTAL for r in pr.reviews):
            raise _err(
                f"PR {pr.id} already has a REBUTTAL; cap is 1 rebuttal per PR"
            )
        return

    if phase == TurnPhase.FOLLOWUP:
        if reviewer == pr.author:
            raise _err("--phase FOLLOWUP cannot be posted by PR author")
        if reviewer not in pr.open_change_requests:
            raise _err(
                f"--phase FOLLOWUP only valid for reviewers in "
                f"open_change_requests (currently: {pr.open_change_requests})"
            )
        if not any(r.phase == TurnPhase.REBUTTAL for r in pr.reviews):
            raise _err("--phase FOLLOWUP requires a prior REBUTTAL from the author")
        existing = sum(
            1
            for r in pr.reviews
            if r.phase == TurnPhase.FOLLOWUP and r.reviewer == reviewer
        )
        if existing >= cfg.rebuttal_depth:
            raise _err(
                f"reviewer {reviewer!r} exceeded rebuttal_depth "
                f"({cfg.rebuttal_depth}) on PR {pr.id}"
            )
        if parent_turn_idx is None:
            raise _err("--phase FOLLOWUP requires --parent-turn-idx")


def _extend_schedule_after_comment(
    debate: DebateState,
    pr: PullRequest,
    phase: TurnPhase,
    verdict: Verdict,
) -> None:
    """Append REBUTTAL / FOLLOWUP slots in response to verdict events.

    Schedule is append-only — consumed slots and their ordering are never
    touched here. This is the contract softening called out in
    AGENTS.md §6.
    """
    if phase == TurnPhase.REVIEW and verdict == Verdict.REQUEST_CHANGES:
        if _rebuttal_slot_idx(debate, pr.id) is None:
            debate.schedule.append(
                DebateTurn(
                    round_idx=0,
                    speaker=pr.author,
                    target_pr_id=pr.id,
                    phase=TurnPhase.REBUTTAL,
                )
            )
        return

    if phase == TurnPhase.REBUTTAL:
        rebuttal_idx = _rebuttal_slot_idx(debate, pr.id)
        for cr_reviewer in pr.open_change_requests:
            # Skip if this reviewer already has a FOLLOWUP slot scheduled.
            already_scheduled = any(
                t.phase == TurnPhase.FOLLOWUP
                and t.target_pr_id == pr.id
                and t.speaker == cr_reviewer
                for t in debate.schedule
            )
            if already_scheduled:
                continue
            debate.schedule.append(
                DebateTurn(
                    round_idx=0,
                    speaker=cr_reviewer,
                    target_pr_id=pr.id,
                    phase=TurnPhase.FOLLOWUP,
                    parent_turn_idx=rebuttal_idx,
                )
            )


@pr_app.command("list")
def pr_list(
    ctx: typer.Context,
    sprint: Optional[str] = typer.Option(None, "--sprint"),
) -> None:
    """Print a tabular view of PRs."""
    root = _root(ctx)
    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    rows = sorted(
        (p for p in prs.values() if sprint is None or p.sprint_id == sprint),
        key=lambda p: (p.sprint_id, p.created_at),
    )
    if not rows:
        typer.echo("(no PRs)")
        return
    typer.echo(
        f"{'id':<14} {'sprint':<12} {'author':<6} {'status':<13} "
        f"{'approvals':<9} title"
    )
    for p in rows:
        typer.echo(
            f"{p.id:<14} {p.sprint_id:<12} {p.author:<6} {p.status.value:<13} "
            f"{p.approval_count:<9} {p.title}"
        )


def _merge_pr(
    ctx: typer.Context,
    prs: dict[str, PullRequest],
    id: str,
    *,
    force_deadlock: bool = False,
) -> str:
    """Shared merge implementation used by ``pr merge`` and ``human-gate``.

    Caller holds the state lock and is responsible for persisting ``prs``
    via :meth:`PRRegistry.save` on success. The conflict path persists
    inline because we mutate ``pr.status`` and ``pr.conflict_details`` and
    then raise ``typer.Exit``.

    ``force_deadlock`` makes the human-gate the final arbiter for a
    deadlocked PR: it bypasses both the ``DEADLOCKED`` guard and the
    open-change-request guard (the human is explicitly overriding a
    "stand" follow-up). The quorum guard still fires — a PR that no spoke
    ever approved should never merge silently, even via the gate.
    """
    root = _root(ctx)
    if id not in prs:
        raise _err(f"unknown PR: {id}")
    pr = prs[id]
    if pr.status in (PRStatus.MERGED, PRStatus.REJECTED):
        raise _err(f"PR {id} already {pr.status.value}")

    cfg = _load_sprint_or_die(root, pr.sprint_id)

    if pr.status == PRStatus.DEADLOCKED and not force_deadlock:
        raise _err(
            f"PR {id} is DEADLOCKED; resolve via "
            f"'agensuite human-gate --sprint {pr.sprint_id} --resolve-deadlocks' "
            "before merging"
        )
    if pr.approval_count < cfg.approval_quorum:
        raise _err(
            f"PR {id} has {pr.approval_count} approvals "
            f"(quorum is {cfg.approval_quorum})"
        )
    if pr.open_change_requests and not force_deadlock:
        raise _err(
            f"PR {id} has {len(pr.open_change_requests)} open change request(s) "
            f"from: {', '.join(pr.open_change_requests)}"
        )

    try:
        merge_sha = _engine(ctx).merge_branch(pr.branch, base=pr.base)
    except MergeConflict as e:
        pr.status = PRStatus.REJECTED
        pr.conflict_details = e.details
        prs[id] = pr
        PRRegistry.save(root, prs)
        raise _err(f"merge conflict: {e.details}", code=2) from e
    except GitCommandError as e:
        raise _err(f"merge failed: {e}") from e

    pr.status = PRStatus.MERGED
    prs[id] = pr
    return merge_sha


@pr_app.command("merge")
def pr_merge(
    ctx: typer.Context,
    id: str = typer.Option(..., "--id"),
) -> None:
    """Merge an approved PR.

    Hardened predicate (in order): not DEADLOCKED, quorum met, no open
    change requests, no merge conflict. On conflict the PR is marked
    REJECTED and the command exits 2.
    """
    root = _root(ctx)
    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
            sha = _merge_pr(ctx, prs, id)
            PRRegistry.save(root, prs)
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    # Successful merge only (a conflict raises typer.Exit inside the block).
    cfg = _load_sprint_or_die(root, prs[id].sprint_id)
    sprint_prs = sorted(
        [p for p in prs.values() if p.sprint_id == prs[id].sprint_id],
        key=lambda p: p.created_at,
    )
    _emit_digest(root, _digest.render_pr_terminal(pr=prs[id], quorum=cfg.approval_quorum))
    _emit_digest(root, _digest.render_ledger(sprint_prs, quorum=cfg.approval_quorum))

    typer.echo(sha)


# ---------------------------------------------------------------------------
# debate
# ---------------------------------------------------------------------------


def _pr_converged(pr: PullRequest, cfg: SprintConfig) -> bool:
    """A non-terminal PR is "done" if it has quorum + no open change requests."""
    return (
        pr.status not in (PRStatus.MERGED, PRStatus.REJECTED, PRStatus.DEADLOCKED)
        and pr.approval_count >= cfg.approval_quorum
        and not pr.open_change_requests
    )


def _termination(
    sprint_prs: list[PullRequest],
    cfg: SprintConfig,
) -> Optional[dict]:
    """Return a ``done`` payload if every PR is resolved one way or another.

    Convergence rule (per the user's "convergence over coverage" choice):
    every PR must be (a) MERGED, (b) REJECTED, (c) DEADLOCKED, or (d)
    quorum met + no open change requests. Otherwise the debate is not
    finished even if the pre-computed schedule has been drained.
    """
    if not sprint_prs:
        return {"done": True, "reason": "no_prs"}

    deadlocked: list[str] = []
    pending: list[str] = []
    merged_or_converged = 0
    rejected = 0
    for p in sprint_prs:
        if p.status == PRStatus.MERGED:
            merged_or_converged += 1
        elif p.status == PRStatus.REJECTED:
            rejected += 1
        elif p.status == PRStatus.DEADLOCKED:
            deadlocked.append(p.id)
        elif _pr_converged(p, cfg):
            merged_or_converged += 1
        else:
            pending.append(p.id)

    if pending:
        return None

    if deadlocked:
        return {
            "done": True,
            "reason": "deadlocked",
            "deadlocked_prs": deadlocked,
        }
    if merged_or_converged > 0:
        return {"done": True, "reason": "quorum_met"}
    return {"done": True, "reason": "all_rejected"}


def _rebuttal_msg_id(pr: PullRequest) -> Optional[str]:
    """Return ``Message.id`` of the most recent REBUTTAL transcript line.

    Used to surface the exact rebuttal text to a FOLLOWUP subagent. The
    transcript message ID is computed in :func:`pr_comment`; we reconstruct
    the same id via :func:`_short_id` so callers don't need to re-load the
    debate transcript.
    """
    for r in reversed(pr.reviews):
        if r.phase == TurnPhase.REBUTTAL:
            return (
                f"msg-{_short_id(pr.id, r.reviewer, r.comment, r.timestamp.isoformat())}"
            )
    return None


@debate_app.command("next-turn")
def debate_next_turn(
    ctx: typer.Context,
    sprint: str = typer.Option(..., "--sprint"),
) -> None:
    """Return the next required turn or ``{"done": true, "reason": ...}``.

    Termination is now verdict-based: even if the pre-computed schedule has
    more slots, the debate is "done" the moment every PR is either terminal
    (MERGED / REJECTED / DEADLOCKED) or has quorum + zero open change
    requests. The pre-computed schedule is therefore an upper bound, not a
    fixed length.

    Advance-then-save ordering is deliberate: if ``DebateStore.save`` raises
    (disk full, permission flip), the in-memory ``cursor`` mutation is
    discarded with the process and the next CLI invocation re-reads the old
    cursor, replaying the same turn. That's the correct recovery contract —
    a turn is "consumed" only after it has been durably persisted.
    """
    root = _root(ctx)
    emit_ledger_for_round: Optional[str] = None
    try:
        with state_lock(root):
            cfg = _load_sprint_or_die(root, sprint)
            prs = PRRegistry.load(root)
            sprint_prs = sorted(
                [p for p in prs.values() if p.sprint_id == sprint],
                key=lambda p: p.created_at,
            )

            done = _termination(sprint_prs, cfg)
            if done is not None:
                typer.echo(json.dumps(done))
                return

            debate = DebateStore.load(root, sprint)
            if debate is None or not debate.schedule:
                typer.echo(json.dumps({"done": True, "reason": "no_schedule"}))
                return

            turn: Optional[DebateTurn] = None
            while debate.cursor < len(debate.schedule):
                candidate = debate.schedule[debate.cursor]
                pr = next(
                    (p for p in sprint_prs if p.id == candidate.target_pr_id),
                    None,
                )
                if pr is None or not _turn_still_meaningful(candidate, pr, cfg):
                    debate.advance()
                    continue
                turn = candidate
                debate.advance()
                break

            if turn is not None and turn.round_idx > debate.last_emitted_round:
                debate.last_emitted_round = turn.round_idx
                emit_ledger_for_round = f"ROUND {turn.round_idx}"

            DebateStore.save(root, debate)

            if turn is None:
                # Schedule exhausted but not every PR converged. Promote
                # the stragglers to DEADLOCKED so `human-gate
                # --resolve-deadlocks` picks them up — otherwise a PR
                # that's CHANGES_REQUESTED with no further scheduled
                # turns has no resolution path (review finding #3).
                done = _termination(sprint_prs, cfg)
                if done is not None:
                    typer.echo(json.dumps(done))
                    return
                stragglers = [
                    p
                    for p in sprint_prs
                    if p.status
                    not in (
                        PRStatus.MERGED,
                        PRStatus.REJECTED,
                        PRStatus.DEADLOCKED,
                    )
                    and not _pr_converged(p, cfg)
                ]
                for p in stragglers:
                    p.status = PRStatus.DEADLOCKED
                    prs[p.id] = p
                if stragglers:
                    PRRegistry.save(root, prs)
                typer.echo(
                    json.dumps(
                        {
                            "done": True,
                            "reason": "deadlocked",
                            "deadlocked_prs": [p.id for p in stragglers],
                        }
                    )
                )
                return
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    if emit_ledger_for_round is not None:
        _emit_digest(
            root,
            _digest.render_ledger(
                sprint_prs, quorum=cfg.approval_quorum, round_label=emit_ledger_for_round
            ),
        )

    target_pr = next(p for p in sprint_prs if p.id == turn.target_pr_id)
    result: dict = {
        "round": turn.round_idx,
        "speaker": turn.speaker,
        "pr_id": turn.target_pr_id,
        "phase": turn.phase.value,
    }
    if turn.parent_turn_idx is not None:
        result["parent_turn_idx"] = turn.parent_turn_idx
    if turn.phase == TurnPhase.REVIEW:
        result["prompt_hint"] = "initial_review"
    elif turn.phase == TurnPhase.REBUTTAL:
        result["prompt_hint"] = "address_change_requests"
        result["open_change_requests"] = target_pr.open_change_requests
    elif turn.phase == TurnPhase.FOLLOWUP:
        result["prompt_hint"] = "decide_stand_or_withdraw"
        rebuttal_id = _rebuttal_msg_id(target_pr)
        if rebuttal_id is not None:
            result["rebuttal_msg_id"] = rebuttal_id
    typer.echo(json.dumps(result))


def _turn_still_meaningful(
    turn: DebateTurn,
    pr: PullRequest,
    cfg: SprintConfig,
) -> bool:
    """Return False if the scheduled turn no longer makes sense.

    Examples:
      * the PR was merged / rejected / deadlocked before this slot ran;
      * a REVIEW slot fires for a PR that already has quorum + no open
        change requests (skip to keep the simulation efficient);
      * a REBUTTAL slot fires but no change requests are open (the
        author has nothing to rebut).
    """
    if pr.status in (PRStatus.MERGED, PRStatus.REJECTED, PRStatus.DEADLOCKED):
        return False
    if _pr_converged(pr, cfg):
        return False
    if turn.phase == TurnPhase.REBUTTAL and not pr.open_change_requests:
        return False
    if turn.phase == TurnPhase.FOLLOWUP and turn.speaker not in pr.open_change_requests:
        return False
    return True


@debate_app.command("tail")
def debate_tail(
    ctx: typer.Context,
    sprint: str = typer.Option(..., "--sprint"),
    window: int = typer.Option(6, "--window"),
) -> None:
    """Print the last N transcript messages as JSON."""
    root = _root(ctx)
    try:
        with state_lock(root):
            debate = DebateStore.load(root, sprint)
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    if debate is None:
        typer.echo(json.dumps([]))
        return
    tail = debate.transcript[-window:] if window > 0 else []
    typer.echo(
        json.dumps([json.loads(m.model_dump_json()) for m in tail], indent=2)
    )


# ---------------------------------------------------------------------------
# human-gate
# ---------------------------------------------------------------------------


@app.command("human-gate")
def human_gate(
    ctx: typer.Context,
    message: Optional[str] = typer.Option(None, "--message"),
    sprint: Optional[str] = typer.Option(None, "--sprint"),
    resolve_deadlocks: bool = typer.Option(
        False,
        "--resolve-deadlocks",
        help="Iterate over DEADLOCKED PRs in --sprint and prompt the human "
             "to [m]erge / [r]eject / [a]dr-options / [s]kip each one.",
    ),
) -> None:
    """Default mode: print a banner and block on stdin until Enter.

    With ``--resolve-deadlocks --sprint <s>`` the command instead walks
    every DEADLOCKED PR in the sprint, shows the rebuttal + every "stand"
    follow-up, and applies the human's choice. This is the only path that
    can merge a PR whose status is DEADLOCKED.
    """
    root = _root(ctx)
    if resolve_deadlocks:
        if not sprint:
            raise _err("--resolve-deadlocks requires --sprint <id>")
        _resolve_deadlocks_loop(ctx, sprint)
        return

    if not message:
        raise _err("--message is required (or pass --resolve-deadlocks --sprint <s>)")

    bar = "=" * 72
    typer.echo(f"\n{bar}")
    typer.echo(f"  HUMAN GATE: {message}")
    typer.echo(f"{bar}")

    # Surface the decision ledger so the human sees the plan's standing before
    # deciding. Best-effort: a missing/locked state never blocks the gate.
    if sprint:
        try:
            with state_lock(root):
                cfg = _load_sprint_or_die(root, sprint)
                prs = PRRegistry.load(root)
                sprint_prs = sorted(
                    [p for p in prs.values() if p.sprint_id == sprint],
                    key=lambda p: p.created_at,
                )
            _emit_digest(root, _digest.render_ledger(
                sprint_prs, quorum=cfg.approval_quorum, round_label="HUMAN GATE"))
        except (StateLockTimeout, StateSchemaMismatch):
            pass

    try:
        input("press Enter to continue: ")
    except EOFError:
        # Non-interactive caller (CI, scripted orchestrator): treat as
        # immediate continuation. The banner is still printed so the log
        # records the gate was passed through.
        typer.echo("(no tty — auto-continuing)")


def _resolve_deadlocks_loop(ctx: typer.Context, sprint: str) -> None:
    """Walk every DEADLOCKED PR in ``sprint`` and apply the human's choice.

    Per-PR prompt: ``[m]erge / [r]eject / [a]dr-options / [s]kip``.

    Choices:
      * ``m`` — call :func:`_merge_pr` with ``force_deadlock=True`` so the
        DEADLOCKED guard is bypassed (quorum + open-change-request guards
        still fire).
      * ``r`` — mark the PR REJECTED.
      * ``a`` — keep status as DEADLOCKED but stamp ``human_disposition``
        so :func:`adr_record` surfaces both positions in the ADR Options
        block instead of merging either.
      * ``s`` — leave the PR untouched (operator wants to come back later).

    EOF / non-interactive: every remaining PR is treated as "skip" and the
    command exits cleanly so scripted runs don't hang.
    """
    root = _root(ctx)
    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
            deadlocked = sorted(
                [p for p in prs.values() if p.sprint_id == sprint
                 and p.status == PRStatus.DEADLOCKED],
                key=lambda p: p.created_at,
            )
            if not deadlocked:
                typer.echo(json.dumps({"resolved": [], "reason": "no_deadlocks"}))
                return

            bar = "=" * 72
            resolutions: list[dict] = []
            # Human-readable prose flows to stderr so stdout stays a clean
            # JSON channel for the orchestrator. Matches the same
            # separation used by `bootstrap` (banner → stderr, info → stdout).
            for pr in deadlocked:
                typer.echo(f"\n{bar}", err=True)
                typer.echo(f"  DEADLOCK: {pr.id} — {pr.title}", err=True)
                typer.echo(
                    f"  author: {pr.author}  approvals: {pr.approval_count}  "
                    f"open change requests: "
                    f"{', '.join(pr.open_change_requests) or '(none)'}",
                    err=True,
                )
                typer.echo(f"{bar}", err=True)
                rebuttals = [r for r in pr.reviews if r.phase == TurnPhase.REBUTTAL]
                if rebuttals:
                    typer.echo(
                        f"  REBUTTAL ({rebuttals[-1].reviewer}): "
                        f"{rebuttals[-1].comment}",
                        err=True,
                    )
                stands = [
                    r for r in pr.reviews
                    if r.phase == TurnPhase.FOLLOWUP
                    and r.verdict == Verdict.REQUEST_CHANGES
                ]
                for s in stands:
                    typer.echo(f"  STAND ({s.reviewer}): {s.comment}", err=True)

                # ``input`` writes its prompt to stdout by default, which
                # would mix banner prose into the JSON channel. Use
                # sys.stderr.write + sys.stdin.readline so the prompt stays
                # on stderr too and stdout remains JSON-only.
                import sys as _sys
                _sys.stderr.write(
                    "  choose [m]erge / [r]eject / [a]dr-options / [s]kip: "
                )
                _sys.stderr.flush()
                line = _sys.stdin.readline()
                if not line:
                    typer.echo(
                        "(no tty — skipping remaining deadlocks)", err=True
                    )
                    break
                choice = line.strip().lower()

                if choice.startswith("m"):
                    try:
                        sha = _merge_pr(ctx, prs, pr.id, force_deadlock=True)
                    except typer.Exit:
                        # _merge_pr already printed the reason on stderr
                        # and persisted any conflict-driven state changes.
                        resolutions.append({"pr": pr.id, "action": "merge_failed"})
                        continue
                    resolutions.append({"pr": pr.id, "action": "merge", "sha": sha})
                elif choice.startswith("r"):
                    pr.status = PRStatus.REJECTED
                    prs[pr.id] = pr
                    resolutions.append({"pr": pr.id, "action": "reject"})
                elif choice.startswith("a"):
                    pr.human_disposition = "adr_options"
                    prs[pr.id] = pr
                    resolutions.append({"pr": pr.id, "action": "adr_options"})
                else:
                    resolutions.append({"pr": pr.id, "action": "skip"})

            PRRegistry.save(root, prs)
            typer.echo(json.dumps({"resolved": resolutions}))
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e


# ---------------------------------------------------------------------------
# adr
# ---------------------------------------------------------------------------


@adr_app.command("record")
def adr_record(
    ctx: typer.Context,
    sprint: str = typer.Option(..., "--sprint"),
) -> None:
    """Compose and commit an ADR summarising the sprint's outcome."""
    root = _root(ctx)
    cfg = _load_sprint_or_die(root, sprint)

    try:
        with state_lock(root):
            prs = PRRegistry.load(root)
            sprint_prs = sorted(
                [p for p in prs.values() if p.sprint_id == sprint],
                key=lambda p: p.created_at,
            )
            if not sprint_prs:
                raise _err(f"no PRs found for sprint {sprint}")

            merged = [p for p in sprint_prs if p.status == PRStatus.MERGED]
            rejected = [p for p in sprint_prs if p.status == PRStatus.REJECTED]
            deadlocked = [p for p in sprint_prs if p.status == PRStatus.DEADLOCKED]
            adr_optioned = [
                p for p in sprint_prs if p.human_disposition == "adr_options"
            ]

            options = [
                f"{p.author.upper()}: {p.title} (status={p.status.value})"
                for p in sprint_prs
            ]
            decision_lines: list[str] = []
            if merged:
                decision_lines.append("Merged:")
                decision_lines.extend(f"  - {p.title} ({p.id})" for p in merged)
            if rejected:
                decision_lines.append("Rejected:")
                decision_lines.extend(
                    f"  - {p.title} ({p.id}): {p.conflict_details or 'no quorum'}"
                    for p in rejected
                )
            if deadlocked:
                decision_lines.append("Deadlocked (unresolved):")
                decision_lines.extend(
                    f"  - {p.title} ({p.id}): open from "
                    f"{', '.join(p.open_change_requests) or '(no open requesters)'}"
                    for p in deadlocked
                )
            if adr_optioned:
                decision_lines.append("Human-resolved as ADR options:")
                decision_lines.extend(
                    f"  - {p.title} ({p.id})" for p in adr_optioned
                )
            decision = "\n".join(decision_lines) or "(no decisions recorded)"

            signoffs = sorted(
                {
                    r.reviewer
                    for p in merged
                    for r in p.reviews
                    if r.verdict == Verdict.APPROVE
                }
            )

            # Status reflects what actually happened in the debate:
            #   - at least one PR merged → "Accepted"
            #   - no merges, at least one rejection → "Rejected"
            #   - everything stuck in review (no merges, no rejections) →
            #     "Proposed" (ADR recorded but outcome is provisional)
            if merged:
                adr_status = "Accepted"
            elif rejected:
                adr_status = "Rejected"
            else:
                adr_status = "Proposed"

            adr_id = f"adr-{_short_id(sprint, *[p.id for p in sprint_prs])}"
            record = DecisionRecord(
                id=adr_id,
                title=f"{cfg.title} — outcome",
                sprint_id=sprint,
                context=cfg.body.strip() or cfg.title,
                options=options,
                decision=decision,
                consequences=(
                    "Downstream sprints inherit the merged artifacts via "
                    "`prerequisite_files`. Rejected PRs leave their branches "
                    "intact for follow-up iterations."
                ),
                signoffs=signoffs,
                status=adr_status,
            )

            eng = _engine(ctx)
            try:
                eng.write_file("main", f"governance/{adr_id}.md", record.to_markdown())
                sha = eng.commit(
                    branch="main",
                    author="ceo",
                    message=f"ADR: {record.title}",
                    files=[f"governance/{adr_id}.md"],
                )
            except GitCommandError as e:
                raise _err(f"failed to commit ADR: {e}") from e
    except StateLockTimeout as e:
        raise _err(str(e)) from e
    except StateSchemaMismatch as e:
        raise _err(str(e)) from e

    typer.echo(json.dumps({"adr_id": adr_id, "sha": sha}))


# ---------------------------------------------------------------------------
# state maintenance
# ---------------------------------------------------------------------------


@state_app.command("unlock")
def state_unlock(ctx: typer.Context) -> None:
    """Force-clear the state lock directory (use only after a crashed holder)."""
    cleared = clear_stale_lock(_root(ctx))
    if cleared:
        typer.echo("lock cleared")
    else:
        typer.echo("no lock to clear")


# ---------------------------------------------------------------------------
# chief customize — zero-touch persona tuning
# ---------------------------------------------------------------------------


def _substitute_tokens(text: str, idea: str) -> str:
    """Replace the documented init tokens with the user's idea string.

    Both ``{{COMPANY_MISSION}}`` and ``{{CORE_PRODUCT_IDEA}}`` map to the same
    ``--idea`` value by design — a single flag drives every template slot.
    """
    return (
        text.replace("{{COMPANY_MISSION}}", idea)
            .replace("{{CORE_PRODUCT_IDEA}}", idea)
    )


def _set_sprint_frontmatter(
    content: str,
    *,
    rounds: int,
    quorum: int,
    participants: list[str],
) -> str:
    """Rewrite the YAML frontmatter of a sprint file, preserving its body.

    Updates ``debate_rounds`` / ``approval_quorum`` / ``participants`` and
    leaves every other frontmatter key and the markdown body untouched.
    Validates ``rounds >= 1`` and ``1 <= quorum <= len(participants)``.
    """
    if rounds < 1:
        raise ValueError("rounds must be >= 1")
    if not (1 <= quorum <= len(participants)):
        raise ValueError(
            "quorum must satisfy 1 <= quorum <= number of participants"
        )

    if not content.startswith("---\n"):
        raise ValueError("sprint file has no YAML frontmatter")
    parts = content.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError("sprint file frontmatter is not closed (missing closing '---')")
    _, front, body = parts

    meta = yaml.safe_load(front) or {}
    meta["debate_rounds"] = rounds
    meta["approval_quorum"] = quorum
    meta["participants"] = participants

    new_front = yaml.safe_dump(meta, sort_keys=False, default_flow_style=None)
    return f"---\n{new_front}---\n{body}"


def _append_operational_bias(content: str, focus: str) -> str:
    """Return ``content`` with ``- {focus}`` appended to ``## Operational Biases``.

    Section boundary = next ``## `` heading (any level-2) or EOF. The new
    bullet is inserted just before the boundary, after trimming trailing
    blank lines so the surrounding structure stays clean.
    """
    lines = content.splitlines(keepends=True)

    start = next(
        (i for i, line in enumerate(lines) if line.strip() == "## Operational Biases"),
        None,
    )
    if start is None:
        raise ValueError("`## Operational Biases` section not found")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break

    insert_at = end
    while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    bullet = f"- {focus.strip()}\n"
    return "".join(lines[:insert_at] + [bullet] + lines[insert_at:])


@chief_app.command("customize")
def chief_customize(
    ctx: typer.Context,
    role: str = typer.Argument(..., help="One of: ceo, cpo, cto, cdo, cco."),
    focus: str = typer.Option(
        ...,
        "--focus",
        help="Bias line to append under the persona's Operational Biases section.",
    ),
) -> None:
    """Append a bias line to ``.claude/agents/<role>.md``.

    Targets the project root resolved by ``--root`` / ``AGENSUITE_ROOT`` / CWD,
    so it works without opening the file in an editor.
    """
    role_norm = role.lower()
    if role_norm not in CHIEF_ROLES:
        raise _err(
            f"unknown role: {role!r} (expected one of: {', '.join(CHIEF_ROLES)})"
        )

    focus_text = focus.strip()
    if not focus_text:
        raise _err("--focus must be a non-empty string")

    agent_file = _root(ctx) / ".claude" / "agents" / f"{role_norm}.md"
    if not agent_file.exists():
        raise _err(f"agent file not found: {agent_file}")

    original = agent_file.read_text(encoding="utf-8")
    try:
        updated = _append_operational_bias(original, focus_text)
    except ValueError as e:
        raise _err(str(e)) from e
    agent_file.write_text(updated, encoding="utf-8")
    typer.echo(str(agent_file))


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover - thin shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
