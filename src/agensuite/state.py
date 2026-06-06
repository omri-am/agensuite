"""Atomic JSON registries for PRs and per-sprint debate state.

Persistence layout under ``<root>/state/``::

    state/
      .lock/                  # directory used as the cross-process mutex
        owner                 # pid/host/created-at of current lockholder
      prs.json                # PR registry (id -> PullRequest)
      debates/
        <sprint_id>.json      # DebateState per sprint

All writes go through :func:`_atomic_write` (temp file + ``fsync`` +
``os.replace``) and every read/write site is expected to be wrapped in
:func:`state_lock`. The lock is a directory created via ``os.mkdir`` — see
the docstring on :func:`state_lock` for why this is preferred over
``fcntl.flock``.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import DebateState, PullRequest


# Bump this whenever an on-disk schema change is incompatible with the
# previous format. Old state files trigger a friendly "run bootstrap
# --reset" error rather than a cryptic Pydantic ValidationError — state is
# regenerable from ``sprints/*.md`` so there's no migration path.
STATE_SCHEMA_VERSION = 3


class StateSchemaMismatch(ValueError):
    """On-disk state predates the current ``STATE_SCHEMA_VERSION``."""

    def __init__(self, path: Path, found: object) -> None:
        self.path = path
        self.found = found
        super().__init__(
            f"state file at {path} has schema_version={found!r} but the "
            f"current CLI expects {STATE_SCHEMA_VERSION}. State is "
            "regenerable from sprints/*.md — run 'agensuite bootstrap --reset' "
            "to wipe and start over."
        )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _state_dir(root: Path) -> Path:
    return root / "state"


def _prs_path(root: Path) -> Path:
    return _state_dir(root) / "prs.json"


def _debate_path(root: Path, sprint_id: str) -> Path:
    return _state_dir(root) / "debates" / f"{sprint_id}.json"


def _lock_dir(root: Path) -> Path:
    return _state_dir(root) / ".lock"


def ensure_dirs(root: Path) -> None:
    """Create the ``state/`` and ``state/debates/`` directories.

    Idempotent — safe to call before every operation. The git workspace is
    created separately by :class:`agensuite.git_engine.GitEngine`.
    """
    (_state_dir(root) / "debates").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Strategy:
      1. ``tempfile.mkstemp`` allocates a unique sibling temp file in
         ``path.parent`` — even if two callers ever bypass :func:`state_lock`
         (e.g. during a ``clear_stale_lock`` race), they get distinct temp
         paths so neither corrupts the other's in-flight write.
      2. ``fsync`` the temp file's descriptor before closing, so the
         contents actually hit the underlying device before we swap the inode.
      3. ``os.replace`` is the cross-platform "atomic-overwrite rename": on
         POSIX it's a single syscall, on Windows it maps to ``MoveFileEx``
         with ``MOVEFILE_REPLACE_EXISTING``. Both require the source and
         destination on the same filesystem, which the same-directory temp
         guarantees.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some filesystems (notably tmpfs in CI) reject fsync; the
                # write still lands, so degrade gracefully.
                pass
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of orphan temp file on failure.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Cross-platform directory lock
# ---------------------------------------------------------------------------


class StateLockTimeout(TimeoutError):
    """Could not acquire the state lock within the timeout."""

    def __init__(self, lock_path: Path, timeout_s: float, owner: str | None) -> None:
        self.lock_path = lock_path
        self.timeout_s = timeout_s
        self.owner = owner
        owner_blurb = f" held by {owner}" if owner else ""
        super().__init__(
            f"state lock at {lock_path}{owner_blurb} not released within {timeout_s}s; "
            "if the holder has died, remove the directory manually or call "
            "agensuite.state.clear_stale_lock()."
        )


def _read_lock_owner(lock_path: Path) -> str | None:
    """Read the ``owner`` metadata file inside the lock directory, if any."""
    owner_file = lock_path / "owner"
    try:
        return owner_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_lock_owner(lock_path: Path) -> None:
    """Stamp ``<lock>/owner`` with pid/host/timestamp so debugging is possible.

    Best-effort: any failure here is non-fatal. If we crash between mkdir
    and writing the owner file, ``_read_lock_owner`` simply returns ``None``.
    """
    try:
        marker = (
            f"pid={os.getpid()} host={socket.gethostname()} "
            f"created_at={int(time.time())}\n"
        )
        (lock_path / "owner").write_text(marker, encoding="utf-8")
    except OSError:
        pass


@contextmanager
def state_lock(
    root: Path,
    timeout_s: float = 10.0,
    poll_interval_s: float = 0.05,
) -> Iterator[None]:
    """Acquire an exclusive lock on the state directory for the duration of ``with``.

    Uses ``os.mkdir`` rather than ``fcntl.flock`` for two reasons:

    1. **Portability.** ``fcntl`` does not exist on Windows; ``msvcrt.locking``
       has different semantics; the directory approach works identically on
       Linux, macOS, and Windows.
    2. **Filesystem safety.** ``mkdir`` is kernel-atomic on every filesystem
       we expect to encounter (ext4, APFS, NTFS, tmpfs). Advisory locks
       (``flock``) are unreliable on networked filesystems like NFS.

    Trade-off: there is no automatic cleanup if the holder crashes. The owner
    metadata file lets a human (or :func:`clear_stale_lock`) decide whether
    to remove a stale directory.
    """
    state_root = _state_dir(root)
    state_root.mkdir(parents=True, exist_ok=True)
    lock = _lock_dir(root)

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise StateLockTimeout(
                    lock_path=lock,
                    timeout_s=timeout_s,
                    owner=_read_lock_owner(lock),
                )
            time.sleep(poll_interval_s)

    _write_lock_owner(lock)
    try:
        yield
    finally:
        # Best-effort release: remove owner file then the directory itself.
        # Even if cleanup fails (e.g. permission flip mid-flight) we don't
        # raise from the finally block — that would mask the actual error
        # from the wrapped operation.
        try:
            owner_file = lock / "owner"
            if owner_file.exists():
                owner_file.unlink()
        except OSError:
            pass
        try:
            os.rmdir(lock)
        except OSError:
            pass


def clear_stale_lock(root: Path) -> bool:
    """Forcibly remove the state lock directory.

    Returns ``True`` if a lock was actually cleared, ``False`` if there was
    nothing to clear. Intended for human / CLI use after a crashed holder
    leaves the directory behind; never call this from within normal program
    flow because it can race with a live holder.
    """
    lock = _lock_dir(root)
    if not lock.exists():
        return False

    owner_file = lock / "owner"
    if owner_file.exists():
        try:
            owner_file.unlink()
        except OSError:
            pass
    try:
        os.rmdir(lock)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# PR registry
# ---------------------------------------------------------------------------


class PRRegistry:
    """JSON-backed map ``pr_id -> PullRequest``.

    Stateless — all methods take ``root`` explicitly so a single process can
    operate on multiple project roots in tests.
    """

    @staticmethod
    def load(root: Path) -> dict[str, PullRequest]:
        path = _prs_path(root)
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"corrupt PR registry at {path}: {e.msg} (line {e.lineno}, col {e.colno})"
            ) from e
        if not isinstance(raw, dict):
            raise ValueError(
                f"PR registry at {path} must be a JSON object, got {type(raw).__name__}"
            )
        version = raw.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise StateSchemaMismatch(path, version)
        prs_block = raw.get("prs", {})
        if not isinstance(prs_block, dict):
            raise ValueError(
                f"PR registry at {path} has invalid 'prs' block: "
                f"expected object, got {type(prs_block).__name__}"
            )
        return {pid: PullRequest.model_validate(pr) for pid, pr in prs_block.items()}

    @staticmethod
    def save(root: Path, prs: dict[str, PullRequest]) -> None:
        wrapped = {
            "schema_version": STATE_SCHEMA_VERSION,
            "prs": {
                pid: json.loads(pr.model_dump_json()) for pid, pr in prs.items()
            },
        }
        _atomic_write(_prs_path(root), json.dumps(wrapped, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Debate store
# ---------------------------------------------------------------------------


class DebateStore:
    """One JSON file per sprint under ``state/debates/<sprint_id>.json``."""

    @staticmethod
    def load(root: Path, sprint_id: str) -> DebateState | None:
        path = _debate_path(root, sprint_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"corrupt debate state at {path}: {e.msg} (line {e.lineno}, col {e.colno})"
            ) from e
        if not isinstance(raw, dict):
            raise ValueError(
                f"debate state at {path} must be a JSON object, got {type(raw).__name__}"
            )
        version = raw.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise StateSchemaMismatch(path, version)
        debate_block = raw.get("debate")
        if not isinstance(debate_block, dict):
            raise ValueError(
                f"debate state at {path} missing 'debate' block"
            )
        return DebateState.model_validate(debate_block)

    @staticmethod
    def save(root: Path, state: DebateState) -> None:
        wrapped = {
            "schema_version": STATE_SCHEMA_VERSION,
            "debate": json.loads(state.model_dump_json()),
        }
        _atomic_write(
            _debate_path(root, state.sprint_id),
            json.dumps(wrapped, indent=2) + "\n",
        )

    @staticmethod
    def list_sprint_ids(root: Path) -> list[str]:
        """All sprint ids that currently have persisted debate state."""
        debates_dir = _state_dir(root) / "debates"
        if not debates_dir.exists():
            return []
        return sorted(p.stem for p in debates_dir.glob("*.json"))
