"""Errores tipados del núcleo de backtesting (RF-14, mapeados 1:1 a EARS, Art. 6).

Cada error lleva un ``code`` estable y el código de salida CLI / estado HTTP de la tabla
de RF-14; los adaptadores de cada motor los traducen sin inspeccionar mensajes. Los
mensajes son legibles y nunca incluyen trazas (RF-18).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class BacktestError(Exception):
    """Base de los errores de negocio del backtesting."""

    code = "backtest_error"
    exit_code = 3
    http_status = 500


class UnknownKindError(BacktestError, ValueError):
    """RF-1: ``kind`` fuera del registro de estrategias válidas (exit 2 / HTTP 400)."""

    code = "unknown_kind"
    exit_code = 2
    http_status = 400

    def __init__(self, kind: str, valid_kinds: Sequence[str]) -> None:
        listed = ", ".join(valid_kinds) or "(ninguno)"
        super().__init__(f"unknown_kind: {kind!r} no está registrado; válidos: {listed}")
        self.kind = kind
        self.valid_kinds = list(valid_kinds)


class StrategyInitError(BacktestError):
    """RF-1/RF-18: la carpeta de la estrategia falló al inicializarse.

    Local: exit 2 / HTTP 500 al usarla. Integrada: defecto del paquete y la construcción
    del registro falla (exit 3).
    """

    code = "strategy_init_error"
    http_status = 500

    def __init__(self, kind: str, origin: str, reason: str) -> None:
        super().__init__(f"strategy_init_error: {origin} {kind!r}: {reason}")
        self.kind = kind
        self.origin = origin
        self.reason = reason

    @property
    def exit_code(self) -> int:  # type: ignore[override]
        return 3 if self.origin == "builtin" else 2


class InvalidParamsError(BacktestError, ValueError):
    """RF-1: ``parameters``/``params`` incumplen el contrato (exit 2 / HTTP 400).

    ``errors`` contiene el detalle por campo (``ruta -> mensaje``).
    """

    code = "invalid_params"
    exit_code = 2
    http_status = 400

    def __init__(self, kind: str | None, errors: Mapping[str, str]) -> None:
        detail = "; ".join(f"{field}: {message}" for field, message in errors.items())
        target = f" de {kind!r}" if kind else ""
        super().__init__(f"invalid_params{target}: {detail}")
        self.kind = kind
        self.errors = dict(errors)


class InvalidSignalsError(BacktestError, ValueError):
    """RF-16: señales con dtype, longitud o nulos incorrectos (tarea ``FAILED``, exit 3)."""

    code = "invalid_signals"


class StrategyNotFoundError(BacktestError, LookupError):
    """RF-3: la estrategia no existe o está *soft-deleted* (exit 1 / HTTP 404)."""

    code = "strategy_not_found"
    exit_code = 1
    http_status = 404

    def __init__(self, strategy_id: str, message: str = "Strategy not found") -> None:
        super().__init__(f"{message}: {strategy_id}")
        self.strategy_id = strategy_id


class StrategyVersionNotFoundError(StrategyNotFoundError):
    """RF-3: la versión pedida no existe (exit 1 / HTTP 404)."""

    code = "version_not_found"

    def __init__(self, strategy_id: str, version: int) -> None:
        super().__init__(strategy_id, f"Version {version} not found")
        self.version = version
