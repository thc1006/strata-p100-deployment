#!/usr/bin/env bash
# The repo's own checks, without starting or stopping anything: unit tests, shell and Python syntax, the systemd
# units' syntax, and that every program the units run exists.  Run it before every commit.  The last check needs
# this machine (~/dev/strata and the installed watchdog), so on a fresh clone it fails until the rebuild is done.
# (The machine's state is checked by check-drift.sh and check-deployment.sh, not here.)
# Usage: scripts/test.sh
set -u -o pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
cd "$D" || exit 1
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
# keep the lines about these units: some start with the file path, some with the unit name ("x.service: Command …")
pat=(-e "$D/systemd/"); for f in systemd/*.service systemd/*.timer; do pat+=(-e "${f#systemd/}: "); done
out=$(systemd-analyze verify systemd/*.service systemd/*.timer 2>&1 | grep -F "${pat[@]}")
if [ -n "$out" ]; then echo "$out"; fail=1; else echo "no findings for systemd/*"; fi

step "programs the units run (verify checks only the first command of each Exec type and skips '-' ones)"
# a path inside this repo (…/p100-q3-deployment/…) must exist here and be executable; any other path must exist on
# this machine
while IFS= read -r line; do
  unit=${line%%:*}; cmd=${line#*=}; cmd=${cmd#"${cmd%%[![:space:]]*}"}; prog=${cmd%% *}; prog=${prog#[-@+!:]}
  case $prog in
    */p100-q3-deployment/*) f="$D/${prog#*/p100-q3-deployment/}"; [ -x "$f" ] || { echo "$unit: $prog -> $f is missing or not executable"; fail=1; } ;;
    /*) [ -x "$prog" ] || { echo "$unit: $prog does not exist on this machine"; fail=1; } ;;
  esac
done < <(grep -H -E '^Exec[A-Za-z]*[[:space:]]*=' systemd/*.service)
echo "checked $(grep -h -c -E '^Exec[A-Za-z]*[[:space:]]*=' systemd/*.service | paste -sd+ | bc) Exec lines"

echo
if [ $fail = 0 ]; then echo "all checks passed"; else echo "FAILED"; fi
exit $fail
