"""Router FastAPI de ``ft_indicators`` (plan §2.2); montado por ``finances_api.create_app()``.

Esqueleto de la Fase 0: las rutas se añaden en la Fase 5 (T5.4).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/indicators", tags=["ft_indicators"])
