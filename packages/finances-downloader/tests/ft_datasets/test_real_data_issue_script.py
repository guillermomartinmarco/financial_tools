"""T5.7 — ``scripts/ci/real_data_issue.sh`` (RF-10 bis) con un ``gh`` falso (sin GitHub).

Comprueba la clasificación por tipo, la de-duplicación (comentar si hay un issue abierto
con ``real-data`` + ``real-data:<tipo>``, abrir uno si no), el cuerpo (informe de verify y
resumen de tests) y que un fallo de ``gh`` no cambia el resultado (exit 0 + ``::warning::``).
También fija en ``real-data.yml`` que solo la ejecución programada gestiona issues.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "ci" / "real_data_issue.sh"
WORKFLOW = REPO / ".github" / "workflows" / "real-data.yml"

FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_LOG"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [ "${args[$i]}" = "--body-file" ]; then cp "${args[$((i + 1))]}" "$FAKE_GH_BODY"; fi
done
if [ "$1 $2" = "issue list" ]; then printf '%s' "${FAKE_GH_OPEN:-}"; fi
exit "${FAKE_GH_EXIT:-0}"
"""
VERIFY_MD = (
    "### `ft_datasets verify benchmark-v1`: **MISMATCH**\n\n"
    "| `yfinance/equity/AAPL/1d@2015-01-01..2025-01-01` | MISMATCH | 2516/2516 "
    "| SCALE_RESTATED | 0.25 | open, high, low, close | 2020-08 |\n"
)


def _bash() -> str | None:
    """``bash`` POSIX; en Windows, el de Git (nunca el ``bash.exe`` de WSL en System32)."""
    found = shutil.which("bash")
    if sys.platform != "win32":
        return found
    if found and "system32" not in found.lower():
        return found
    git = shutil.which("git")
    if git is None:
        return None
    for root in Path(git).resolve().parents[:3]:
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


BASH = _bash()


@dataclass
class Run:
    returncode: int
    stdout: str
    calls: list[str]
    body: str


RunScript = Callable[..., Run]


@pytest.fixture
def run_script(tmp_path: Path) -> RunScript:
    if BASH is None:
        pytest.fail("bash no disponible para probar scripts/ci/real_data_issue.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_bytes(FAKE_GH.encode("utf-8"))
    gh.chmod(0o755)
    reports = tmp_path / "reports"
    reports.mkdir()

    def run(
        exit_code: str, tests: str, *, open_issue: str = "", gh_exit: int = 0, verify: bool = True
    ) -> Run:
        log, body = tmp_path / "gh.log", tmp_path / "body.md"
        for path in (log, body):
            path.unlink(missing_ok=True)
        (reports / "verify.md").unlink(missing_ok=True)
        if verify:
            (reports / "verify.md").write_text(VERIFY_MD, encoding="utf-8")
        (reports / "pytest.txt").write_text(
            "tests/x.py ..F\nFAILED tests/x.py::test_a - assert 1 == 2\n"
            "========= 1 failed, 2 passed in 1.0s =========\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_BODY": str(body),
            "FAKE_GH_OPEN": open_issue,
            "FAKE_GH_EXIT": str(gh_exit),
            "REAL_DATA_REPORTS_DIR": str(reports),
            "GITHUB_SERVER_URL": "https://github.example",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "123",
        }
        proc = subprocess.run(
            [str(BASH), str(SCRIPT), "benchmark-v1", exit_code, tests],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
            check=False,
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        text = body.read_text(encoding="utf-8") if body.exists() else ""
        return Run(proc.returncode, proc.stdout, calls, text)

    return run


@pytest.mark.parametrize(
    ("exit_code", "tests", "kind"),
    [
        ("3", "skipped", "download"),
        ("5", "skipped", "drift"),
        ("6", "skipped", "unlocked"),
        ("0", "failure", "tests"),
        ("0", "success", "infra"),
        ("", "", "infra"),
    ],
)
def test_opens_issue_with_type_labels_when_none_open(
    run_script: RunScript, exit_code: str, tests: str, kind: str
) -> None:
    run = run_script(exit_code, tests)
    assert run.returncode == 0
    assert run.calls[0].startswith("label create real-data ")
    assert run.calls[1].startswith(f"label create real-data:{kind} ")
    assert run.calls[2].startswith(
        f"issue list --state open --label real-data --label real-data:{kind} "
    )
    create = run.calls[3]
    assert create.startswith(f"issue create --title [real-data] {kind}: benchmark-v1 ")
    assert f"--label real-data --label real-data:{kind}" in create
    assert len(run.calls) == 4
    assert "https://github.example/owner/repo/actions/runs/123" in run.body
    assert "SCALE_RESTATED" in run.body
    assert ("FAILED tests/x.py::test_a" in run.body) is (kind == "tests")


def test_comments_open_issue_of_same_type(run_script: RunScript) -> None:
    run = run_script("5", "skipped", open_issue="42")
    assert run.returncode == 0
    assert run.calls[-1].startswith("issue comment 42 --body-file ")
    assert not any(c.startswith("issue create") for c in run.calls)
    assert "MISMATCH" in run.body


def test_missing_verify_report_is_stated(run_script: RunScript) -> None:
    run = run_script("3", "skipped", verify=False)
    assert "verify.md` no existe" in run.body


def test_gh_failure_does_not_change_job_result(run_script: RunScript) -> None:
    run = run_script("5", "skipped", gh_exit=1)
    assert run.returncode == 0
    assert "::warning::" in run.stdout
    assert len(run.calls) == 1  # se detiene en el primer fallo de gh


def test_workflow_only_scheduled_runs_manage_issues() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "runs-on: ubuntu-24.04" in text
    assert "ubuntu-latest" not in text
    assert "if: failure() && github.event_name == 'schedule'" in text
    assert "contents: read" in text
    assert "issues: write" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "scripts/ci/real_data_issue.sh" in text
    assert "secrets." not in text
