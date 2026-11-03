"""Lectura de la estrategia versionada para el backtesting (RF-3, ADR-B02, A2).

Envoltorio sobre ``ft_strategy_manager.service.get_strategy``: es el único módulo de
``finances_core.backtest`` que importa ``ft_strategy_manager`` (ambos viven en
``finances-core``), de modo que los motores no importan ninguna raíz ``ft_*`` (Art. 4).

- Versión por defecto: la última (``ft_strategy_manager`` RF-2).
- Estrategia *soft-deleted*: se trata como inexistente (A18, Deriva 9).
- ``parameters`` se valida con el registro (RF-1); ``kind_version``, ``code_hash`` y
  ``origin`` provienen del registro (RF-19).
"""

from __future__ import annotations

from pathlib import Path

from finances_core.backtest.catalog import StrategyRegistry, get_registry, parse_strategy_parameters
from finances_core.backtest.errors import StrategyNotFoundError, StrategyVersionNotFoundError
from finances_core.config import FinancesSettings
from finances_core.schemas.backtest import ResolvedStrategy
from ft_strategy_manager import errors as manager_errors
from ft_strategy_manager import service


def load(
    strategy_id: str,
    version: int | None = None,
    *,
    registry: StrategyRegistry | None = None,
    db_path: Path | None = None,
) -> ResolvedStrategy:
    """Resuelve ``strategy_id``/``version`` a una :class:`ResolvedStrategy` validada.

    Args:
        strategy_id: UUID v7 de ``ft_strategy_manager``.
        version: versión pedida; ``None`` = la última.
        registry: registro de estrategias; por defecto, el del proceso (:func:`get_registry`).
        db_path: base concreta (tests); por defecto la de ``ft_strategy_manager``.

    Raises:
        StrategyNotFoundError: SI la estrategia no existe o está *soft-deleted* (404).
        StrategyVersionNotFoundError: SI la versión no existe (404).
        UnknownKindError / InvalidParamsError: SI ``parameters`` no valida (400).
        StrategyInitError: SI ``kind`` es una carpeta local inválida (500).
    """
    try:
        strategy = service.get_strategy(strategy_id, version, db_path=db_path)
    except manager_errors.VersionNotFoundError as exc:
        raise StrategyVersionNotFoundError(exc.strategy_id, exc.version) from None
    except manager_errors.StrategyNotFoundError as exc:
        raise StrategyNotFoundError(exc.strategy_id) from None
    selected = strategy.selected
    if strategy.deleted_at_ms is not None or selected is None:
        raise StrategyNotFoundError(strategy_id)
    active = registry if registry is not None else get_registry(FinancesSettings())
    strategy_cls, params = parse_strategy_parameters(selected.parameters, active)
    kind = strategy_cls.kind
    return ResolvedStrategy(
        strategy_id=strategy.id,
        name=strategy.name,
        version=selected.version,
        hash=selected.hash,
        kind=kind,
        params=params.model_dump(mode="json"),
        kind_version=strategy_cls.version,
        code_hash=active.code_hashes[kind],
        origin=active.origins[kind],
    )
