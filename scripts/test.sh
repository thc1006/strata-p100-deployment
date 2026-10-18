#!/usr/bin/env bash
# The repo's own checks, without starting or stopping anything: unit tests, shell and Python syntax, the systemd
# units' syntax, and that every program the units run exists.  Run it before every commit.  Some of those programs
# are outside the repo (~/dev/strata, the installed watchdog) and exist only on this machine, so elsewhere the last two
# checks fail; --repo-only skips those programs, and CI runs that (.github/workflows/test.yml).
# (The machine's state is checked by check-drift.sh and check-deployment.sh, not here.)
# Usage: scripts/test.sh [--repo-only]
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
cd "$D" || exit 1
case "$*" in
  '') REPO_ONLY=0 ;;
  --repo-only) REPO_ONLY=1 ;;
  *) echo "usage: scripts/test.sh [--repo-only]" >&2; exit 2 ;;
esac
fail=0
step() { printf '\n== %s\n' "$1"; }

step "unit tests (tests/)"
python3 -B -m unittest discover -s tests -q || fail=1

step "shell syntax"
sh_files=(scripts/*.sh bench/*.sh)
for f in "${sh_files[@]}"; do bash -n "$f" || { echo "syntax error: $f"; fail=1; }; done
echo "checked ${#sh_files[@]} files"

step "python syntax"
python3 - <<'EOF' || fail=1
import ast, pathlib, sys
files = [p for p in sorted(pathlib.Path(".").glob("*/**/*.py")) if p.parts[0] in ("bench", "tools", "tests")]
bad = 0
for p in files:
    try:
        ast.parse(p.read_text(), str(p))
    except SyntaxError as e:
        print(f"syntax error: {e}"); bad = 1
print(f"checked {len(files)} files")
sys.exit(bad)
EOF

step "systemd units (systemd-analyze verify; lines about other units on this machine are ignored)"
units=(systemd/*.service systemd/*.timer)
dir=$D/systemd
if [ $REPO_ONLY = 1 ]; then
  # copies of the units in which each program that does not exist here is /bin/true, so that verify checks the rest
  # as on this machine (the next check covers the programs in the repo)
  dir=$(mktemp -d) || exit 1
  trap 'rm -rf -- "$dir"' EXIT
  exec_re='^(Exec[A-Za-z]*[[:space:]]*=[[:space:]]*[-@+!:]*)(/[^[:space:]]*)(.*)$'
  for f in "${units[@]}"; do
    while IFS= read -r line || [ -n "$line" ]; do
      [[ $line =~ $exec_re ]] && [ ! -x "${BASH_REMATCH[2]}" ] && line=${BASH_REMATCH[1]}/bin/true${BASH_REMATCH[3]}
      printf '%s\n' "$line"
    done < "$f" > "$dir/${f#systemd/}"
  done
fi
# keep the lines about these units: some start with the file path, some with the unit name ("x.service: Command …")
pat=(-e "$dir/"); for f in "${units[@]}"; do pat+=(-e "${f#systemd/}: "); done
raw=$(systemd-analyze verify "${units[@]/#systemd/$dir}" 2>&1); rc=$?
out=$(grep -F "${pat[@]}" <<< "$raw")
if [ -n "$out" ]; then echo "$out"; fail=1
elif [ $rc != 0 ]; then echo "systemd-analyze verify exited with $rc:"; tail -n 5 <<< "$raw"; fail=1
else echo "no findings for systemd/*"; fi

step "programs the units run (verify checks only the first command of each Exec type and skips '-' ones)"
# a path inside this repo (…/p100-q3-deployment/…) must exist here and be executable; any other path must exist on
# this machine (not checked with --repo-only)
n=0; skipped=0
while IFS= read -r line; do
  n=$((n + 1))
  unit=${line%%:*}; cmd=${line#*=}; cmd=${cmd#"${cmd%%[![:space:]]*}"}; prog=${cmd%% *}; prog=${prog#[-@+!:]}
  case $prog in
    */p100-q3-deployment/*) f="$D/${prog#*/p100-q3-deployment/}"; [ -x "$f" ] || { echo "$unit: $prog -> $f is missing or not executable"; fail=1; } ;;
    /*) if [ $REPO_ONLY = 1 ]; then skipped=$((skipped + 1))
        else [ -x "$prog" ] || { echo "$unit: $prog does not exist on this machine"; fail=1; }; fi ;;
  esac
done < <(grep -H -E '^Exec[A-Za-z]*[[:space:]]*=' systemd/*.service)
if [ $REPO_ONLY = 1 ]; then echo "checked $((n - skipped)) of $n Exec lines (--repo-only: $skipped outside the repo)"
else echo "checked $n Exec lines"; fi

echo
if [ $fail = 0 ]; then echo "all checks passed"; else echo "FAILED"; fi
exit $fail
