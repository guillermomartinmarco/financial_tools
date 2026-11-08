# RFC-0007 — Varianza de `strategies/_lib` acumulada en `Float64` por prefijo-estabilidad y precisión (excepción Art. 5.3 / RFC-0004, en continuidad con RFC-0006)

| Campo | Valor |
|---|---|
| Estado | **Aprobado** (2026-11-08, Tech Lead) — excepción Art. 5.3 sobre RFC-0004; aplicado en el PR del Lote 1 |
| Fecha | 2026-11-08 |
| Artículo afectado | Art. 5.3 (Conversión y Validación) — **excepción/aclaración**, no enmienda |
| Alcance | `packages/finances-core/src/finances_core/backtest/strategies/_lib/__init__.py`, funciones `rolling_std_stable` y `zscore` (Enmienda L1 de `specs/finances_backtester/spec.md`) |
| Referencias | RFC-0004 (frontera `Float64` de los motores; `backtest/**` es `Float32`), RFC-0006 (mismo problema en `ft_indicators`), `docs/design/strategy-library.md`, `tests/unit/test_backtest_isolation.py`, `tests/unit/test_strategies_lib.py` |

## Contexto
El subsistema de señales (`finances_core/backtest/**`, incluidas las ayudas `strategies/_lib`) computa **solo en
`Float32`** por política constitucional (RF-11, RFC-0004); el test de pureza `test_backtest_isolation.py` prohíbe el
nombre `Float64`/`double` en todo el árbol. A la vez, las señales deben ser **prefijo-estables bit a bit** (sin
anticipación, RF-20): `f(x[:k]) == f(x)[:k]` para todo `k`.

El Lote 1 de la biblioteca de estrategias añade la primitiva `rolling_std_stable` (desviación estándar poblacional
rodante) y `zscore` (que la usa). Ambas chocan con la misma tensión que resolvió RFC-0006 en `ft_indicators`.

## Motivación (dos requisitos en tensión)
1. **Prefijo-estabilidad (RF-20)**: `rolling_std`/`rolling_var` de Polars **no** son prefijo-estables bit a bit
   (acumulan por *chunks* según el largo total; diferencias de 1 ULP — bug Bollinger, PR #50). Obliga a reconstruir σ
   con el primitivo estable `rolling_sum` vía la identidad `σ² = E[x²] − E[x]²`.
2. **Precisión `Float32`**: esa identidad sufre **cancelación catastrófica** en `Float32`. Medido sobre este contrato:
   con precios a escala BTC (~50 000) el σ en `Float32` da **171 % de error relativo** (22.63 vs 14.69 reales); a
   escala ~100, 1.8 %. El *two-pass* centrado numéricamente estable no es expresable de forma vectorizada y
   período-independiente con `rolling_sum` (misma conclusión que RFC-0006).

## Propuesta
- `rolling_std_stable` y `zscore` **acumulan la varianza en `Float64`** (sumas rodantes de `x` y de `x²`, identidad
  `E[x²] − E[x]²` con `clip(≥ 0)` por la cancelación residual) y **bajan el resultado a `Float32`** con un único `cast`
  al final. `Float64` **no sale** de esas dos funciones.
- El resto de la fundación `_lib` del Lote 1 permanece **`Float32` puro**: `ema`, `atr` (Wilder), `wma` (media
  ponderada, sin resta de magnitudes grandes), `roc`, `obv` y `vwap` (cociente de sumas positivas) no sufren
  cancelación y no usan `Float64`.
- El test de pureza se actualiza con una **excepción nominal y acotada** (`DOUBLE_EXCEPTIONS`): permite `Float64` solo
  dentro de `rolling_std_stable`/`zscore` de `_lib`; cualquier otro uso en `backtest/**` (incluida otra función de
  `_lib`) sigue siendo infracción. Un test lo verifica (`test_double_exception_is_scoped_to_named_lib_functions`).

## Consecuencia explícita
La σ (y por tanto `zscore` y las bandas/indicadores que la usen) **no** es bit-reproducible en un cómputo puramente
`Float32`: requiere `Float64` para la subcuenta de varianza. Es coherente con RFC-0004/RFC-0006: la doble precisión
queda **confinada** a la acumulación interna y el **contrato de salida sigue siendo `Float32`** (Art. 5.1 intacto); el
dato persistido y las señales expuestas son `Float32`.

## Verificación (obligatoria para aceptar la excepción)
- `test_strategies_lib.py`: valor hand-computed de `rolling_std_stable`/`zscore` (población) ✓ y
  `test_primitive_is_prefix_stable_bit_for_bit` sobre las 12 primitivas ✓ (incluye `rolling_std_stable`/`zscore`).
- `test_backtest_isolation.py`: excepción `Float64` acotada por nombre de función ✓; fuera de alcance sigue flagueada ✓.
- `run_id` y goldens de `sma_cross`/`rsi_reversion`/`donchian_breakout` **byte-idénticos**; `CATALOG_VERSION` **no**
  cambia (L1 es aditivo, ninguna integrada usa las nuevas ayudas).
- Suite `finances-core` verde; `ruff` + `ruff format --check` + `mypy --strict` verdes.

## Alternativas descartadas
- **`rolling_std` de Polars**: estable en `f32` pero **no prefijo-estable** (el bug original).
- **`E[x²] − E[x]²` en `Float32`**: prefijo-estable pero inutilizable por cancelación (171 % a escala BTC).
- **`f32` centrado por constante global**: reduce la cancelación pero reaparece con *drift* grande de precio; más
  código y menos robusto que confinar `Float64`.
- **Diferir `rolling_std_stable`/`zscore`**: deja incompleta una familia entera de estrategias (volatilidad, reversión
  por z-score) sin beneficio.

## Impacto
Sin cambios para otras herramientas ni para el formato de datos persistido. No modifica la constitución (vía de
excepción que el propio Art. 5.3 prevé, en continuidad con RFC-0004/RFC-0006); **no requiere *bump* de versión** ni de
`CATALOG_VERSION`.

## Aprobación
- [x] Aprobado por el Tech Lead — Firma: Guillermo Martín Marco Fecha: 08/11/2026
