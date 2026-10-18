#!/usr/bin/env bash
# Deploy origin/main to this checkout, the live deployment, when that needs no step from the maintainer.  It stops at
# the first of these checks that fails:
#   1. this checkout is on main with no local changes, and origin/main is a fast-forward of it
#   2. the commits change nothing that applies outside this checkout: systemd/, config/, scripts/strata-health.sh and
#      scripts/install-system-files.sh, nor scripts/check-drift.sh, which check 5 relies on and CI cannot run
#   3. strata-q3.service is not starting or stopping
#   4. the newest run of .github/workflows/test.yml for the push of that commit to main passed
#   5. scripts/test.sh and scripts/check-drift.sh of that commit pass here.  So even without --apply, the script runs
#      code of origin/main on this machine
# Commits that check 2 stops are deployed by hand (docs/runbooks/deploy.md).  With --apply it checks the checkout and
# the service again and fast-forwards.  It never runs sudo and restarts nothing: strata-q3.service runs the new
# preflight, wait-ready and tools/ at its next start.
# Exit 0 when the checks pass (and with --apply, when deployed) or when up to date; 1 when it refuses or is interrupted,
# or when check-drift.sh finds drift after the fast-forward; 2 on bad usage.
# Usage: scripts/deploy.sh [--apply]
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
UNIT=strata-q3.service
WORKFLOW=test.yml                                 # in .github/workflows/
MANUAL=(systemd/ config/ scripts/strata-health.sh scripts/install-system-files.sh scripts/check-drift.sh)
AT_START=(scripts/strata-preflight.sh scripts/strata-wait-ready.sh tools/)
LIMIT=600                                         # seconds for each check of the new commit
# no background gc that keeps the lock open after the script; file names quoted when they hold control characters
GIT_OPTS=(-c gc.auto=0 -c maintenance.auto=false -c core.quotePath=true)
tmp=
why=
child=

ok()     { printf 'ok     %s\n' "$1"; }
refuse() { printf 'not deployed: %s\n' "$1" >&2; exit 1; }
git()    { command git "${GIT_OPTS[@]}" "$@"; }
# shellcheck disable=SC2329  # the trap in main calls it
stop()   { [ -z "$child" ] || kill -TERM -- "-$child" "$child" 2> /dev/null; refuse "interrupted"; }
# commit subjects and test output come from other people: no control or bidi characters on the terminal
printable() {
  python3 -I -c 'import sys, unicodedata
text = sys.stdin.buffer.read().decode("utf-8", "replace")
sys.stdout.write("".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp")))'
}
# seconds, output file (- for the output of this script), command...: timeout runs the command in a process group of
# its own, which a Ctrl-C on the terminal does not reach; so it runs in the background, where the trap can stop it
limited() {
  local seconds=$1 out=$2 rc; shift 2
  if [ "$out" = - ]; then timeout -k 10 "$seconds" "$@" < /dev/null 9>&- &
  else timeout -k 10 "$seconds" "$@" > "$out" 2>&1 < /dev/null 9>&- &
  fi
  child=$!
  wait "$child"; rc=$?
  child=
  return $rc
}
checkout_clean() {   # refuses unless this checkout is on main with no local changes
  local st
  [ "$(git symbolic-ref -q --short HEAD)" = main ] || refuse "this checkout is not on branch main"
  st=$(git status --porcelain --untracked-files=all) || refuse "git status failed"
  [ -z "$st" ] || refuse "this checkout has local changes (git status)"
  st=$(git ls-files -v) || refuse "git ls-files failed"
  if grep -q -E '^([a-z]|S) ' <<< "$st"; then
    refuse "this checkout hides files from git status (skip-worktree or assume-unchanged: git ls-files -v)"
  fi
}
service_idle() {   # prints the unit's state; fails while it starts or stops or has a job queued
  local state job
  state=$(systemctl show -p ActiveState --value "$UNIT") || { echo "in an unknown state"; return 1; }
  job=$(systemctl show -p Job --value "$UNIT") || { echo "in an unknown state"; return 1; }
  [ -z "$job" ] || { echo "$state, with job $job queued"; return 1; }
  echo "${state:-in an unknown state}"
  case $state in active|inactive|failed) ;; *) return 1 ;; esac
}
check_of_target() {   # script, its last line on success, log: within $LIMIT s; sets $why
  local rc
  limited "$LIMIT" "$3" "$1"; rc=$?
  if [ $rc = 124 ] || [ $rc = 137 ]; then why="it did not finish in $LIMIT s"
  elif [ $rc != 0 ]; then why="exit $rc"
  elif [ "$(tail -n 1 "$3")" != "$2" ]; then why="exit 0, but no \"$2\" at the end"
  else
    grep -q '^DRIFT' "$3"; rc=$?
    case $rc in 0) why="exit 0, but with DRIFT lines" ;; 1) return 0 ;; *) why="its output cannot be read" ;; esac
  fi
  return 1
}

# all of it in one function, which bash reads in full before running it: --apply replaces this file
main() {
  local apply head target short url repo state changed manual
  local github='^(https://([^/@]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$'
  case "$*" in '') apply=0 ;; --apply) apply=1 ;; *) echo "usage: scripts/deploy.sh [--apply]" >&2; exit 2 ;; esac
  trap stop INT TERM HUP
  command -v git > /dev/null || refuse "git is not installed"
  # shellcheck disable=SC2046  # git's repository variables, one word each: a caller's GIT_DIR (a hook) must not apply
  unset $(command git rev-parse --local-env-vars)
  export GIT_TERMINAL_PROMPT=0
  cd "$D" || exit 1
  exec 9> .git/deploy.lock || refuse "cannot open .git/deploy.lock"
  flock -n 9; case $? in 0) ;; 1) refuse "another scripts/deploy.sh is running" ;; *) refuse "flock failed" ;; esac

  checkout_clean
  limited 120 - git "${GIT_OPTS[@]}" fetch -q origin +refs/heads/main:refs/remotes/origin/main ||
    refuse "git fetch origin main failed"
  head=$(git rev-parse -q --verify 'HEAD^{commit}') || refuse "cannot read HEAD"
  target=$(git rev-parse -q --verify 'refs/remotes/origin/main^{commit}') || refuse "cannot read origin/main"
  if [ "$head" = "$target" ]; then echo "up to date: $(git log -1 --format='%h %s' | printable)"; exit 0; fi
  short=$(git rev-parse --short "$target")
  git merge-base --is-ancestor "$head" "$target" ||
    refuse "origin/main ($short) is not a fast-forward: this checkout has commits that origin/main does not"
  echo "origin/main is $(git rev-list --count "$head..$target") commit(s) ahead of this checkout:"
  git log --format='  %h %s' "$head..$target" | printable
  ok "on main, no local changes, and a fast-forward to $short"

  manual=$(git diff --name-only "$head" "$target" -- "${MANUAL[@]}" | tr '\n' ' ') || refuse "git diff failed"
  [ -z "$manual" ] || refuse "the commits change ${manual% }, which apply outside this checkout; deploy them by hand"
  ok "nothing in the commits applies outside this checkout"

  state=$(service_idle) || refuse "$UNIT is $state; deploy when it is not starting or stopping"
  ok "$UNIT is $state"

  url=$(git config --get remote.origin.url); url=${url%/}; url=${url%.git}
  shopt -s nocasematch
  [[ $url =~ $github ]] || refuse "origin is not a GitHub repository: $(sed -E 's#^([a-z+]+://)[^/@]*@#\1#' <<< "$url")"
  shopt -u nocasematch
  repo=${BASH_REMATCH[3]}
  tmp=$(mktemp -d) || refuse "mktemp failed"
  trap 'rm -rf -- "$tmp"' EXIT
  url="https://api.github.com/repos/$repo/actions/workflows/$WORKFLOW/runs"
  curl -q -fsS --max-time 30 -H 'Accept: application/vnd.github+json' \
    "$url?head_sha=$target&event=push&branch=main&per_page=100" > "$tmp/runs.json" ||
    refuse "cannot read the runs of $WORKFLOW for $short from GitHub"
  state=$(python3 -I - "$tmp/runs.json" "$target" ".github/workflows/$WORKFLOW" <<'EOF'
import json, sys
path, sha, workflow = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    runs = [r for r in data["workflow_runs"] if r["path"] == workflow and r["head_sha"] == sha
            and r["event"] == "push" and r["head_branch"] == "main"]
    if data["total_count"] > len(data["workflow_runs"]):
        print("unreadable: more runs than one page")
    elif not runs:
        print("missing")
    else:
        newest = max(runs, key=lambda r: r["run_number"])
        print(f"{newest['status']} {newest['conclusion'] or ''}".strip())
except (OSError, ValueError, KeyError, TypeError):
    print("unreadable")
EOF
)
  [ "$state" = "completed success" ] || refuse "the newest run of $WORKFLOW for $short is ${state:-unreadable}"
  ok "the newest run of $WORKFLOW for $short passed"

  mkdir "$tmp/tree" || refuse "mkdir failed"
  git archive "$target" | tar -x -C "$tmp/tree" || refuse "cannot export $short"
  if ! check_of_target "$tmp/tree/scripts/test.sh" "all checks passed" "$tmp/test.log"; then
    sed 's/^/  /' "$tmp/test.log" | printable
    refuse "scripts/test.sh of $short failed: $why"
  fi
  ok "scripts/test.sh of $short: all checks passed"
  if ! check_of_target "$tmp/tree/scripts/check-drift.sh" "no drift" "$tmp/drift.log"; then
    sed 's/^/  /' "$tmp/drift.log" | printable
    refuse "scripts/check-drift.sh of $short failed: $why; docs/runbooks/deploy.md says what to do"
  fi
  ok "scripts/check-drift.sh of $short: no drift"
  changed=$(git diff --name-only "$head" "$target" -- "${AT_START[@]}" | tr '\n' ' ')
  [ -z "$changed" ] || echo "note   $UNIT runs these from this checkout at its next start: ${changed% }"
  if [ $apply = 0 ]; then echo "dry run: scripts/deploy.sh --apply deploys $short"; exit 0; fi

  # the checks took a while: the checkout and the service must still be as they were
  checkout_clean
  [ "$(git rev-parse HEAD)" = "$head" ] || refuse "this checkout moved while the checks ran"
  state=$(service_idle) || refuse "$UNIT is $state; deploy when it is not starting or stopping"
  git merge -q --ff-only --no-overwrite-ignore "$target" || refuse "git merge --ff-only $short failed"
  echo "deployed $short: $(git log -1 --format=%s | printable)"
  [ -z "$changed" ] || echo "to run them now: sudo systemctl restart strata-q3 (ready after about 7-9 minutes)"
  if ! check_of_target scripts/check-drift.sh "no drift" "$tmp/drift.log"; then
    sed 's/^/  /' "$tmp/drift.log" | printable
    echo "deployed, but scripts/check-drift.sh now finds drift: $why" >&2
    exit 1
  fi
}
main "$@"; exit
