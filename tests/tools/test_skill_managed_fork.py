"""CF patch: an edit to a managed (symlinked) skill forks it into a real copy, from trusted sessions only."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tools.skill_managed_fork import MARKER, tree_hash
from tools.skill_manager_tool import skill_manage

SKILL = """\
---
name: reply
description: How to answer a couple.
---

# Reply

Step 1: Be warm.
"""


@contextmanager
def _box(tmp_path, platform="slack", cron=False, review=False):
    """A profile skills/ dir holding one managed skill linked in from a fake git checkout."""
    repo = tmp_path / "repo" / "reply"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text(SKILL)
    (repo / "references").mkdir()
    (repo / "references" / "notes.md").write_text("old notes\n")
    live = tmp_path / "skills"
    live.mkdir()
    (live / "reply").symlink_to(repo)
    with patch("tools.skill_manager_tool.SKILLS_DIR", live), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[live]), \
         patch("tools.approval_context._get_session_platform", return_value=platform), \
         patch("tools.approval_context._is_cron_approval_context", return_value=cron), \
         patch("tools.skill_provenance.is_background_review", return_value=review), \
         patch("tools.skill_managed_fork._allowed_platforms", return_value=("cli", "slack", "photon")):
        yield live, repo


def _patch(**kw):
    return json.loads(skill_manage(action="patch", name="reply", old_string="Be warm.",
                                   new_string="Be warm, and short.", **kw))


def test_patch_forks_and_leaves_git_untouched(tmp_path):
    with _box(tmp_path) as (live, repo):
        before = tree_hash(repo)
        result = _patch()
        assert result["success"], result
        assert not (live / "reply").is_symlink()
        assert "Be warm, and short." in (live / "reply" / "SKILL.md").read_text()
        assert (live / "reply" / "references" / "notes.md").read_text() == "old notes\n"
        assert "Be warm, and short." not in (repo / "SKILL.md").read_text()
        base = json.loads((live / "reply" / MARKER).read_text())
        assert base["tree_sha256"] == before
        assert base["source"] == str(repo.resolve())


def test_second_edit_hits_the_fork(tmp_path):
    with _box(tmp_path) as (live, _repo):
        assert _patch()["success"]
        again = json.loads(skill_manage(action="patch", name="reply", old_string="and short.",
                                        new_string="and short. Never pushy."))
        assert again["success"], again
        assert "Never pushy." in (live / "reply" / "SKILL.md").read_text()


def test_tree_hash_ignores_marker(tmp_path):
    with _box(tmp_path) as (live, repo):
        assert json.loads(skill_manage(action="write_file", name="reply",
                                       file_path="references/notes.md",
                                       file_content="old notes\n"))["success"]
        # Same content as git, marker aside: this is what sync-skills reads as "merged".
        assert tree_hash(live / "reply") == tree_hash(repo)


@pytest.mark.parametrize("platform", ["whatsapp_cloud", "webhook"])
def test_stranger_facing_sessions_cannot_fork(tmp_path, platform):
    with _box(tmp_path, platform=platform) as (live, _repo):
        result = _patch()
        assert not result["success"]
        assert platform in result["error"]
        assert (live / "reply").is_symlink()


def test_cron_cannot_fork(tmp_path):
    with _box(tmp_path, cron=True) as (live, _repo):
        assert not _patch()["success"]
        assert (live / "reply").is_symlink()


def test_background_review_cannot_fork(tmp_path):
    with _box(tmp_path, review=True) as (live, _repo):
        result = json.loads(skill_manage(action="patch", name="reply", old_string="Be warm.",
                                         new_string="x"))
        assert not result["success"]
        assert (live / "reply").is_symlink()


def test_delete_of_managed_skill_is_refused_and_explained(tmp_path):
    with _box(tmp_path) as (live, repo):
        result = json.loads(skill_manage(action="delete", name="reply"))
        assert not result["success"]
        assert "reviewed skill" in result["error"]
        assert (live / "reply").is_symlink() and (repo / "SKILL.md").exists()


def test_create_over_managed_name_never_writes_into_git(tmp_path):
    with _box(tmp_path) as (_live, repo):
        result = json.loads(skill_manage(action="create", name="reply", content=SKILL))
        assert not result["success"]
        assert (repo / "SKILL.md").read_text() == SKILL


def test_batch_forks_before_snapshot(tmp_path):
    with _box(tmp_path) as (live, _repo):
        result = json.loads(skill_manage(action="patch", name="reply", operations=[
            {"action": "patch", "name": "reply", "old_string": "Be warm.", "new_string": "Be kind."},
            {"action": "write_file", "name": "reply", "file_path": "references/new.md",
             "file_content": "new\n"},
        ]))
        assert result["success"], result
        assert "Be kind." in (live / "reply" / "SKILL.md").read_text()
        assert (live / "reply" / "references" / "new.md").exists()


def test_failed_batch_keeps_a_usable_skill(tmp_path):
    with _box(tmp_path) as (live, _repo):
        result = json.loads(skill_manage(action="patch", name="reply", operations=[
            {"action": "patch", "name": "reply", "old_string": "Be warm.", "new_string": "Be kind."},
            {"action": "patch", "name": "reply", "old_string": "NOT THERE", "new_string": "x"},
        ]))
        assert not result["success"]
        # Rolled back to the forked original, which still loads; sync-skills relinks it as merged.
        assert (live / "reply" / "SKILL.md").read_text() == SKILL
