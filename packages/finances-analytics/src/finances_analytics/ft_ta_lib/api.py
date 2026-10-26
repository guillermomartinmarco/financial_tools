"""Router FastAPI ``/ta-lib`` de ``ft_ta_lib`` (plan §2.2); lo monta ``finances-api``.

Esqueleto (T0.2): las rutas de plan §2.2 (``/catalog``, ``/compute``, ``/status``,
``/plan``, ``/result``, ``/datasets``) llegan en la Fase 4 (T4.4) con nombres únicos por
herramienta (``ta_lib_*``).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/ta-lib", tags=["ft_ta_lib"])

__all__ = ["router"]
