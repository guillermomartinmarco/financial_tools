"""Ayudas compartidas de las estrategias en Polars ``Float32`` (RF-2, plan §1.1, ADR-B12).

Cada ayuda recibe y devuelve una ``pl.Expr``: se componen dentro de ``candles.select``
sin materializar series intermedias. Solo usan datos de las barras ``<= t`` (RF-20).

Los cruces devuelven ``false`` (nunca nulo) mientras falta algún dato: el calentamiento
no genera señal (RF-2). El código de ``_lib`` no entra en el ``strategy_code_hash``
de cada estrategia; un cambio de su semántica exige subir ``CATALOG_VERSION`` (A22).

Importación (integradas y locales)::

    from finances_core.backtest.strategies._lib import cross_above, sma, position_from_events
"""

from __future__ import annotations

import polars as pl

Operand = pl.Expr | float


def _operands(b: Operand) -> tuple[pl.Expr, pl.Expr]:
    """``(b[t], b[t-1])``; un umbral constante vale lo mismo en ambas barras."""
    if isinstance(b, pl.Expr):
        return b, b.shift(1)
    threshold = pl.lit(b, dtype=pl.Float32)
    return threshold, threshold


def sma(x: pl.Expr, n: int) -> pl.Expr:
    """Media aritmética de ``x[t-n+1..t]``; nula para ``t < n-1``."""
    return x.rolling_mean(window_size=n)


def cross_above(a: pl.Expr, b: Operand) -> pl.Expr:
    """``a[t-1] <= b[t-1]`` **y** ``a[t] > b[t]`` (cruce estricto al alza, RF-2).

    Un empate en ``t`` no genera señal; un empate en ``t-1`` seguido de ``a > b`` sí.
    ``false`` donde falta ``a`` o ``b`` en ``t`` o ``t-1``.
    """
    now, prev = _operands(b)
    return ((a.shift(1) <= prev) & (a > now)).fill_null(value=False)


def cross_below(a: pl.Expr, b: Operand) -> pl.Expr:
    """``a[t-1] >= b[t-1]`` **y** ``a[t] < b[t]`` (cruce estricto a la baja, RF-2)."""
    now, prev = _operands(b)
    return ((a.shift(1) >= prev) & (a < now)).fill_null(value=False)


def _wilder_mean(
    values: pl.Expr,
    period: int,
    *,
    seed_at: int | None = None,
    seed_window: int | None = None,
) -> pl.Expr:
    """Media de Wilder: semilla SMA de ``values[1..period]`` en ``t = period`` y después
    ``media[t] = (media[t-1]·(period-1) + values[t]) / period``; nula para ``t < period``.

    ``ewm_mean(alpha=1/period, adjust=False)`` es exactamente esa recursión y arranca en
    el primer valor no nulo, que aquí es la semilla.

    ``seed_at``/``seed_window`` desplazan la semilla (por defecto ``period``/``period``,
    comportamiento histórico intacto para ``rsi_wilder``/``atr``). El segundo suavizado de
    ADX la necesita en ``t = 2n-2`` con ventana ``n-1`` (``DX`` recién válido desde ``n``).
    """
    seed_at = period if seed_at is None else seed_at
    seed_window = period if seed_window is None else seed_window
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    seeded = (
        pl.when(t < seed_at)
        .then(None)
        .when(t == seed_at)
        .then(values.rolling_mean(window_size=seed_window))
        .otherwise(values)
    )
    return seeded.ewm_mean(alpha=1 / period, adjust=False)


def rsi_wilder(close: pl.Expr, period: int) -> pl.Expr:
    """RSI de Wilder con semilla SMA (RF-2), en ``[0, 100]``; nulo para ``t < period``.

    ``Δ[t] = close[t] - close[t-1]``; ganancia ``max(Δ, 0)`` y pérdida ``max(-Δ, 0)``;
    ``RSI = 100 - 100 / (1 + RS)`` con ``RS = media_ganancia / media_pérdida``. SI la
    media de pérdidas es 0 y de ganancias > 0, ENTONCES 100; SI ambas son 0, 50.
    """
    delta = close - close.shift(1)
    gain = _wilder_mean(delta.clip(lower_bound=0.0), period)
    loss = _wilder_mean((-delta).clip(lower_bound=0.0), period)
    hundred = pl.lit(100.0, dtype=pl.Float32)
    rsi = (
        pl.when(loss == 0)
        .then(pl.when(gain > 0).then(hundred).otherwise(pl.lit(50.0, dtype=pl.Float32)))
        .otherwise(hundred - hundred / (1 + gain / loss))
    )
    return rsi.cast(pl.Float32)


def ema(x: pl.Expr, n: int) -> pl.Expr:
    """Media exponencial ``alpha = 2/(n+1)`` sembrada con ``SMA(n)`` en ``t = n-1`` (L1).

    ``ema[t] = alpha·x[t] + (1-alpha)·ema[t-1]`` para ``t >= n``; nula para ``t < n-1``.
    ``ewm_mean(adjust=False)`` es esa recursión y arranca en el primer valor no nulo (la
    semilla), así que es prefijo-estable.
    """
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    seeded = (
        pl.when(t < n - 1)
        .then(None)
        .when(t == n - 1)
        .then(x.rolling_mean(window_size=n))
        .otherwise(x)
    )
    return seeded.ewm_mean(alpha=2.0 / (n + 1.0), adjust=False).cast(pl.Float32)


def atr(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """Average True Range de Wilder (L1); nulo para ``t < n``.

    ``TR[t] = max(high-low, |high-close[t-1]|, |low-close[t-1]|)``; en ``t=0`` (sin
    ``close[t-1]``), ``TR[0] = high[0]-low[0]``. ``atr`` es la media de Wilder de ``TR``
    con periodo ``n`` (misma recursión sembrada que ``rsi_wilder``).
    """
    prev_close = close.shift(1)
    true_range = pl.max_horizontal(high - low, (high - prev_close).abs(), (low - prev_close).abs())
    return _wilder_mean(true_range, n).cast(pl.Float32)


def _true_range(high: pl.Expr, low: pl.Expr, close: pl.Expr) -> pl.Expr:
    """``TR[t] = max(high-low, |high-close[t-1]|, |low-close[t-1]|)`` (``TR[0] = high-low``)."""
    prev_close = close.shift(1)
    return pl.max_horizontal(high - low, (high - prev_close).abs(), (low - prev_close).abs())


def _plus_dm(high: pl.Expr, low: pl.Expr) -> pl.Expr:
    """``+DM``: movimiento direccional al alza de Wilder; ``0`` en ``t=0`` (sin barra previa).

    ``up = high-high[t-1]``, ``dn = low[t-1]-low``; ``+DM = up`` si ``up > dn`` y ``up > 0``,
    si no ``0`` (predicado nulo en ``t=0`` cuenta como ``false`` → ``0``).
    """
    up = high - high.shift(1)
    dn = low.shift(1) - low
    return pl.when((up > dn) & (up > 0.0)).then(up).otherwise(0.0)


def _minus_dm(high: pl.Expr, low: pl.Expr) -> pl.Expr:
    """``-DM``: movimiento direccional a la baja de Wilder; ``0`` en ``t=0`` (simétrico)."""
    up = high - high.shift(1)
    dn = low.shift(1) - low
    return pl.when((dn > up) & (dn > 0.0)).then(dn).otherwise(0.0)


def _wilder_di_smooth(values: pl.Expr, n: int) -> pl.Expr:
    """Suavizado direccional de TA-Lib en forma media ``M = S/n`` (semilla de ``n-1`` términos).

    TA-Lib siembra la suma de Wilder con ``S[n-1] = Σ values[1..n-1]`` (``n-1`` términos, excluye
    ``t=0``) y recurre ``S[t] = S[t-1] - S[t-1]/n + values[t]`` desde ``t=n``. En forma media
    ``M[t] = S[t]/n`` eso es exactamente ``ewm_mean(alpha=1/n)`` sembrado con ``Σ[1..n-1]/n`` en
    ``t = n-1``. ``+DI = 100·M(+DM)/M(TR)`` cancela el ``1/n``, así que la forma media basta y
    reproduce a TA-Lib (verificado). Nulo para ``t < n-1``.
    """
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    seed = values.rolling_sum(window_size=n - 1) / n
    seeded = pl.when(t < n - 1).then(None).when(t == n - 1).then(seed).otherwise(values)
    return seeded.ewm_mean(alpha=1 / n, adjust=False)


def plus_di(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """``+DI = 100·Wilder(+DM, n)/Wilder(TR, n)`` (Wilder, convención TA-Lib); nulo para ``t < n``.

    Float64 interno (el cociente sufre cancelación; excepción RFC-0007); salida ``Float32``.
    """
    h, low_, c = high.cast(pl.Float64), low.cast(pl.Float64), close.cast(pl.Float64)
    sm_dm = _wilder_di_smooth(_plus_dm(h, low_), n)
    sm_tr = _wilder_di_smooth(_true_range(h, low_, c), n)
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    di = pl.when((t >= n) & (sm_tr > 0)).then(100.0 * sm_dm / sm_tr).otherwise(None)
    return di.cast(pl.Float32)


def minus_di(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """``-DI = 100·Wilder(-DM, n)/Wilder(TR, n)`` (Wilder/TA-Lib); nulo para ``t < n``."""
    h, low_, c = high.cast(pl.Float64), low.cast(pl.Float64), close.cast(pl.Float64)
    sm_dm = _wilder_di_smooth(_minus_dm(h, low_), n)
    sm_tr = _wilder_di_smooth(_true_range(h, low_, c), n)
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    di = pl.when((t >= n) & (sm_tr > 0)).then(100.0 * sm_dm / sm_tr).otherwise(None)
    return di.cast(pl.Float32)


def adx(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """``ADX = Wilder(DX, n)`` (doble suavizado Wilder, convención TA-Lib); nulo para ``t < 2n-1``.

    ``DX = 100·|+DI - -DI|/(+DI + -DI)`` (válido desde ``t=n``). El segundo Wilder siembra con
    la media de ``DX[n..2n-2]`` (``n-1`` términos) en ``t = 2n-2`` y da el primer ADX en
    ``t = 2n-1``. Float64 interno (cociente/cancelación); salida ``Float32``.
    """
    h, low_, c = high.cast(pl.Float64), low.cast(pl.Float64), close.cast(pl.Float64)
    sm_tr = _wilder_di_smooth(_true_range(h, low_, c), n)
    sm_pdm = _wilder_di_smooth(_plus_dm(h, low_), n)
    sm_mdm = _wilder_di_smooth(_minus_dm(h, low_), n)
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    pdi = pl.when(sm_tr > 0).then(100.0 * sm_pdm / sm_tr).otherwise(0.0)
    mdi = pl.when(sm_tr > 0).then(100.0 * sm_mdm / sm_tr).otherwise(0.0)
    di_sum = pdi + mdi
    dx = pl.when((t >= n) & (di_sum > 0)).then(100.0 * (pdi - mdi).abs() / di_sum).otherwise(None)
    smoothed = _wilder_mean(dx, n, seed_at=2 * n - 2, seed_window=n - 1)
    return pl.when(t >= 2 * n - 1).then(smoothed).otherwise(None).cast(pl.Float32)


def stoch_k(high: pl.Expr, low: pl.Expr, close: pl.Expr, k: int) -> pl.Expr:
    """``%K`` rápido de Stochastic (convención TA-Lib ``STOCHF``); nulo para ``t < k-1``.

    ``100·(close - min(low, k)) / (max(high, k) - min(low, k))`` sobre la ventana de ``k``
    barras **incluida** la actual; nulo si el rango es ``0``. Cociente de magnitudes del
    mismo orden (sin cancelación), ``Float32``; solo mira ``<= t`` (prefijo-estable).
    """
    hh = high.rolling_max(window_size=k)
    ll = low.rolling_min(window_size=k)
    rng = hh - ll
    return pl.when(rng > 0).then(100.0 * (close - ll) / rng).otherwise(None).cast(pl.Float32)


def stoch_d(high: pl.Expr, low: pl.Expr, close: pl.Expr, k: int, d: int) -> pl.Expr:
    """``%D`` rápido ``= SMA(stoch_k, d)`` (``STOCHF`` ``fastd``); nulo para ``t < k+d-2``."""
    return sma(stoch_k(high, low, close, k), d)


def williams_r(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """Williams %R (``WILLR`` de TA-Lib) en ``[-100, 0]``; nulo para ``t < n-1``.

    ``-100·(max(high, n) - close) / (max(high, n) - min(low, n))`` sobre la ventana ``n``
    incluida la actual; nulo si el rango es ``0``. ``Float32`` (sin cancelación); prefijo-estable.
    """
    hh = high.rolling_max(window_size=n)
    ll = low.rolling_min(window_size=n)
    rng = hh - ll
    return pl.when(rng > 0).then(-100.0 * (hh - close) / rng).otherwise(None).cast(pl.Float32)


def _mean_abs_dev(window: pl.Series) -> float:
    """Desviación media **absoluta** de la ventana respecto de su media (para CCI)."""
    mean = window.mean()
    if not isinstance(mean, (int, float)):
        return 0.0
    mad = (window - mean).abs().mean()
    return float(mad) if isinstance(mad, (int, float)) else 0.0


def cci(high: pl.Expr, low: pl.Expr, close: pl.Expr, n: int) -> pl.Expr:
    """Commodity Channel Index (convención TA-Lib); nulo para ``t < n-1``.

    ``tp = (high+low+close)/3``; ``CCI = (tp - SMA(tp,n)) / (0.015 · MAD(tp,n))`` donde
    ``MAD`` es la desviación **media absoluta** de la ventana respecto de su media (no la
    desviación estándar; convención TA-Lib). Float64 interno (``tp - SMA`` sufre cancelación;
    excepción RFC-0007); salida ``Float32``; nulo si ``MAD == 0``. El ``MAD`` rodante se
    reduce por ventana (``rolling_map``) porque cada ventana se centra en **su propia** media.
    """
    tp = ((high + low + close) / 3.0).cast(pl.Float64)
    sma_tp = tp.rolling_mean(window_size=n)
    mad = tp.rolling_map(_mean_abs_dev, window_size=n)
    return pl.when(mad > 0).then((tp - sma_tp) / (0.015 * mad)).otherwise(None).cast(pl.Float32)


def mfi(high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr, n: int) -> pl.Expr:
    """Money Flow Index (``MFI`` de TA-Lib) en ``[0, 100]``; nulo para ``t < n``.

    ``tp = (high+low+close)/3``; *raw money flow* ``= tp·volume``; es **positivo** si ``tp`` sube
    vs ``tp[t-1]`` y **negativo** si baja (``0`` en empate). ``MFI = 100·Σpos / (Σpos + Σneg)``
    con sumas rodantes de ventana ``n``. El *money flow* arranca en ``t=1`` (necesita ``tp[t-1]``),
    así que la primera ventana completa es ``[1..n]`` y el primer MFI cae en ``t=n``. Float64
    interno (magnitudes grandes ``tp·volume``; excepción RFC-0007); salida ``Float32``.
    """
    tp = ((high + low + close) / 3.0).cast(pl.Float64)
    raw = tp * volume.cast(pl.Float64)
    prev = tp.shift(1)
    pos_mf = pl.when(tp > prev).then(raw).otherwise(0.0)
    neg_mf = pl.when(tp < prev).then(raw).otherwise(0.0)
    pos_sum = pos_mf.rolling_sum(window_size=n)
    neg_sum = neg_mf.rolling_sum(window_size=n)
    total = pos_sum + neg_sum
    t = pl.int_range(pl.len(), dtype=pl.Int64)
    ratio = pl.when((t >= n) & (total > 0)).then(100.0 * pos_sum / total).otherwise(None)
    return ratio.cast(pl.Float32)


def bbands_pctb(close: pl.Expr, n: int, k: float) -> pl.Expr:
    """%b de Bollinger ``= (close - lower) / (upper - lower)`` (convención TA-Lib); nulo t<n-1.

    ``mid = SMA(close,n)``, ``sd = rolling_std_stable(close,n)`` (desv. estándar **poblacional**,
    como ``BBANDS`` matype=0), ``upper/lower = mid ± k·sd``. ``%b`` puede salirse de ``[0,1]``.
    Nulo si el ancho es ``0``. ``Float32`` (la cancelación crítica ya vive en
    ``rolling_std_stable``).
    """
    mid = sma(close, n)
    sd = rolling_std_stable(close, n)
    lower = mid - k * sd
    width = 2.0 * k * sd
    return pl.when(width > 0).then((close - lower) / width).otherwise(None).cast(pl.Float32)


def bbands_bandwidth(close: pl.Expr, n: int, k: float) -> pl.Expr:
    """Ancho de Bollinger ``= (upper - lower) / mid = 2k·sd / SMA(close,n)``; nulo para ``t<n-1``.

    Mismas ``mid``/``sd`` que :func:`bbands_pctb`; nulo si ``mid == 0``. ``Float32``.
    """
    mid = sma(close, n)
    sd = rolling_std_stable(close, n)
    width = 2.0 * k * sd
    return pl.when(mid != 0).then(width / mid).otherwise(None).cast(pl.Float32)


def ichimoku(
    high: pl.Expr, low: pl.Expr, tenkan: int, kijun: int, senkou_b: int
) -> dict[str, pl.Expr]:
    """Ichimoku **causal** (sin ``chikou``, que mira el futuro; RF-20). Devuelve 4 ``pl.Expr``.

    * ``tenkan = (max(high,tenkan) + min(low,tenkan)) / 2`` (nulo t<tenkan-1).
    * ``kijun  = (max(high,kijun)  + min(low,kijun))  / 2`` (nulo t<kijun-1).
    * ``senkou_a = ((tenkan + kijun) / 2)`` desplazado ``.shift(kijun)``; ``senkou_b =
      (max(high,senkou_b)+min(low,senkou_b))/2`` desplazado ``.shift(kijun)``.

    El *forward shift* clásico (los senkou se dibujan ``kijun`` barras **adelante**) se modela
    hacia atrás: el "valor del cloud en ``t``" es el calculado hace ``kijun`` barras
    (``.shift(kijun)`` toma el valor de ``t-kijun``), nunca mira el futuro. Por eso difiere de
    ``ICHIMOKU`` de TA-Lib, que devuelve el span crudo en ``t`` (``nuestro[t] = TA-Lib[t-kijun]``).
    ``Float32`` (promedio de máx/mín, sin cancelación); prefijo-estable.
    """

    def _mid_channel(period: int) -> pl.Expr:
        return (high.rolling_max(window_size=period) + low.rolling_min(window_size=period)) / 2.0

    tenkan_line = _mid_channel(tenkan)
    kijun_line = _mid_channel(kijun)
    senkou_a = ((tenkan_line + kijun_line) / 2.0).shift(kijun)
    senkou_b_line = _mid_channel(senkou_b).shift(kijun)
    return {
        "tenkan": tenkan_line.cast(pl.Float32),
        "kijun": kijun_line.cast(pl.Float32),
        "senkou_a": senkou_a.cast(pl.Float32),
        "senkou_b": senkou_b_line.cast(pl.Float32),
    }


def rolling_std_stable(x: pl.Expr, n: int) -> pl.Expr:
    """Desviación estándar **poblacional** (``ddof=0``) de ``x[t-n+1..t]``, prefijo-estable.

    ``var = E[x²] - E[x]²`` con sumas rodantes en ``Float64`` (**excepción RFC-0007**: la
    identidad sufre cancelación catastrófica en ``Float32`` —171% de error a escala BTC—,
    mismo motivo que RFC-0006 en ``ft_indicators``), recortada a ``>= 0`` y con raíz
    casteada a ``Float32``; nula para ``t < n-1``. Se reconstruye con ``rolling_sum`` porque
    ``rolling_std``/``rolling_var`` de Polars **no** son prefijo-estables (bug Bollinger #50).
    """
    x64 = x.cast(pl.Float64)
    mean = x64.rolling_sum(window_size=n) / n
    mean_sq = (x64 * x64).rolling_sum(window_size=n) / n
    variance = (mean_sq - mean * mean).clip(lower_bound=0.0)
    return variance.sqrt().cast(pl.Float32)


def zscore(x: pl.Expr, n: int) -> pl.Expr:
    """``(x[t] - SMA(x,n)[t]) / rolling_std_stable(x,n)[t]`` (L1); nulo si ``std == 0``.

    Cálculo interno en ``Float64`` (media y desviación poblacionales; **excepción RFC-0007**,
    como ``rolling_std_stable``); salida ``Float32``; nulo para ``t < n-1``.
    """
    x64 = x.cast(pl.Float64)
    mean = x64.rolling_sum(window_size=n) / n
    mean_sq = (x64 * x64).rolling_sum(window_size=n) / n
    std = (mean_sq - mean * mean).clip(lower_bound=0.0).sqrt()
    return pl.when(std > 0).then((x64 - mean) / std).otherwise(None).cast(pl.Float32)


def roc(x: pl.Expr, n: int) -> pl.Expr:
    """Tasa de cambio como **fracción**: ``x[t]/x[t-n] - 1`` (L1); nula si ``x[t-n] == 0``.

    Nula para ``t < n`` (no hay ``x[t-n]``).
    """
    prev = x.shift(n)
    return pl.when(prev != 0).then(x / prev - 1.0).otherwise(None).cast(pl.Float32)


def wma(x: pl.Expr, n: int) -> pl.Expr:
    """Media ponderada lineal: peso ``n`` a la barra actual, ``1`` a ``t-n+1`` (L1).

    ``Σ_{k=0}^{n-1} (n-k)·x[t-k] / (n(n+1)/2)``; nula para ``t < n-1``. Salida ``Float32``;
    sin cancelación (media ponderada), así que no necesita doble precisión. Solo mira el
    pasado, así que es prefijo-estable.
    """
    weighted = n * x
    for k in range(1, n):
        weighted = weighted + (n - k) * x.shift(k)
    return (weighted / (n * (n + 1) / 2.0)).cast(pl.Float32)


def obv(close: pl.Expr, volume: pl.Expr) -> pl.Expr:
    """On-Balance Volume acumulado (L1): ``obv[0] = 0``, prefijo-estable por ``cum_sum``.

    ``obv[t] = obv[t-1] + sign(close[t]-close[t-1])·volume[t]`` con ``sign in {-1,0,+1}``
    (``0`` en empate). Sin nulos.
    """
    delta = close - close.shift(1)
    step = pl.when(delta > 0).then(volume).when(delta < 0).then(-volume).otherwise(0.0)
    return step.cum_sum().cast(pl.Float32)


def vwap(high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr, n: int) -> pl.Expr:
    """VWAP de **ventana rodante** ``n`` (sin estado de sesión) (L1); nulo si ``Σ volume == 0``.

    ``tp = (high+low+close)/3``; ``vwap[t] = Σ(tp·volume)[t-n+1..t] / Σ(volume)[t-n+1..t]``;
    nulo para ``t < n-1``. Cociente de sumas positivas (sin cancelación), ``Float32``. Solo
    mira ``<= t``: prefijo-estable.
    """
    typical = (high + low + close) / 3.0
    num = (typical * volume).rolling_sum(window_size=n)
    den = volume.rolling_sum(window_size=n)
    return pl.when(den > 0).then(num / den).otherwise(None).cast(pl.Float32)


def session_vwap(
    high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr, session: pl.Expr
) -> pl.Expr:
    """VWAP **anclado a sesión**: acumulado desde la apertura de cada sesión; nulo si ``Σvol==0``.

    ``tp = (high+low+close)/3``; ``vwap[t] = Σ(tp·volume) / Σ(volume)`` **dentro de la sesión**
    (``cum_sum().over(session)``), reseteando en cada apertura. ``session`` es un id de
    agrupamiento que provee el consumidor (p. ej. fecha ET), causal y derivado de
    ``open_time_ms``; la primitiva es genérica (sin lógica de zona horaria/horario adentro).
    Cociente de sumas positivas (sin cancelación), ``Float32``. Solo mira barras ``<= t``
    dentro de la sesión: prefijo-estable (RF-20).
    """
    typical = (high + low + close) / 3.0
    num = (typical * volume).cum_sum().over(session)
    den = volume.cum_sum().over(session)
    return pl.when(den > 0).then(num / den).otherwise(None).cast(pl.Float32)


def session_vwap_sigma(
    high: pl.Expr, low: pl.Expr, close: pl.Expr, volume: pl.Expr, session: pl.Expr
) -> pl.Expr:
    """Desvío **ponderado por volumen** de ``tp`` dentro de la sesión; nulo si ``Σvol==0``.

    ``sigma[t] = sqrt(E_v[tp²] - vwap[t]²)`` con ``E_v[tp²] = Σ(tp²·volume)/Σ(volume)`` y
    ``vwap = Σ(tp·volume)/Σ(volume)``, todo acumulado dentro de la sesión
    (``cum_sum().over(session)``). El área de valor se compone en la estrategia
    (``VAH/VAL = session_vwap ± k·session_vwap_sigma``). Float64 interno (la identidad
    ``E_v[tp²] - vwap²`` sufre cancelación catastrófica en ``Float32``, **excepción RFC-0007**
    como ``rolling_std_stable``; ``session_vwap``, cociente de sumas positivas, no la necesita);
    recortado a ``>= 0`` y casteado a ``Float32``; prefijo-estable (solo mira ``<= t`` dentro de
    la sesión, RF-20). Enmienda L3 de ``specs/finances_backtester/spec.md``.
    """
    typical = ((high + low + close) / 3.0).cast(pl.Float64)
    vol = volume.cast(pl.Float64)
    sum_v = vol.cum_sum().over(session)
    mean_tp = (typical * vol).cum_sum().over(session) / sum_v
    mean_tp2 = (typical * typical * vol).cum_sum().over(session) / sum_v
    variance = (mean_tp2 - mean_tp * mean_tp).clip(lower_bound=0.0)
    return pl.when(sum_v > 0).then(variance.sqrt()).otherwise(None).cast(pl.Float32)


def rolling_max_prev(x: pl.Expr, window: int) -> pl.Expr:
    """``max(x[t-window..t-1])``: excluye la barra actual; nulo para ``t < window``."""
    return x.shift(1).rolling_max(window_size=window)


def rolling_min_prev(x: pl.Expr, window: int) -> pl.Expr:
    """``min(x[t-window..t-1])``: excluye la barra actual; nulo para ``t < window``."""
    return x.shift(1).rolling_min(window_size=window)


def position_from_events(enter: pl.Expr, exit_: pl.Expr) -> pl.Expr:
    """Deriva una posición sostenida largo/plano a partir de eventos de entrada/salida.

    ``enter`` tiene prioridad sobre ``exit_`` en el mismo ``t``. Sigue la semántica
    ``when(enter).then(1).when(exit_).then(0).otherwise(None)``, luego ``forward_fill()``
    y ``fill_null(0)``, con cast final a ``Int8``.
    """
    return (
        pl.when(enter)
        .then(1)
        .when(exit_)
        .then(0)
        .otherwise(None)
        .forward_fill()
        .fill_null(0)
        .cast(pl.Int8)
    )


__all__ = [
    "adx",
    "atr",
    "bbands_bandwidth",
    "bbands_pctb",
    "cci",
    "cross_above",
    "cross_below",
    "ema",
    "ichimoku",
    "mfi",
    "minus_di",
    "obv",
    "plus_di",
    "position_from_events",
    "roc",
    "rolling_max_prev",
    "rolling_min_prev",
    "rolling_std_stable",
    "rsi_wilder",
    "session_vwap",
    "session_vwap_sigma",
    "sma",
    "stoch_d",
    "stoch_k",
    "vwap",
    "williams_r",
    "wma",
    "zscore",
]
