"""Router FastAPI ``/backtest/vectorbt`` de ``ft_vectorbt`` (RF-7; plan §2.2).

Lo monta ``finances-api`` (``ft_serve``). Las rutas se añaden en la Fase 4; sus nombres
llevan el prefijo ``vectorbt_*`` para que ``url_for`` resuelva sin colisiones.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/backtest/vectorbt", tags=["ft_vectorbt"])
