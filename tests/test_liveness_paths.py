"""Real process cwd checks, including escaped names from the platform scanner."""

import subprocess
import sys

import pytest

from token_burn.worktree.git import WorktreeError
from token_burn.worktree.liveness import require_idle


@pytest.mark.parametrize("name", ["target\nrow", "literal\\n", "target雪"])
def test_live_nested_cwd_with_unusual_target_path_is_not_missed(tmp_path, name):
    target = tmp_path / name
    nested = target / "deep/a/b"
    nested.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], cwd=nested)
    try:
        with pytest.raises(WorktreeError, match="active_process"):
            require_idle(target)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_sibling_prefix_is_not_a_descendant(tmp_path):
    target = tmp_path / "target"
    sibling = tmp_path / "target-sibling"
    target.mkdir()
    sibling.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], cwd=sibling)
    try:
        require_idle(target)
    finally:
        child.terminate()
        child.wait(timeout=5)
