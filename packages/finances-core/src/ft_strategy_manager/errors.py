"""Errores de negocio de ``ft_strategy_manager`` (mapeados 1:1 a EARS, Art. 6).

CLI y API traducen estas excepciones: ver ``cli.EXIT_*`` y ``api`` (404/409/422/400).
"""

from __future__ import annotations


class StrategyError(Exception):
    """Base de los errores de negocio."""


class StrategyNotFoundError(StrategyError):
    """RF-2/RF-3: ``strategy_id`` inexistente (API 404, CLI exit 1)."""

    def __init__(self, strategy_id: str) -> None:
        super().__init__("Strategy not found")
        self.strategy_id = strategy_id


class VersionNotFoundError(StrategyError):
    """RF-2/RF-6: la versión pedida no existe (API 404, CLI exit 1)."""

    def __init__(self, strategy_id: str, version: int) -> None:
        super().__init__("Version not found")
        self.strategy_id = strategy_id
        self.version = version


class StrategyConflictError(StrategyError):
    """RF-1/RF-8: ``name`` duplicado (API 409, CLI exit 3)."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"Strategy name already exists: {name}. Choose a different name "
            "(a soft-deleted strategy keeps its name until it is purged)."
        )
        self.name = name


class StrategyValidationError(StrategyError):
    """RF-1/RF-3/RF-7: parámetros, schema o patch inválidos (API 422, CLI exit 2)."""


class NoChangesError(StrategyError):
    """RF-3 (plan §3.2): la nueva versión tendría el mismo hash (API 400, CLI exit 2)."""

    def __init__(self) -> None:
        super().__init__("No changes detected")
