# Diseño: Biblioteca de estrategias técnicas mono-activo

> Documento de **investigación y diseño** (previo a cualquier spec/código). Define qué
> estrategias construir, con qué primitivas, y —lo más importante— **cómo decidir honestamente
> si tienen filo**. Al acordarse, cada lote se baja a su tridente SDD (`specs/ft_<kind>/` o un
> spec de familia) e implementación.

## 0. Principio rector (leer primero)

**La rentabilidad no es un requisito de entrada a la biblioteca; es el resultado de validar
honestamente out-of-sample.** Juntar "estrategias que ganaron en el pasado" produce
*overfitting*, *survivorship bias* y *data-snooping*. Por eso:

- La biblioteca guarda **hipótesis de edge bien implementadas y testeadas**, agrupadas por
  **tipo de edge** — es **edge-agnóstica** (no prejuzga quién gana).
- Un **proceso separado de validación OOS** (walk-forward anclado, ya construido en
  `ft_vectorbt optimize`) arma un **leaderboard por (estrategia, activo, timeframe)**. Ese
  proceso —no la biblioteca— dice qué tiene filo.

Decidido con el usuario (2026-11-08): **amplitud por tipo de edge**, **mono-activo técnico
(OHLCV)**, **criterio de entrada = hipótesis bien hecha** (validación OOS aparte).

**Decisiones de contrato (2026-11-08, resueltas):**
- **Volumen SÍ**: se carga `volume` al frame de `signals` (abre la familia volumétrica).
- **Longs + shorts SÍ**, en forma **incremental**: el *contrato de señales* migra ya a
  **posición-objetivo** `pos: Series[int8] ∈ {-1,0,+1}` (reemplaza los 2 booleanos
  `entries/exits`); el **motor arranca long/flat** (honra solo `{0,+1}`) y la **ejecución en
  corto** (`-1`) llega como un empujón de motor posterior, **sin reescribir estrategias** (ya
  nacen con el contrato final).
- **Por estrategia NO hay spec**: la carpeta `strategies/<kind>/` (`logic.py` + `README.md` con
  diagramas/ecuaciones) + tests + golden es la unidad (camino liviano). Los **cambios de
  contrato** (volumen, migración a posición-objetivo, ejecución short) **sí** llevan tridente SDD.
- **Base única de autoría**: `docs/guides/strategy-authoring.md` + la estructura de `README`
  obligatoria. Se enmiendan al entrar volumen/posición-objetivo/short.

## 1. Restricciones del motor hoy (contrato de autoría)

Toda estrategia es una subclase de `BaseStrategy` en
`packages/finances-core/src/finances_core/backtest/strategies/<kind>/logic.py` con:
`kind`, `version` (SemVer), `description`, `Params` (Pydantic `extra="forbid"`),
`default_params`, `warmup(params)->int`, y `signals(candles, params)->Signals(entries, exits)`.

Reglas duras (las verifica `assert_strategy_contract` / el AST guard):

1. **Long/flat únicamente** (`side={1}`): `entries` abre largo, `exits` cierra. **No hay corto.**
2. **Sin volumen en v1.0.0**: `signals` recibe `open_time_ms, open, high, low, close` (Float32).
   **No hay `volume`.**
3. **Puro y determinista**, solo `Polars` Float32; prohibido `pandas/numpy/float64`,
   `eval/exec/open`, e **imports a herramientas hermanas `ft_*`** (aislamiento). Nada de
   `shift(-1)` ni mirar barras `> t` (guard anti-anticipación por prueba de prefijo).
4. `signals` devuelve dos `pl.Series` **Boolean** de largo `n`; la señal se evalúa al cierre de
   `t`; el *shift* t→t+1 de ejecución lo hace el motor.
5. README obligatorio (4 secciones + diagrama mermaid + tabla de parámetros) y golden fixtures
   para los *built-in*.

### 1.1 Primitivas disponibles hoy (`strategies/_lib/`)
`sma(x,n)`, `cross_above(a,b)`, `cross_below(a,b)`, `rsi_wilder(close,period)`,
`rolling_max_prev(x,w)`, `rolling_min_prev(x,w)`. Todo lo demás se compone inline en Polars.

### 1.2 Cambios de contrato EN ALCANCE (cada uno su tridente SDD, alto riesgo)
Resueltos con el usuario; van **primero** porque las estrategias dependen de ellos:

- **G1 — Cargar `volume`** (Float32) al frame de `signals` (hoy RF-4 lo excluye). Habilita OBV,
  VWAP, MFI, volume-breakout, confirmación por volumen.
- **G2a — Migrar el contrato de señales a posición-objetivo** `{-1,0,+1}` (reemplaza
  `Signals(entries, exits)` booleanos). `finalize`/runner/entrada del motor adaptados; el motor
  **honra solo `{0,+1}`** por ahora (long/flat). Migrar las 3 estrategias actuales + `_template`;
  **el golden de las 3 debe quedar idéntico** (refactor que preserva comportamiento, verificable).
- **G2b — Ejecución en corto** (motor honra `-1`): posición corta en VectorBT, P&L de corto,
  `TRADES_SCHEMA.side ∈ {-1,1}`, métricas de corto. **Posterior** (empujón de motor); no reescribe
  estrategias. Habilita las variantes short de cada familia.

Enmendar en cada uno: `strategy-authoring.md`, `_template`, el check de README y los goldens.

## 2. Primitivas a crecer en `_lib` (fundación reutilizable)

Antes de las estrategias conviene ampliar `_lib` con primitivas **puras, Float32 y
prefijo-estables**. ⚠️ Lección del fix de Bollinger (RFC-0006): **`rolling_std`/`rolling_var` de
Polars NO son prefijo-estables bit a bit** — toda varianza/σ se computa con `rolling_sum`
(estable) y, si la precisión lo exige, acumulando en Float64 y bajando a Float32.

| Primitiva | Qué | Usada por |
|---|---|---|
| `ema(x, n)` | media exponencial (recursiva, seed documentado) | MACD, Keltner, trend |
| `atr(h,l,c, n)` | Average True Range (Wilder) | volatilidad, Keltner, stops |
| `rolling_std_stable(x, n)` | σ poblacional **prefijo-estable** (ver RFC-0006) | Bollinger, z-score |
| `bbands(c, n, k)` | medio ± k·σ | reversión/breakout de bandas |
| `macd(c, f, s, sig)` | EMA_f − EMA_s y su señal | momentum/trend |
| `stoch(h,l,c, k, d)` | %K/%D estocástico | reversión/oscilador |
| `adx(h,l,c, n)` | fuerza de tendencia | filtro de régimen |
| `zscore(x, n)` | (x − media)/σ, prefijo-estable | reversión estadística |
| `roc(c, n)` | tasa de cambio (momentum) | momentum |
| `wma(x, n)` | media ponderada lineal | variantes de MA |

Cada primitiva se agrega con su test (incl. **prueba de prefijo bit a bit**, como el resto del
contrato). `_lib` versiona por `CATALOG_VERSION`, no por `code_hash` de cada estrategia.

## 3. Taxonomía de familias (el catálogo objetivo)

Agrupadas por **tipo de edge**. Las ✅ son implementables ya; 🔶 requieren crecer `_lib`; 🔒
dependen de un *gate* (G1 volumen / G2 corto). Cada una lista la hipótesis y la señal long/flat.

### A. Seguimiento de tendencia / momentum
- `sma_cross` ✅ (existe). Variantes: `ema_cross` 🔶, `triple_ma` 🔶, `ma_slope` (pendiente de la MA) 🔶.
- `donchian_breakout` ✅ (existe). Variantes: `keltner_breakout` 🔶 (EMA ± k·ATR), `bollinger_breakout` 🔶.
- `macd_trend` 🔶 (cruce MACD/señal, o histograma > 0).
- `momentum_roc` 🔶 (entrar si `roc(close,n) > umbral`; momentum de series temporales / "absolute momentum").
- `high_breakout` ✅ (nuevo máximo de `w` barras vía `rolling_max_prev`; estilo Turtle/52-week-high).
- `adx_trend_filter` 🔶 (tendencia sólo si `adx > umbral`; típicamente **combinador**, ver E).

### B. Reversión a la media / contratendencia (long-only = comprar debilidad)
- `rsi_reversion` ✅ (existe). Variantes: `rsi2_connors` 🔶 (RSI(2) < 5, salir > 50; con filtro SMA200).
- `bollinger_reversion` 🔶 (comprar bajo la banda inferior, salir en el medio).
- `zscore_reversion` 🔶 (comprar si `zscore(close,n) < −k`).
- `stoch_reversion` 🔶 (%K sale de sobreventa).
- `williams_r` 🔶 (equivalente a %R).

### C. Volatilidad
- `atr_channel_breakout` 🔶 (entrar en `close > open + k·ATR`; volatility breakout clásico).
- `squeeze` 🔶 (Bollinger dentro de Keltner → ruptura; detección de compresión → expansión).
- `vol_regime_filter` 🔶 (operar sólo en régimen de baja/alta volatilidad; **combinador**).

### D. Price action / estructura
- `channel_breakout` ✅ (soporte/resistencia por `rolling_max/min_prev`; cercano a donchian).
- `inside_bar_breakout` ✅ (barra interior → ruptura del rango).
- `higher_highs` 🔶 (swing structure: secuencia de máximos/mínimos crecientes).

### E. Combinadores (filtro de régimen + gatillo) — donde suele vivir la robustez
Patrón: **un filtro lento** (ej. `close > sma200` = sólo largos en tendencia alcista) **+ un
gatillo rápido** (ej. RSI(2) sobreventa). No son una familia nueva de indicadores sino una
**composición** parametrizable. Ejemplos: `trend_filtered_reversion` 🔶, `regime_momentum` 🔶.
Diseño: permitir que una estrategia tome un `trend_filter` y un `entry_trigger` como params
(o estrategias dedicadas por combinación).

### F. Volumétricas 🔒 G1 (diferidas hasta cargar volumen)
`obv_trend`, `vwap_reversion`, `volume_breakout`, `mfi_reversion`.

### G. Patrones de velas (candlestick)
TA-Lib (vía `ft_ta_lib`) tiene el catálogo, **pero las estrategias no pueden importar `ft_ta_lib`**
(aislamiento). Dos caminos: (i) implementar inline en `_lib` unos pocos patrones robustos
(engulfing, hammer, doji) como primitivas; (ii) diferir. Recomiendo **diferir** hasta tener
demanda — los patrones sueltos rara vez tienen edge sin contexto.

## 4. Protocolo de validación honesto (lo que separa esto de un backtest de juguete)

La biblioteca es edge-agnóstica; la verdad la dice **este proceso**, aparte:

1. **Siempre con costos**: fees + slippage (modelo de costos ya existe). Un edge que no sobrevive
   costos no existe.
2. **Walk-forward anclado** (ya en `optimize`): el score que cuenta es **out-of-sample**. Nunca
   se elige por in-sample.
3. **Holdout intocable**: reservar un tramo final (o activos) que **no se usa** ni para optimizar
   ni para elegir. Se mira **una sola vez** al final.
4. **Benchmark buy-and-hold obligatorio**: en mono-activo largo, comparar contra comprar y
   mantener, **ajustado por riesgo** (una estrategia long-only sobre BTC en un bull "gana" trivial;
   lo relevante es Calmar/Sharpe **vs** B&H y el drawdown).
5. **Multiple-testing / data-snooping** (CRÍTICO al barrer N estrategias × M activos × params):
   el mejor Sharpe de muchos intentos está **inflado**. Mitigar con:
   - Registrar el **número de pruebas** (combinaciones evaluadas).
   - **Deflated Sharpe Ratio** (Bailey & López de Prado) y/o **PBO** (Probability of Backtest
     Overfitting, CSCV) para descontar el inflado por multiplicidad.
   - **Longitud mínima de track record** / mínimo de operaciones (ya tenemos `--min-trades`).
6. **Robustez de parámetros**: preferir **mesetas** (vecindad de params que anda parecido) a picos
   aislados — se lee de los fold scores y de barrer vecinos.
7. **Por activo y régimen**: el leaderboard es por `(estrategia, activo, timeframe)`, nunca "la
   mejor estrategia" global. Reportar desempeño por sub-períodos (bull/bear/lateral).

Entregable futuro: un comando/flujo (`ft_vectorbt screen` o similar, spec aparte) que corre la
grilla estrategia×activo con este protocolo y emite el leaderboard con las métricas anti-overfit.

## 5. Estructura, versionado y convenciones

- Un folder `strategies/<kind>/` por estrategia (built-in) con `logic.py` + `README.md` + golden.
- Familias como **prefijo de nombre** para navegar: `ma_*`, `rsi_*`, `bb_*`, `atr_*`, `chan_*`…
  (o documentar el mapeo familia→kind en el README del catálogo).
- `version` SemVer por estrategia; cambia `code_hash`→`run_id` al editar lógica. `CATALOG_VERSION`
  sólo si cambia el contrato o `_lib`.
- Cada estrategia: `Params` con rangos y relaciones (p. ej. `fast < slow`), `warmup` correcto,
  `check_params` con sets ricos en señal para la prueba de contrato, `metrics_target` orientativo.
- Tests por estrategia: contrato (prefijo/anti-anticipación), golden fixtures, y casos de borde
  de la lógica.

## 6. Roadmap (orden acordado; cada contrato con tridente SDD, cada lote de estrategias liviano)

- **Lote 0 — Contrato (SDD, alto riesgo)**: **G1** (cargar `volume`) + **G2a** (migrar señales a
  posición-objetivo `{-1,0,+1}`, motor long/flat). Incluye migrar las 3 estrategias actuales +
  `_template` + enmendar `strategy-authoring.md`/checks; **golden idéntico** como prueba.
- **Lote 1 — Fundación `_lib`** (SDD de `_lib`/catalog): `ema`, `atr`, `rolling_std_stable`,
  `zscore`, `roc`, `wma`, y volumétricas `obv`/`vwap` (ya hay volumen), con tests incl. **prefijo**.
- **Lote 2 — Momentum/trend** (liviano): `ema_cross`, `macd_trend`, `momentum_roc`, `high_breakout`.
- **Lote 3 — Reversión**: `bollinger_reversion`, `zscore_reversion`, `rsi2_connors`.
- **Lote 4 — Volatilidad + combinadores**: `keltner_breakout`, `atr_channel_breakout`,
  `trend_filtered_reversion`.
- **Lote 5 — Price action + volumétricas**: `channel_breakout`, `inside_bar_breakout`, `obv_trend`,
  `vwap_reversion`.
- **Motor shorts (SDD, G2b)**: habilita las variantes short de los lotes anteriores (sin
  reescribir estrategias).
- **Validación** (`screen`/leaderboard con anti-overfit) — spec propio, **en paralelo** desde el
  Lote 2 (para validar el loop con las primeras estrategias).

## 7. Decisiones (tomadas 2026-11-08) y lo que queda abierto

**Tomadas:** volumen SÍ · longs+shorts SÍ (incremental: contrato posición-objetivo ya, ejecución
short después) · por-estrategia sin spec (carpeta+README+tests) · contratos con SDD · base de
autoría = `strategy-authoring.md` + estructura de README · validación OOS honesta aprobada ·
secuencia incremental (Lote 0 contrato → `_lib` → estrategias; validación en paralelo).

**Abierto (se resuelve al llegar):**
1. **Combinadores** (Lote 4): ¿estrategias dedicadas por combinación, o una parametrizable que
   tome `trend_filter` + `entry_trigger`? (afecta el diseño de `Params`).
2. **Validación** (`screen`): ¿herramienta dedicada o un flujo sobre `optimize`? (se decide al
   abrir su spec, en paralelo al Lote 2).
