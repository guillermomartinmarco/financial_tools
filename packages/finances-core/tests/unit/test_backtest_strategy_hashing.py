"""B1.3: ``strategy_code_hash`` (RF-19, plan §3.6) con vectores fijos."""

from __future__ import annotations

import hashlib
from pathlib import Path

from finances_core.backtest.strategies.hashing import code_hash, iter_python_files

LOGIC = "x = 1\n"
HELPER = "def f() -> int:\n    return 2\n"
#: SHA-256 literal de {helpers.py: HELPER, logic.py: LOGIC} (LF), fijado una vez.
EXPECTED = "2b503caadf42a492acaf6314f2f981afbf861070c498b2bcc7e16ba004c38fa4"


def _folder(root: Path, newline: str = "\n") -> Path:
    folder = root / "momentum"
    folder.mkdir(parents=True)
    for name, text in (("logic.py", LOGIC), ("helpers.py", HELPER)):
        (folder / name).write_bytes(text.replace("\n", newline).encode("utf-8"))
    (folder / "README.md").write_text("# doc\n", encoding="utf-8")
    return folder


def _reference(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0" + files[rel].encode("utf-8") + b"\0")
    return digest.hexdigest()


def test_code_hash_matches_fixed_vector(tmp_path: Path) -> None:
    value = code_hash(_folder(tmp_path))
    assert value == _reference({"logic.py": LOGIC, "helpers.py": HELPER})
    assert value == EXPECTED


def test_code_hash_is_independent_of_line_endings(tmp_path: Path) -> None:
    lf = code_hash(_folder(tmp_path / "lf"))
    crlf = code_hash(_folder(tmp_path / "crlf", newline="\r\n"))
    assert lf == crlf == EXPECTED


def test_editing_readme_does_not_change_hash(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = code_hash(folder)
    (folder / "README.md").write_text("# otro\n\ntexto distinto\n", encoding="utf-8")
    assert code_hash(folder) == before


def test_editing_a_python_file_changes_hash(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = code_hash(folder)
    (folder / "logic.py").write_text("x = 2\n", encoding="utf-8")
    assert code_hash(folder) != before


def test_adding_a_python_file_changes_hash(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = code_hash(folder)
    (folder / "extra.py").write_text("", encoding="utf-8")
    assert code_hash(folder) != before


def test_nested_files_use_posix_paths_and_pycache_is_excluded(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = code_hash(folder)
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "junk.py").write_text("y = 1\n", encoding="utf-8")
    assert code_hash(folder) == before
    (folder / "sub").mkdir()
    (folder / "sub" / "mod.py").write_text("z = 3\n", encoding="utf-8")
    expected = _reference({"logic.py": LOGIC, "helpers.py": HELPER, "sub/mod.py": "z = 3\n"})
    assert code_hash(folder) == expected
    assert [rel for rel, _ in iter_python_files(folder)] == [
        "helpers.py",
        "logic.py",
        "sub/mod.py",
    ]


def test_only_lowercase_py_suffix_is_hashed_on_every_os(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    before = code_hash(folder)
    (folder / "Helper.PY").write_text("x = 1\n", encoding="utf-8")
    assert "Helper.PY" not in [relative for relative, _ in iter_python_files(folder)]
    assert code_hash(folder) == before
