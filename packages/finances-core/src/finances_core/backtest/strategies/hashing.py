"""Hash del código de una estrategia (``strategy_code_hash``, RF-19, plan §3.6, ADR-B12).

SHA-256 de la concatenación, ordenada por ruta POSIX relativa, de
``ruta + b"\\0" + contenido + b"\\0"`` para cada ``*.py`` de la carpeta (recursivo), con
los finales de línea CRLF normalizados a LF. ``README.md`` y ``__pycache__`` no entran:
editar la documentación no cambia el ``run_id``; editar o añadir un ``*.py`` sí. Las
ayudas compartidas ``strategies/_lib`` se versionan con ``CATALOG_VERSION`` (A22).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_EXCLUDED_DIRS = frozenset({"__pycache__"})


def iter_python_files(folder: Path) -> list[tuple[str, Path]]:
    """``(ruta POSIX relativa, ruta)`` de cada ``*.py`` de ``folder``, en orden de ruta.

    Se excluye todo archivo bajo un directorio ``__pycache__``. El orden es el de los
    puntos de código de la ruta relativa (igual al orden de bytes UTF-8), independiente
    del sistema operativo y del orden del sistema de archivos.
    """
    found: list[tuple[str, Path]] = []
    for path in folder.rglob("*.py"):
        relative = path.relative_to(folder)
        # rglob no distingue mayúsculas en Windows: se exige el sufijo exacto en todo SO.
        if (
            path.suffix != ".py"
            or not path.is_file()
            or _EXCLUDED_DIRS.intersection(relative.parts[:-1])
        ):
            continue
        found.append((relative.as_posix(), path))
    found.sort(key=lambda item: item[0])
    return found


def code_hash(folder: Path) -> str:
    """``strategy_code_hash`` (hex) de la carpeta de una estrategia (RF-19)."""
    digest = hashlib.sha256()
    for relative, path in iter_python_files(folder):
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()
