"""Errores de dominio de ``ft_ta_lib`` (RF-13).

Los adaptadores los traducen: :class:`TaLibValidationError` → CLI exit 2 / HTTP 400;
:class:`OutputOutOfRangeError` es un error por item (``output_out_of_range``, la tarea sigue);
:class:`TaLibExecutionError` es una excepción de TA-Lib durante el cálculo (error por item);
:class:`EmptyExportError` → CLI exit 3 / HTTP 400.
"""

from __future__ import annotations


class TaLibValidationError(ValueError):
    """Petición inválida (RF-4, RF-5, RF-13): función o grupo desconocido, función no
    soportada, parámetro inexistente/de tipo erróneo/``TA_BAD_PARAM`` o entrada inválida.

    Atributos: ``function``, ``param``, ``value`` y ``valid`` (valores o grupos válidos).
    """

    def __init__(
        self,
        message: str,
        *,
        function: str | None = None,
        param: str | None = None,
        value: object = None,
        valid: str = "",
    ) -> None:
        super().__init__(message)
        self.function = function
        self.param = param
        self.value = value
        self.valid = valid


class OutputOutOfRangeError(ValueError):
    """Una salida entera no cabe en ``Int16`` (RF-7, Art. 5.1 bis): nunca se trunca."""

    code = "output_out_of_range"

    def __init__(self, series_id: str, column: str, low: int, high: int) -> None:
        super().__init__(
            f"{self.code}: {series_id}.{column} con valores en [{low}, {high}] fuera de Int16"
        )
        self.series_id = series_id
        self.column = column
        self.low = low
        self.high = high


class TaLibExecutionError(RuntimeError):
    """TA-Lib lanzó una excepción al calcular una función (error por item, RF-11)."""

    code = "talib_error"


class EmptyExportError(Exception):
    """El rango pedido no tiene filas que exportar (RF-10: CLI exit 3 / HTTP 400)."""


__all__ = [
    "EmptyExportError",
    "OutputOutOfRangeError",
    "TaLibExecutionError",
    "TaLibValidationError",
]
