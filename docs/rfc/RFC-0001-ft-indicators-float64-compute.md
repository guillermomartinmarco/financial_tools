# RFC-0001 — Cálculo interno en `Float64` para `ft_indicators` (excepción Art. 5.3)

| Campo | Valor |
|---|---|
| Estado | **Propuesto** — pendiente de aprobación del Tech Lead |
| Fecha | 2026-10-25 |
| Artículo afectado | Art. 5.3 (Conversión y Validación) — **excepción**, no enmienda |
| Alcance | Solo `packages/finances-analytics/.../ft_indicators/kernels/` y `engine.py` |
| Referencias | `specs/ft_indicators/spec.md` (RF-17, Precisión), `specs/ft_indicators/plan.md` (§3.3, §3.10, ADR-I11) |

## Contexto
El Art. 5.1 fija `float32` para precios, volúmenes e **indicadores**. El Art. 5.3 acepta la pérdida de precisión del
*downcast* como *trade-off* deliberado y establece que "cualquier caso que requiera mayor precisión debe ser
explícitamente justificado en RFC y aislado en módulo dedicado". Este RFC es esa justificación.

## Motivación
1. **Rol de oráculo**: `ft_indicators` es la referencia contra la cual se validarán los futuros kernels de GPU
   (WGSL no tiene `f64`). Si el oráculo calculara en `f32`, sus valores tendrían el mismo tipo de error de redondeo
   que se quiere medir y no serviría como verdad de referencia.
2. **Deriva numérica**: sumas móviles y suavizados recursivos largos (EMA, Wilder en RSI/ATR/ADX, OBV) acumulan error
   en `f32` sobre series de millones de velas. Los experimentos WGSL del Tech Lead mostraron que RSI, Bollinger y MACD
   no pueden ser bit-exactos en `f32` (divisiones, raíces y cancelación).

## Propuesta
- Los kernels de `ft_indicators` convierten las entradas `Float32` a `Float64`, calculan en `Float64` y el motor
  realiza **un único `cast` a `Float32`** antes de persistir.
- `Float64` **no sale** de `kernels/` + `engine.py`, salvo hacia el paquete de *fixtures* del oráculo
  (`fixtures/oracle/v1`), que es material de test y no dato de mercado.
- **Todo lo persistido** en `FT_INDICATORS_DIR` y todo lo exportado a consumidores sigue siendo `Float32`/`int64`
  (Art. 5.1 intacto).

## Verificación (obligatoria para aceptar la excepción)
- Test de tipos/AST: ningún módulo fuera de `kernels/` y `engine.py` produce columnas `Float64`; lo persistido es `Float32`.
- Test de frontera: existe un único punto de `cast` a `Float32` en el motor.

## Alternativas descartadas
- **Calcular en `Float32`**: el oráculo perdería su valor de referencia frente a la GPU.
- **Persistir en `Float64`**: viola el Art. 5.1, duplica el almacenamiento y ningún consumidor lo necesita.

## Impacto
Sin cambios para otras herramientas ni para el formato de datos persistido. No requiere *bump* de versión de la
constitución (no la modifica; aplica la vía de excepción que el propio Art. 5.3 prevé).

## Aprobación
- [ ] Aprobado por el Tech Lead — Firma: ________________ Fecha: __________
