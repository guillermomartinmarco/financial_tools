"""``ft_vectorbt``: motor de backtesting sobre VectorBT (``specs/ft_vectorbt/``).

``ENGINE_NAME`` identifica al motor en la identidad de corrida y en el almacén de
resultados (``finances_core.backtest``); ``__version__`` es la del paquete.
"""

from finances_backtester import __version__

ENGINE_NAME = "vectorbt"

__all__ = ["ENGINE_NAME", "__version__"]
