#!/usr/bin/env bash
# Local Linux check that mirrors .github/workflows/ci.yml (ruff, then mypy --strict and pytest
# per package) inside WSL, so Linux-only failures show up before pushing. Optional: GitHub CI
# remains the gate. Benchmarks are not run (their thresholds are calibrated on ubuntu-24.04).
#
# From Windows (Git Bash or PowerShell), at the repository root:
#   wsl bash scripts/wsl_ci.sh                          # full check, every package
#   wsl bash scripts/wsl_ci.sh --package finances-core  # one package (repeatable)
#   wsl bash scripts/wsl_ci.sh --package finances-core --stress 40 -- \
#       tests/unit/test_backtest_results_store.py -k concurrent
#
# Options:
#   --package NAME  limit mypy/pytest (and --stress) to NAME; repeatable. Default: all.
#   --stress N      run pytest N times with the arguments after "--" and report the failures
#                   (needs exactly one --package); skips ruff and mypy.
#   --reinstall     recreate the editable installs even if no pyproject.toml changed.
#
# The repository is copied with rsync to $FT_WSL_WORKDIR (default ~/financial_tools-wsl):
# reading /mnt/c directly is slow. The Linux venv lives in $FT_WSL_VENV (default
# ~/.venvs/financial_tools), never in /tmp, which WSL wipes on restart. The Windows .venv
# cannot be reused: its wheels (numba, vectorbt, ta-lib) are built for another platform.
set -euo pipefail

PACKAGES_ALL=(finances-core finances-downloader finances-analytics finances-backtester finances-api)
INSTALL_SPECS=(
  "packages/finances-core[dev]"
  "packages/finances-downloader[dev]"
  "packages/finances-analytics[dev]"
  "packages/finances-backtester[dev]"
  "packages/finances-api[service,dev]"
)
PYTHON_BIN="${FT_WSL_PYTHON:-python3.12}"
WORK="${FT_WSL_WORKDIR:-$HOME/financial_tools-wsl}"
VENV="${FT_WSL_VENV:-$HOME/.venvs/financial_tools}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

packages=()
stress=0
reinstall=0
pytest_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --package) packages+=("${2:?--package needs a name}"); shift 2 ;;
    --stress) stress="${2:?--stress needs a count}"; shift 2 ;;
    --reinstall) reinstall=1; shift ;;
    --) shift; pytest_args=("$@"); break ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "wsl_ci: unknown option: $1" >&2; exit 2 ;;
  esac
done
[ ${#packages[@]} -eq 0 ] && packages=("${PACKAGES_ALL[@]}")
for pkg in "${packages[@]}"; do
  if [[ ! " ${PACKAGES_ALL[*]} " == *" $pkg "* ]]; then
    echo "wsl_ci: unknown package: $pkg" >&2
    exit 2
  fi
done
if ! [[ "$stress" =~ ^[0-9]+$ ]]; then
  echo "wsl_ci: --stress needs a non-negative integer" >&2
  exit 2
fi
if [ "$stress" -gt 0 ] && [ ${#packages[@]} -ne 1 ]; then
  echo "wsl_ci: --stress needs exactly one --package" >&2
  exit 2
fi

echo "==> sync $SRC -> $WORK"
mkdir -p "$WORK"
rsync -a --delete \
  --exclude .git/ --exclude .venv/ --exclude share/ --exclude logs/ --exclude docs/articles/ \
  --exclude __pycache__/ --exclude .mypy_cache/ --exclude .ruff_cache/ --exclude .pytest_cache/ \
  --exclude build/ --exclude '*.egg-info/' \
  "$SRC/" "$WORK/"
cd "$WORK"

stamp_file="$VENV/.ft_pyproject.sha256"
stamp="$(cat packages/*/pyproject.toml | sha256sum | cut -d' ' -f1)"
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> create venv $VENV ($PYTHON_BIN)"
  mkdir -p "$(dirname "$VENV")"
  "$PYTHON_BIN" -m venv "$VENV"
  reinstall=1
fi
if [ "$reinstall" -eq 1 ] || [ "$(cat "$stamp_file" 2>/dev/null)" != "$stamp" ]; then
  echo "==> install packages (editable, core first)"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  for spec in "${INSTALL_SPECS[@]}"; do
    "$VENV/bin/python" -m pip install -q -e "$spec"
  done
  echo "$stamp" > "$stamp_file"
fi
PY="$VENV/bin/python"

if [ "$stress" -gt 0 ]; then
  pkg="${packages[0]}"
  failures=0
  echo "==> stress $pkg x$stress: pytest ${pytest_args[*]}"
  for i in $(seq 1 "$stress"); do
    if ! (cd "packages/$pkg" && "$PY" -m pytest -q -p no:cacheprovider "${pytest_args[@]}" \
      > "$WORK/.stress-last.log" 2>&1); then
      failures=$((failures + 1))
      echo "  run $i: FAILED"
      grep -E "^(FAILED|ERROR)" "$WORK/.stress-last.log" | sed 's/^/    /' || true
    fi
  done
  echo "==> stress: $failures of $stress runs failed"
  [ "$failures" -eq 0 ]
  exit
fi

failed=()
echo "==> ruff"
"$PY" -m ruff check packages || failed+=("ruff check")
"$PY" -m ruff format --check packages || failed+=("ruff format")
for pkg in "${packages[@]}"; do
  echo "==> $pkg: mypy --strict src"
  (cd "packages/$pkg" && "$PY" -m mypy --strict src) || failed+=("$pkg mypy")
  echo "==> $pkg: pytest"
  (cd "packages/$pkg" && "$PY" -m pytest -q -p no:cacheprovider "${pytest_args[@]}") \
    || failed+=("$pkg pytest")
done

if [ ${#failed[@]} -gt 0 ]; then
  printf '==> FAILED: %s\n' "${failed[@]}"
  exit 1
fi
echo "==> all checks passed"
