"""Errores de dominio de ``ft_indicators`` (RF-12).

Los adaptadores los traducen: :class:`IndicatorValidationError` → CLI exit 2 / HTTP 400;
:class:`EmptyExportError` → CLI exit 3 / HTTP 400.
"""

from __future__ import annotations


class IndicatorValidationError(ValueError):
    """Petición inválida: indicador desconocido o parámetro inexistente/fuera de rango (RF-5).

    Atributos: ``indicator``, ``param``, ``value`` y ``valid`` (rango o valores válidos).
    """

    def __init__(
        self,
        message: str,
        *,
        indicator: str | None = None,
        param: str | None = None,
        value: object = None,
        valid: str = "",
    ) -> None:
        super().__init__(message)
        self.indicator = indicator
        self.param = param
        self.value = value
        self.valid = valid


class EmptyExportError(Exception):
    """El rango pedido no tiene filas que exportar (RF-9: CLI exit 3 / HTTP 400)."""


__all__ = ["EmptyExportError", "IndicatorValidationError"]
