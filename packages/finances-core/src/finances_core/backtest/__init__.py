"""Núcleo compartido de backtesting (specs/finances_backtester, ADR-B01).

Contratos y lógica comunes a todos los motores ``ft_*`` de ``finances-backtester``:
contrato y registro de estrategias (:mod:`.strategies`, :mod:`.catalog`), lectura de la
estrategia versionada (:mod:`.strategy_source`), contrato de motor (:mod:`.engine_api`),
orquestación común (:mod:`.runner`) y errores tipados (:mod:`.errors`). Los motores solo
simulan; las señales se calculan aquí con Polars ``Float32`` (ADR-B09).
"""
