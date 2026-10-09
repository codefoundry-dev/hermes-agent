"""Copy-on-write for MANAGED skills (CF patch).

On a CF box a profile's ``skills/`` holds two kinds of entry (ai-employees ``bin/sync-skills.sh``):
a SYMLINK into the git checkout is a managed skill, reviewed in a PR; a REAL directory is the
agent's own. ``_find_skill`` walks with ``rglob``, which does not descend into symlinked
directories, so a managed skill was invisible to ``skill_manage`` and every edit failed with
"not found in active profile". A correction JD gave in Slack ("always report this way") was then
lost, because the only place the agent knew to write it refused.

Now an edit to a managed skill FORKS it: the symlink is replaced by a real copy of what it points
at, and the edit lands on the copy. The copy is live at once. The nightly ``box-export.sh`` ships it
as a PR against the managed path, and once that PR is merged ``sync-skills.sh`` sees the copy equal
to git and puts the symlink back. ``.managed_base`` in the copy records where it came from and the
tree hash at fork time, which is how sync-skills tells "merged" from "still waiting for review".

Forking is limited to sessions where a trusted person is talking (``skills.managed_edit_platforms``,
default cli / slack / photon). A stranger-facing session (WhatsApp, webhook) must never be able to
rewrite the reviewed instructions it runs on, and neither may cron or the background reviewer,
because both act with no human in the turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MARKER = ".managed_base"
DEFAULT_PLATFORMS = ("cli", "slack", "photon")
FORKABLE_ACTIONS = frozenset({"edit", "patch", "write_file", "remove_file"})


def tree_hash(root: Path) -> str:
    """sha256 over every regular file under ``root`` (relative path + bytes), marker excluded.
    The same definition is implemented in sync-skills.sh, so the two must stay in step."""
    h = hashlib.sha256()
    files = {p.relative_to(root).as_posix(): p for p in root.rglob("*")
             if p.is_file() and p.name != MARKER}
    for rel in sorted(files):
        path = files[rel]
        h.update(rel.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def managed_link(skills_dir: Path, name: str) -> Optional[Path]:
    """The top-level symlink ``skills_dir/<name>`` if it is a managed skill, else None."""
    if not name or "/" in name or "\\" in name:
        return None
    entry = skills_dir / name
    if entry.is_symlink() and (entry / "SKILL.md").is_file():
        return entry
    return None


def _allowed_platforms() -> tuple:
    try:
        from hermes_cli.config import cfg_get, load_config
        value = cfg_get(load_config(), "skills", "managed_edit_platforms")
    except Exception:
        value = None
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip().lower() for v in value if str(v).strip())
    return DEFAULT_PLATFORMS


def refusal(action: str) -> Optional[str]:
    """None when this session may fork a managed skill, else the reason it may not."""
    if action not in FORKABLE_ACTIONS:
        return (f"it is a reviewed skill kept in git, and '{action}' is not something a live "
                f"copy can do. Ask JD to change it in the repo.")
    try:
        from tools.skill_provenance import is_background_review
        if is_background_review():
            return "the background reviewer may not rewrite a reviewed skill; nobody is in the turn."
    except Exception:
        pass
    try:
        from tools.approval_context import _get_session_platform, _is_cron_approval_context
        if _is_cron_approval_context():
            return "a scheduled job may not rewrite a reviewed skill; nobody is in the turn."
        platform = (_get_session_platform() or "cli").strip().lower()
    except Exception:
        platform = "cli"
    allowed = _allowed_platforms()
    if platform not in allowed:
        return (f"it is a reviewed skill kept in git, and a '{platform}' session may not change it "
                f"(allowed: {', '.join(allowed) or 'none'}). Record the lesson in memory instead.")
    return None


def fork(link: Path) -> Path:
    """Replace the managed symlink with a real copy of its target; return the copy's path.
    The copy is built beside the link and renamed in, so a crash leaves either the link or a
    complete copy, never half of one."""
    source = link.resolve(strict=True)
    staging = link.with_name(f".{link.name}.fork-{os.getpid()}-{int(time.time() * 1000)}")
    shutil.copytree(source, staging, symlinks=False)
    (staging / MARKER).write_text(json.dumps({
        "source": str(source),
        "tree_sha256": tree_hash(source),
        "forked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2) + "\n", encoding="utf-8")
    # rename(2) will not put a directory over a symlink, so unlink first. The caller holds the
    # skill's mutation lock, and readers only ever see the link, nothing, or the full copy.
    link.unlink()
    os.rename(staging, link)
    logger.info("Forked managed skill %s from %s (lands in the next box export)", link.name, source)
    return link


def fork_if_managed(skills_dir: Path, name: str, action: str) -> Optional[str]:
    """Fork ``name`` when it is a managed skill and this session may; error string if it may not,
    None otherwise (including when it is not a managed skill at all)."""
    link = managed_link(skills_dir, name)
    if link is None:
        return None
    reason = refusal(action)
    if reason:
        return f"Skill '{name}' cannot be changed here: {reason}"
    try:
        fork(link)
    except Exception as e:  # noqa: BLE001 - surfaced to the model, never swallowed
        logger.warning("Fork of managed skill %s failed: %s", name, e, exc_info=True)
        return f"Skill '{name}' is a reviewed skill and making an editable copy failed: {e}"
    return None
