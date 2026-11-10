# `vwap_reversion` — Reversión al VWAP rodante

Estrategia integrada (`origin = builtin`); largo/plano por defecto, largo/corto con
`allow_short=True` (Enmienda G3).

## Lógica de negocio

Estrategia de reversión a la media basada en el VWAP (Volume Weighted Average Price) de ventana rodante. Se considera que el activo está "barato" cuando el precio de cierre cae por debajo del VWAP en un porcentaje determinado por el `threshold`. Se entra en largo en este punto y se sale cuando el precio vuelve a cruzar al alza el VWAP.

## Matemática

- **VWAP**: $VWAP_t = \frac{\sum_{i=t-n+1}^{t} (TypicalPrice_i \cdot Volume_i)}{\sum_{i=t-n+1}^{t} Volume_i}$ donde $TypicalPrice = \frac{High+Low+Close}{3}$
- **Entrada**: $Close_t < VWAP_t \cdot (1 - threshold)$
- **Salida**: $Close_t > VWAP_t$
- **Con `allow_short=True`**: 4 condiciones genuinamente distintas, reflejo simétrico
  al otro lado del VWAP: $\text{enter\_short}[t] = Close_t > VWAP_t \cdot (1 + threshold)$
  y $\text{exit\_short}[t] = Close_t < VWAP_t$. Cada salida se evalúa **solo mientras
  se está en su propio lado** (`strategies.signals.position_from_long_short`).

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: Close < VWAP * (1 - threshold)
    Largo --> Plano: Close > VWAP
    Plano --> Corto: Close > VWAP * (1 + threshold), con allow_short=True
    Corto --> Plano: Close < VWAP, con allow_short=True
    Largo --> Plano: fin de datos (liquidación final)
    Corto --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `n` | `int` | [2, 10,000] | 20 | Ventana rodante para el cálculo del VWAP. |
| `threshold` | `float` | [0.0, 1.0] | 0.02 | Fracción por debajo del VWAP necesaria para entrar. |
| `allow_short` | `bool` | — | `false` | El precio por encima del VWAP (simétrico) abre corto. |

## Limitaciones

- En tendencias bajistas fuertes, el precio puede permanecer por debajo del VWAP durante mucho tiempo, generando pérdidas prolongadas.
- Depende críticamente de la calidad y precisión de los datos de volumen.
