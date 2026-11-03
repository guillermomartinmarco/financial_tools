# RFC-0004 — Frontera `float64` y `pandas` de los motores de backtesting (excepción Art. 5.3/5.4 y enmienda Art. 8)

| Campo | Valor |
|---|---|
| Estado | **Aprobado** (2026-11-02, Tech Lead) — enmienda aplicada en `docs/constitution.md` v1.2.0 |
| Fecha | 2026-11-02 |
| Artículos afectados | Art. 5.3/5.4 (mayor precisión en el camino de datos) — **excepción**. Art. 8 (Polars sobre Pandas) — **enmienda** (el Art. 8 no prevé excepciones por RFC; ver «Propuesta», punto 6) |
| Alcance | Solo `packages/finances-backtester/src/finances_backtester/<ft_motor>/engine.py` de cada motor basado en bibliotecas `pandas`/NumPy (`ft_vectorbt`, y a futuro `ft_backtesting_py`, `ft_backtrader`, `ft_bt`, `ft_ffn`). **No** alcanza a `finances_core.backtest` ni a `ft_backtestopt_spectre` |
| Referencias | `specs/finances_backtester/spec.md` (RF-11), `specs/ft_vectorbt/spec.md` (RF-3), `specs/ft_vectorbt/plan.md` (ADR-V02), RFC-0001, RFC-0002, RFC-0003 |

## Contexto
El Art. 5.1 fija `float32` para precios, volúmenes y métricas derivadas e `int64` (ms UTC) para tiempos. El Art. 5.4
prohíbe `float64` en rutas de procesamiento masivo salvo excepciones aprobadas por RFC (Art. 5.3), aisladas en un módulo
declarado, con un único punto de conversión a los tipos canónicos y verificadas por un test automático. El Art. 8 fija
Polars por defecto y admite Pandas solo en la capa de visualización final.

Los motores de backtesting de terceros (VectorBT, backtesting.py, Backtrader, bt, ffn) son bibliotecas construidas sobre
`pandas` y NumPy `float64` (y, en VectorBT, kernels Numba sobre `float64`). No ofrecen una ruta `float32`, y su
interfaz de entrada y salida son `pandas.Series`/`DataFrame` con índice temporal. Sin esta excepción esos motores no
pueden integrarse en la suite.

## Motivación
1. **Los motores son bibliotecas de terceros que solo operan en doble precisión**: reescribirlos en `float32` equivale a
   no usarlos (y perdería el valor de contrastar contra motores maduros).
2. **Rol de oráculo CPU**: los motores CPU de este paquete son la referencia contra la que se validará el motor GPU
   `ft_backtestopt_spectre`, que será `float32` nativo (Art. 11). Un oráculo con la misma aritmética que el candidato no
   mide su error; uno en doble precisión sí.
3. **Acumulación**: el valor de una cartera acumula miles de operaciones y barras (equity compuesto); en `float32` el
   error de redondeo acumulado puede alterar decisiones dependientes del saldo (tamaño de posición, margen) y, con ello,
   la secuencia de operaciones.

## Propuesta
1. **Exceptuar solo el interior del motor**: cada motor aísla `pandas`, NumPy `float64` y la biblioteca de terceros en un
   único módulo `engine.py`. Fuera de ese módulo —incluidos `service.py`, `cli.py`, `api.py` y todo `finances_core`—
   no aparecen `float64`, `pandas` ni la biblioteca del motor.
2. **Exactamente dos puntos de conversión** dentro de `engine.py`:
   - `to_framework_input(candles)`: de `polars.DataFrame` canónico (`Float32`/`Int64`) a la entrada del framework
     (`float64`). La ampliación `float32` → `float64` es exacta (sin pérdida).
   - `from_framework_output(...)`: de la salida del framework a los DataFrames canónicos de resultados (trades y equity)
     con `Float32`/`Int64`/`Int8`. Es el **único** punto con *downcast*, que sigue el Art. 5.3 (pérdida aceptada y
     documentada) y rechaza valores no finitos o fuera de rango (Art. 13).
3. **Tiempos siempre `int64`**: el índice del framework es posicional (`RangeIndex`); los `open_time_ms` nunca se
   convierten a `datetime`/`float`. `exit_time_ms` y `entry_time_ms` se recuperan por posición desde el arreglo `int64`
   original. Así no hay objetos con zona horaria (Art. 5.4, segundo punto) ni pérdida de milisegundos.
4. **Qué no se calcula en el motor**: las métricas del resumen (Sharpe, drawdown, etc.) se calculan en
   `finances_core.backtest.metrics` desde las series canónicas `Float32`, idénticas para todos los motores. El motor solo
   aporta trades y equity.
5. **Política de tolerancia frente a los resultados canónicos** (valores iniciales, a calibrar en `specs/ft_vectorbt/tasks.md`
   V1.7 con evidencia y registrar en el `plan.md`):
   - Exactos: número de operaciones, `entry_time_ms`, `exit_time_ms`, `side`.
   - Precios de entrada/salida: error relativo ≤ 1e-6 (≈ 8 ulp de `float32`).
   - `pnl`, `fees`, `return`, `equity`: error relativo ≤ 1e-4 con un error absoluto mínimo documentado.
   - `drawdown`: error absoluto ≤ 1e-5. Métricas del resumen: error relativo ≤ 1e-3.
   La diferencia entre motores o frente al *golden* fuera de estas tolerancias es un defecto del adaptador, nunca un
   motivo para ampliar la tolerancia sin RFC.
6. **Enmienda del Art. 8** (propuesta de texto, versión de la constitución 1.2.0): añadir al final del artículo «*Pandas
   también se admite dentro del módulo `engine.py` de un motor de backtesting aprobado por RFC, cuando la biblioteca del
   motor lo exija, sin salir de ese módulo ni aparecer en las capas de servicio, CLI o API.*». Mientras el Art. 8 no se
   enmiende, los motores basados en `pandas` no pueden fusionarse.
7. **Motor GPU**: `ft_backtestopt_spectre` opera nativamente en `float32`/`int64` (Art. 11, 19), sin esta excepción, y
   se validará contra los motores CPU con las tolerancias del punto 5 (o las que fije su propio RFC futuro, que no se
   redacta aquí).

## Verificación (obligatoria para aceptar la excepción)
- **Test AST de aislamiento** (por motor, como `ft_ta_lib/test_isolation.py`): `float64`/`Float64`/`double` (atributo,
  nombre o literal), `pandas`, `numpy` con dtype de doble precisión y la biblioteca del motor solo aparecen en
  `engine.py`; ningún módulo del motor importa herramientas hermanas, `finances_downloader`, `finances_analytics` ni
  `finances_api`. Un test análogo cubre `finances_core/backtest/` y `schemas/backtest.py` (sin `pandas`, sin `float64`).
- **Test de puntos de conversión**: en `engine.py` la ampliación aparece únicamente dentro de `to_framework_input` y el
  *downcast* únicamente dentro de `from_framework_output` (AST).
- **Test de precisión**: ida y vuelta `Float32` → `float64` → `Float32` idéntica bit a bit; y resultados del motor frente
  al *golden* (referencia de prueba independiente, fuera de `src/`) dentro de la tolerancia del punto 5.
- **Test de contrato de esquema**: toda columna persistida es `Float32`, `Int64` o `Int8` (dominio documentado, Art. 5.1 bis).
  Un valor no finito o fuera de dominio produce `output_out_of_range`, nunca se trunca.

## Alternativas descartadas
- **Motores en `float32`**: imposible o inútil; las bibliotecas no tienen ruta `float32` y reimplementarlas elimina su
  valor como contraste.
- **Permitir `pandas`/`float64` en todo el paquete de backtesting**: ensancha la superficie de la excepción y mezcla
  precisiones en los contratos compartidos; viola el principio de excepción acotada del Art. 5.4.
- **Persistir resultados en `float64`**: rompe el Art. 5.1 y el esquema común entre motores (incluido el motor GPU).
- **Calcular las métricas dentro de cada motor**: cada biblioteca define Sharpe/anualización a su manera; los resultados
  no serían comparables entre motores.

## Impacto
Solo los módulos `engine.py` de los motores basados en `pandas`. Los consumidores de resultados (API, CLI, otros
paquetes) solo ven `Float32`/`Int64`/`Int8`. Requiere bump de la constitución a 1.2.0 por la enmienda del Art. 8.

## Aprobación
- [X] Aprobado por el Tech Lead — Constancia: aprobación expresa del Tech Lead en la sesión de trabajo (enmienda del Art. 8 y excepción del Art. 5.3/5.4) — Firma: Guillermo Martín Marco Fecha: 02/11/2026
