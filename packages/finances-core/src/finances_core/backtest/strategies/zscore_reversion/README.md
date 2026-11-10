# `zscore_reversion` — Reversión estadística por Z-Score

Estrategia integrada (`origin = builtin`); largo/plano por defecto, largo/corto con
`allow_short=True` (Enmienda G3).

## Lógica de negocio

La estrategia utiliza el Z-Score para medir cuántas desviaciones estándar se encuentra el precio actual respecto a su media móvil. Una lectura muy negativa indica que el precio está significativamente por debajo de su promedio histórico reciente (sobreventa), lo que sugiere una probabilidad de reversión al alza. La posición se cierra cuando el Z-Score vuelve a ser positivo (el precio cruza la media).

## Matemática

- **Z-Score**: $z = \frac{\text{close} - \text{SMA}(\text{close}, n)}{\text{std}(\text{close}, n)}$
- **Señales**:
    - $\text{entry}[t] = z[t] < -\text{threshold}$
    - $\text{exit}[t] = z[t] > 0$
- **Con `allow_short=True`**: 4 condiciones genuinamente distintas (no el reflejo de
  las anteriores), ya que el Z-Score es simétrico por construcción:
  $\text{enter\_short}[t] = z[t] > \text{threshold}$ (sobrecompra) y
  $\text{exit\_short}[t] = z[t] < 0$. Cada salida se evalúa **solo mientras se está en
  su propio lado** (`strategies.signals.position_from_long_short`).

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: entry (z < -threshold, sobreventa)
    Largo --> Plano: exit (z > 0, regreso a la media)
    Plano --> Corto: enter_short, con allow_short=True (z > threshold, sobrecompra)
    Corto --> Plano: exit_short, con allow_short=True (z < 0, regreso a la media)
    Largo --> Plano: fin de datos (liquidación final)
    Corto --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
|---|---|---|---|---|
| `n` | entero | 2..10000 | 20 | Periodo de la ventana móvil para media y desviación |
| `threshold` | flotante | 0.0..10.0 | 2.0 | Número de desviaciones estándar para disparar la entrada |
| `allow_short` | booleano | — | `false` | El z-score simétrico positivo abre corto en vez de aplanar |

## Limitaciones

En mercados con alta volatilidad o saltos bruscos (gaps), el Z-Score puede tardar en normalizarse, manteniendo la posición más tiempo de lo deseado.
