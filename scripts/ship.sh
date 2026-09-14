#!/bin/bash
#
# Push main -- but only the commits you NAME, and only after checking the
# alembic graph in the tree those commits actually produce.
#
#   ./scripts/ship.sh <sha> [<sha> ...]
#
# The named commits must be the OLDEST unpushed commits on main, in order,
# with nothing skipped. Anything else is refused.
#
# WHY IT WORKS THIS WAY
#
# The version this replaces held four commits back by taking them as "the
# last 4 commits on main, by construction". That held for exactly one run.
# After a run, main is `origin/main + the held commits`, so the next new
# commit landed ON TOP of the held ones and the window slid by one: it
# pushed a held commit as new work and kept a piece of new work back. It
# did that five times, which put a migration on the production database ten
# days before anyone meant to run it.
#
# The lesson is not "compute the window better". It is that the script was
# deciding WHICH commits to push from a rule, and a rule that is right on
# Tuesday is wrong on Wednesday. So it does not decide any more. You say
# what is going, it checks you are right, and it refuses if the range it
# would actually push differs from what you named by so much as one commit.
#
# AND IT CHECKS THE TREE IT IS PUSHING, not the working directory. The old
# script printed `alembic heads` from the working tree and pushed a
# different one: the working tree had c2f8d61a94b7, the pushed tree did
# not, f3d9b7c1a468 pointed at it, and three production deploys in a row
# died on KeyError at PRE_DEPLOY_COMMAND. A check that reads a tree nobody
# is deploying is decoration.
#
set -euo pipefail

cd "$(dirname "$0")/.."

# Overridable so the test can drive this script against a throwaway
# repository. A shipping tool that cannot be exercised is how the last
# one stayed broken for five runs.
PYTHON="${SHIP_PYTHON:-.venv/Scripts/python.exe}"

if [ "$#" -eq 0 ]; then
    cat >&2 <<'USAGE'
usage: scripts/ship.sh <sha> [<sha> ...]

Name every commit you intend to push, oldest first. Unpushed commits on main:
USAGE
    git --no-pager log --oneline --reverse origin/main..main >&2 || true
    exit 2
fi

git fetch -q origin main

UNPUSHED=()
while read -r sha; do
    [ -n "$sha" ] && UNPUSHED+=("$sha")
done < <(git rev-list --reverse origin/main..main)

if [ "${#UNPUSHED[@]}" -eq 0 ]; then
    echo "nothing to ship -- main is not ahead of origin/main" >&2
    exit 1
fi

NAMED=()
for arg in "$@"; do
    if ! full=$(git rev-parse --verify --quiet "${arg}^{commit}"); then
        echo "*** not a commit: $arg ***" >&2
        exit 1
    fi
    NAMED+=("$full")
done

if [ "${#NAMED[@]}" -gt "${#UNPUSHED[@]}" ]; then
    echo "*** you named more commits (${#NAMED[@]}) than are unpushed (${#UNPUSHED[@]}) ***" >&2
    exit 1
fi

# THE NAMED LIST MUST BE A PREFIX of what is unpushed, position by position.
#
# A prefix is the only thing a linear branch can push: pushing the 1st and
# 3rd unpushed commits without the 2nd is not a thing git can do, and a
# script that silently pushed the 3rd anyway is how the old one leaked. So
# "hold the last few back" stays possible and "skip one in the middle" is
# refused rather than quietly reinterpreted.
for i in "${!NAMED[@]}"; do
    if [ "${NAMED[$i]}" != "${UNPUSHED[$i]}" ]; then
        {
            echo "*** REFUSING: the commits you named are not the oldest unpushed ones ***"
            echo
            echo "    at position $((i + 1)) you named:   ${NAMED[$i]}  $(git log -1 --format=%s "${NAMED[$i]}")"
            echo "    but what is actually there is:      ${UNPUSHED[$i]}  $(git log -1 --format=%s "${UNPUSHED[$i]}")"
            echo
            echo "    unpushed on main, oldest first:"
            git --no-pager log --oneline --reverse origin/main..main | sed 's/^/      /'
        } >&2
        exit 1
    fi
done

TIP="${NAMED[$((${#NAMED[@]} - 1))]}"
HELD=$(( ${#UNPUSHED[@]} - ${#NAMED[@]} ))

# Resolve the alembic graph in the TREE BEING PUSHED. get_heads() raises on
# a dangling down_revision, and walking each head to base proves every
# parent in between really resolves -- which is the failure that took the
# booking service down three times.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git archive "$TIP" alembic alembic.ini | tar -x -C "$WORK"

if ! HEADS=$("$PYTHON" - "$WORK" <<'PY'
import pathlib
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

work = pathlib.Path(sys.argv[1])
cfg = Config(str(work / "alembic.ini"))
cfg.set_main_option("script_location", str(work / "alembic"))
script = ScriptDirectory.from_config(cfg)
heads = script.get_heads()
for head in heads:
    list(script.iterate_revisions(head, "base"))
if len(heads) != 1:
    sys.exit(f"multiple heads: {heads}")
print(heads[0])
PY
); then
    echo "*** REFUSING: the alembic graph does not resolve in the tree being pushed ***" >&2
    exit 1
fi

echo "shipping ${#NAMED[@]} commit(s) to origin/main:"
git --no-pager log --oneline --reverse "origin/main..$TIP" | sed 's/^/    /'
[ "$HELD" -gt 0 ] && echo "holding back $HELD commit(s) on the local branch"
echo "alembic head in the pushed tree: $HEADS"

git push -q origin "$TIP:main"
echo "SHIPPED $TIP"
