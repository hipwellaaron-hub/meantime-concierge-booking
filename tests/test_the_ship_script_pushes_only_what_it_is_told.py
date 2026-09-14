"""scripts/ship.sh refuses anything but the commits it was named.

The script this replaces held commits back with the rule "batch two is the
last 4 commits on main, by construction". True on the first run and wrong
on every one after it: a run leaves main as `origin/main + the held
commits`, so the next new commit lands ON TOP of them and the window slides
by one. It shipped a held commit as new work and held a piece of new work
back, five times in a row, and put a migration on the production database
before anyone meant to run it.

It was never tested, and that is the reason it stayed broken for five runs
rather than one. So this drives the real script against a throwaway
repository with a real local origin -- no mocking of git, because the thing
that went wrong was what git actually did with the ranges it was handed.

Every probe asserts WHERE ORIGIN ENDED UP, not just the exit code. A script
that refuses loudly and pushes anyway is the failure being prevented.
"""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

SHIP = pathlib.Path("scripts/ship.sh").resolve()

# A minimal but REAL alembic chain, so the graph check under test is doing
# the same work it does against the project's own migrations.
REVISION = '''"""r{n}

Revision ID: {rev}
Revises: {down}
"""
revision = "{rev}"
down_revision = {down_literal}
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


@pytest.fixture()
def repo(tmp_path):
    """A working repo on `main` with a bare `origin`, one commit pushed."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)

    _git(work, "config", "user.email", "test@local")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(origin))

    (work / "scripts").mkdir()
    shutil.copy(SHIP, work / "scripts" / "ship.sh")
    (work / "alembic" / "versions").mkdir(parents=True)
    (work / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n", encoding="utf-8")
    _add_revision(work, "aaaa", None)

    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "main")
    return work


def _add_revision(work, rev, down):
    down_literal = "None" if down is None else f'"{down}"'
    (work / "alembic" / "versions" / f"{rev}_.py").write_text(
        REVISION.format(n=rev, rev=rev, down=down or "", down_literal=down_literal),
        encoding="utf-8",
    )


def _commit(work, subject, *, touch="app.py"):
    path = work / touch
    path.write_text((path.read_text(encoding="utf-8") if path.exists() else "") + subject + "\n",
                    encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", subject)
    return _git(work, "rev-parse", "HEAD").stdout.strip()


def _ship(work, *args):
    env = dict(os.environ, SHIP_PYTHON=sys.executable)
    return subprocess.run(
        ["bash", "scripts/ship.sh", *args],
        cwd=work, capture_output=True, text=True, env=env,
    )


def _origin_head(work):
    return _git(work, "rev-parse", "origin/main").stdout.strip()


def _origin_subjects(work):
    _git(work, "fetch", "-q", "origin", "main")
    out = _git(work, "log", "--format=%s", "origin/main").stdout.split()
    return out


# --- the prefix rule --------------------------------------------------------


def test_naming_every_unpushed_commit_pushes_them_all(repo):
    a = _commit(repo, "one")
    b = _commit(repo, "two")

    result = _ship(repo, a, b)

    assert result.returncode == 0, result.stderr
    assert _origin_head(repo) == b


def test_naming_a_prefix_pushes_the_prefix_and_holds_the_rest(repo):
    """The capability the old script was trying to provide, and the one it
    got wrong: ship what is ready, keep the rest local."""
    a = _commit(repo, "one")
    b = _commit(repo, "two")
    _commit(repo, "three-held-back")

    result = _ship(repo, a, b)

    assert result.returncode == 0, result.stderr
    assert _origin_head(repo) == b
    assert "three-held-back" not in _origin_subjects(repo)
    assert "holding back 1 commit" in result.stdout


def test_skipping_a_commit_in_the_middle_is_refused(repo):
    """EXACTLY THE INCIDENT. The old script decided for itself which
    commits were "new work" and got it off by one, so a commit nobody named
    went to production. Naming the 1st and 3rd cannot mean "push the 3rd"."""
    a = _commit(repo, "one")
    _commit(repo, "two")
    c = _commit(repo, "three")
    before = _origin_head(repo)

    result = _ship(repo, a, c)

    assert result.returncode != 0
    assert "REFUSING" in result.stderr
    assert _origin_head(repo) == before, "it refused and pushed anyway"


def test_naming_a_later_commit_first_is_refused(repo):
    """A commit that IS unpushed, named out of order. It is still not the
    oldest unpushed one, and pushing its tip would carry the one before it."""
    a = _commit(repo, "one")
    b = _commit(repo, "two")
    before = _origin_head(repo)

    result = _ship(repo, b, a)

    assert result.returncode != 0
    assert _origin_head(repo) == before


def test_naming_a_commit_that_is_already_pushed_is_refused(repo):
    already = _git(repo, "rev-parse", "origin/main").stdout.strip()
    _commit(repo, "one")
    before = _origin_head(repo)

    result = _ship(repo, already)

    assert result.returncode != 0
    assert _origin_head(repo) == before


def test_naming_more_commits_than_are_unpushed_is_refused(repo):
    """Asserted on the MESSAGE, not just the exit code.

    Without the count check the prefix loop reads past the end of the
    unpushed array, which under `set -u` aborts with "unbound variable" --
    a refusal, so an exit-code-only assertion passed with the check
    deleted. Mutation-checked, and this is what it caught.
    """
    a = _commit(repo, "one")
    before = _origin_head(repo)

    result = _ship(repo, a, a)

    assert result.returncode != 0
    assert "than are unpushed" in result.stderr, (
        f"refused for some other reason: {result.stderr}"
    )
    assert "unbound variable" not in result.stderr
    assert _origin_head(repo) == before


def test_naming_nothing_prints_what_is_unpushed_and_pushes_nothing(repo):
    _commit(repo, "one")
    before = _origin_head(repo)

    result = _ship(repo)

    assert result.returncode == 2
    assert "one" in result.stderr, "it did not show what is waiting to go"
    assert _origin_head(repo) == before


def test_an_unknown_sha_is_refused_as_an_unknown_sha(repo):
    """And says so, rather than falling through to the ordering complaint.

    Without the early exit, the unresolved argument becomes an empty string
    that fails the prefix comparison -- so the script still refuses, but
    tells you the commits are in the wrong order when the real problem is
    that one of them does not exist. An exit-code-only assertion passed
    with the exit deleted; this is what caught it.
    """
    _commit(repo, "one")
    before = _origin_head(repo)

    result = _ship(repo, "0" * 40)

    assert result.returncode != 0
    assert "not a commit" in result.stderr
    assert "not the oldest unpushed" not in result.stderr, (
        "an unknown sha was reported as an ordering problem"
    )
    assert _origin_head(repo) == before


def test_nothing_to_ship_is_refused_rather_than_pushed_empty(repo):
    result = _ship(repo, "HEAD")

    assert result.returncode != 0
    assert "nothing to ship" in result.stderr


# --- the graph check --------------------------------------------------------


def test_a_dangling_down_revision_in_the_pushed_tree_is_refused(repo):
    """THE deploy failure. A revision whose parent is not in the tree being
    pushed resolves fine in the working directory and dies at
    PRE_DEPLOY_COMMAND with KeyError. The old script checked the working
    directory, which is why it shipped three times into that failure."""
    _add_revision(repo, "cccc", "bbbb-which-is-not-here")
    a = _commit(repo, "adds a revision with a missing parent")
    before = _origin_head(repo)

    result = _ship(repo, a)

    assert result.returncode != 0
    assert "alembic graph does not resolve" in result.stderr
    assert _origin_head(repo) == before, "it pushed a tree whose migrations cannot load"


def test_a_resolvable_chain_is_pushed_and_its_head_reported(repo):
    """The positive control. If the graph check refused everything, every
    refusal above would pass for the wrong reason."""
    _add_revision(repo, "bbbb", "aaaa")
    a = _commit(repo, "adds a revision that chains properly")

    result = _ship(repo, a)

    assert result.returncode == 0, result.stderr
    assert "bbbb" in result.stdout, "the head of the pushed tree was not reported"
    assert _origin_head(repo) == a


def test_two_heads_in_the_pushed_tree_are_refused(repo):
    """Two roots is a merge nobody wrote, and `alembic upgrade head` on
    production becomes ambiguous rather than wrong-but-deterministic."""
    _add_revision(repo, "dddd", None)
    a = _commit(repo, "adds a second root revision")
    before = _origin_head(repo)

    result = _ship(repo, a)

    assert result.returncode != 0
    assert _origin_head(repo) == before
