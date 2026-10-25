# RFC-0002 — Salidas enteras `Int16` y frontera `float64` de TA-Lib en `ft_ta_lib` (excepciones Art. 5)

| Campo | Valor |
|---|---|
| Estado | **Propuesto** — pendiente de aprobación del Tech Lead |
| Fecha | 2026-10-25 |
| Artículos afectados | Art. 5.1 (tipos canónicos) y Art. 5.3/5.4 (mayor precisión en el camino de datos) — **excepciones**, no enmiendas |
| Alcance | Solo `packages/finances-analytics/.../ft_ta_lib/engine.py` (frontera `float64`) y las columnas enteras que persiste `ft_ta_lib` |
| Referencias | `specs/ft_ta_lib/spec.md` (RF-7), `specs/ft_ta_lib/plan.md` (§3.3, ADR-T05, ADR-T06), RFC-0001 |

## Contexto
El Art. 5.1 fija que los datos normalizados usan **exclusivamente** `float32` (precios, volúmenes, indicadores) e
`int64` (timestamps ms). El Art. 5.3 exige RFC y aislamiento para cualquier caso de mayor precisión, y el 5.4 prohíbe
`float64` en *feature engineering*. `ft_ta_lib` envuelve TA-Lib, que plantea dos situaciones que el artículo no
contempla:
1. Las funciones de reconocimiento de patrones (61) y algunas categóricas (`HT_TRENDMODE`) devuelven **enteros**
   (−100/0/+100, y ±200 en `CDLHIKKAKE`/`CDLHIKKAKEMOD`), no magnitudes reales.
2. TA-Lib C solo calcula en `double`, y el wrapper de Python exige arrays `float64`.

## Propuesta
1. **`Int16`** para salidas enteras de patrones y categóricas. `Int8` no es suficiente (±200 excede −128…127).
   Se controla el rango: un valor fuera de `Int16` falla el item, nunca se trunca.
2. **Frontera `float64`**: `engine.py` convierte las entradas `Float32` a `float64` solo para llamar a TA-Lib y
   convierte las salidas reales a `Float32` en un único punto. `float64` no sale de `engine.py`.
3. Las salidas de índice (`MAXINDEX`, `MININDEX`, `MINMAXINDEX`) se persisten como `open_time_ms` `Int64`: cumple el
   Art. 5.1 sin excepción.

## Verificación (obligatoria para aceptar las excepciones)
- Test: toda columna persistida por `ft_ta_lib` es `Float32`, `Int16` (solo salidas `pattern`/`category`) o `Int64`
  (solo `open_time_ms` e índices convertidos).
- Test: `CDLHIKKAKE` conserva ±200; un valor simulado fuera de `Int16` produce `output_out_of_range`.
- Test AST: `float64` y `talib` solo aparecen en `ft_ta_lib/engine.py` (y metadatos en `catalog.py`/`resolver.py`).

## Alternativas descartadas
- **`Int8`**: no representa ±200.
- **`Float32` para patrones** (cumpliría el Art. 5.1 sin RFC; valores exactos): se descarta como opción principal
  porque oculta la naturaleza categórica de la columna y duplica el tamaño frente a `Int16`; **queda como alternativa
  si este RFC se rechaza** (cambio local en `engine.py` y RF-7).
- **Calcular en `float32` con TA-Lib**: imposible; TA-Lib no tiene ruta `float32`.

## Impacto
Solo `ft_ta_lib`. Consumidores que unan columnas de patrones deben aceptar `Int16`. No modifica la constitución
(aplica la vía de excepción del Art. 5.3; para el Art. 5.1 se solicita la misma vía por analogía).

## Aprobación
- [ ] Aprobado por el Tech Lead — Firma: ________________ Fecha: __________
