"""Contrato de despacho asíncrono compartido por los routers de descarga (Art. 18)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DispatchResponse(BaseModel):
    """Contrato de despacho asíncrono (Art. 18)."""

    task_id: str
    status: Literal["ACCEPTED"] = "ACCEPTED"
    poll_url: str
    plan_url: str
    result_url: str
