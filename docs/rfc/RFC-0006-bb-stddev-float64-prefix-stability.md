# RFC-0006 — Desviación estándar de Bollinger acumulada en `Float64` por prefijo-estabilidad (excepción Art. 5.3, sobre RFC-0001)

| Campo | Valor |
|---|---|
| Estado | **Aprobado** (2026-11-07, Tech Lead) — excepción Art. 5.3 sobre RFC-0001; aplicado en PR #50 |
| Fecha | 2026-11-07 |
| Artículo afectado | Art. 5.3 (Conversión y Validación) — **excepción/aclaración**, no enmienda |
| Alcance | `packages/finances-analytics/.../ft_indicators/kernels/windowed.py` (kernel `bb`) |
| Referencias | RFC-0001 (cálculo `Float64` en `ft_indicators`), `tests/ft_indicators/test_properties.py` (RF-3, prefijo-estable), `tests/ft_indicators/test_precision.py`, PR #50 (fix) |

## Contexto
`ft_indicators` garantiza la propiedad **"sin anticipación / prefijo estable bit a bit"** (RF-3): para todo `t`,
calcular un indicador sobre `[0..t]` da **bit a bit** el mismo valor en `≤ t` que calcularlo sobre la serie completa.
Es la base de su rol de **oráculo** (RFC-0001) para los futuros kernels de GPU.

El kernel de Bollinger (`bb`) calculaba σ con `rolling_std` de Polars. Se detectó (PR #50) que **`rolling_std` de Polars
no es prefijo-estable bit a bit**: acumula por *chunks* según el largo total del array y produce diferencias de **1 ULP**
(p. ej. ventana `[500, 1, 1000]` → prefijo `1112.0934987576604` vs serie completa `…602`). Esto rompía
`test_prefix_is_stable_bit_for_bit`. `bb_middle` (SMA) nunca falló porque usa el primitivo de ventana estable
`rolling_sum`.

## Motivación (dos requisitos en tensión)
1. **Prefijo-estabilidad (RF-3)**: exige reconstruir σ con `rolling_sum` (estable) en lugar de `rolling_std`.
2. **Precisión `Float32`**: la identidad `σ² = E[x²] − E[x]²` sufre **cancelación catastrófica** en `Float32`
   (excedía la tolerancia publicada por `0.00238` en `bb_*_upper`). La alternativa numéricamente estable —el *two-pass*
   centrado— no es expresable de forma vectorizada y período-independiente con `rolling_sum`, y **desenrollarla es
   inviable** porque `period` llega a `MAX_PERIOD = 10 000`.

## Propuesta
- El kernel `bb` **acumula la varianza y calcula σ en `Float64`** (vía `rolling_sum` de `x` y de `x²`, identidad
  `E[x²] − E[x]²`, con `clip(≥0)` por la cancelación residual) y **baja σ al `dtype` de salida** con un único `cast`.
- Esto **fija `Float64` para la subcuenta de la varianza aun cuando el kernel se invoque con `dtype = Float32`**
  (único caso: el test de *alcanzabilidad* `test_precision`). Es coherente con RFC-0001: la **producción ya computa
  en `Float64`** (`COMPUTE_DTYPE = Float64`) y sólo baja a `Float32` al persistir. En `Float64` la identidad conserva
  ~10 dígitos significativos → holgadamente dentro de la tolerancia `Float32`.
- **El contrato de datos no cambia**: lo persistido en `FT_INDICATORS_DIR` y lo exportado sigue siendo `Float32`
  (Art. 5.1 intacto). `Float64` no sale de `kernels/` + `engine.py` (RFC-0001).

## Consecuencia explícita
Para `bb`, la tolerancia `Float32` publicada se alcanza **gracias a** la acumulación en `Float64` de la varianza:
las bandas de Bollinger **no** son bit-reproducibles en un motor puramente `Float32`; requieren `Float64` para σ. Esto
es aceptable porque el motor de producción es `Float64` (RFC-0001).

## Verificación (obligatoria para aceptar la excepción)
- `test_properties.py::test_prefix_is_stable_bit_for_bit` y `test_altering_future_rows_does_not_change_the_past` ✓
  (prefijo-estable bit a bit).
- `test_precision.py` (f32 end-to-end dentro de tolerancia) ✓; `test_oracle` ✓; `test_reference_fixtures` (StockCharts) ✓;
  `test_talib_parity` (paridad BBANDS: la identidad coincide con TA-Lib) ✓; `test_kernels_windowed` ✓.
- `ft_indicators`: **408 passed**; `ruff` + `ruff format` + `mypy --strict` verdes.

## Alternativas descartadas
- **`rolling_std` de Polars**: numéricamente estable en `f32` pero **no prefijo-estable** (el bug original).
- **`E[x²] − E[x]²` en `Float32`**: prefijo-estable pero excede la tolerancia por cancelación (`0.00238`).
- ***Two-pass* centrado desenrollado**: numéricamente estable y prefijo-estable, pero inviable con `period ≤ 10 000`
  (hasta 10 000 términos por fila).

## Impacto
Sin cambios para otras herramientas ni para el formato de datos persistido. No modifica la constitución (aplica la vía
de excepción que el propio Art. 5.3 prevé, en continuidad con RFC-0001); **no requiere *bump* de versión**.

## Aprobación
- [x] Aprobado por el Tech Lead — Firma: Guillermo Martín Marco Fecha: 07/11/2026
