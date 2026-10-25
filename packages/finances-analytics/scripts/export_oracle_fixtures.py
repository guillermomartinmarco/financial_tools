"""Regenera el paquete de *fixtures* del oráculo de ``ft_indicators`` (plan §3.10, T3.3).

Uso (desde la raíz del repositorio)::

    python packages/finances-analytics/scripts/export_oracle_fixtures.py [--version v1] [--out DIR]

Por defecto escribe en ``packages/finances-analytics/tests/ft_indicators/fixtures/oracle/``.
El commit generador (``git describe --always --dirty``) queda en ``manifest.json``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from finances_analytics.ft_indicators.oracle import export_oracle_pack

PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PACKAGE_DIR / "tests" / "ft_indicators" / "fixtures" / "oracle"


def _commit() -> str:
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        result = subprocess.run(
            [git, "describe", "--always", "--dirty", "--abbrev=12"],
            cwd=PACKAGE_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada: genera el paquete y escribe su ruta en stdout."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default="v1", help="versión del paquete (subdirectorio)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directorio base")
    args = parser.parse_args(argv)
    root = export_oracle_pack(args.out, args.version, commit=_commit())
    sys.stdout.write(f"{root}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
