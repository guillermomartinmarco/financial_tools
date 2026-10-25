# Project Engram — Financial Tools Suite

## Architectural Decisions
- **Core Technology:** Hybrid engine Python + C++ (PyBind11) + CUDA (`ft_backtestopt_spectre`).
- **Data Format:** Historical market data is stored and distributed strictly via Apache Parquet.
- **Strict Quality Bar:** Ruff for linting, MyPy in strict mode for typing, Pytest for regression tests.

## Active Session Memory
- [2026-10-25] Environment synchronized locally with Gentle-AI 4.1.1 on MSYS2 UCRT64 + OpenCode.
- Avoid rewriting C++ code unless memory allocations or parallel executions explicitly require optimization.
