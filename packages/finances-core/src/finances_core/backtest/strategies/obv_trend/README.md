# `obv_trend` — Cruce del OBV sobre su media móvil

Estrategia integrada (`origin = builtin`); largo/plano por defecto, largo/corto con
`allow_short=True` (Enmienda G3).

## Lógica de negocio

Utiliza el On-Balance Volume (OBV) para confirmar la fuerza de la tendencia mediante el flujo de volumen. Se entra en largo cuando el OBV cruza al alza su media móvil simple (SMA), lo que indica que el volumen está respaldando el movimiento alcista. Se sale de la posición cuando el OBV cruza a la baja su media móvil.

## Matemática

- **OBV**: $OBV_t = OBV_{t-1} + \text{sign}(Close_t - Close_{t-1}) \cdot Volume_t$
- **OBV Media**: $OBV\_Avg_t = \text{SMA}(OBV, n)_t$
- **Entrada**: $\text{cross\_above}(OBV, OBV\_Avg)$
- **Salida**: $\text{cross\_below}(OBV, OBV\_Avg)$
- **Con `allow_short=True`**: el cruce a la baja abre corto en vez de solo aplanar; el
  cruce al alza cierra el corto, con una barra en plano al revertir (Enmienda
  G2b/`position_from_long_short`).

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: OBV cruza al alza SMA(OBV)
    Largo --> Plano: OBV cruza a la baja SMA(OBV)
    Plano --> Corto: OBV cruza a la baja, con allow_short=True
    Corto --> Plano: OBV cruza al alza, con allow_short=True
    Largo --> Plano: fin de datos (liquidación final)
    Corto --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `n` | `int` | [2, 10,000] | 20 | Periodo de la media móvil simple aplicada al OBV. |
| `allow_short` | `bool` | — | `false` | El cruce a la baja abre corto en vez de aplanar. |

## Limitaciones

- El OBV es un indicador acumulativo; su valor absoluto no es significativo, solo su relación con la media.
- Puede generar señales tardías en cambios bruscos de tendencia.
