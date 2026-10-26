"""Router FastAPI de ft_datasets (adaptador delgado, Art. 1; rutas en plan §2.2).

Se exporta ``router`` (prefijo ``/datasets``); lo monta el paquete ``finances-api``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/datasets", tags=["datasets"])
