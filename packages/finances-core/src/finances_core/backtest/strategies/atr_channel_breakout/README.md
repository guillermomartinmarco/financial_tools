# `atr_channel_breakout` — Ruptura de Canal ATR

Estrategia integrada (`origin = builtin`), solo largo/plano.

## Lógica de negocio

Esta estrategia implementa una ruptura de volatilidad basada en el Average True Range (ATR). Se entra en largo cuando el precio de cierre de la barra actual supera el precio de apertura más un múltiplo del ATR, lo que indica un movimiento alcista fuerte y significativo en relación con la volatilidad reciente. La posición se cierra cuando ocurre un movimiento bajista simétrico.

## Matemática

- **Volatilidad**: $\text{vol} = k \cdot \text{ATR}(\text{high}, \text{low}, \text{close}, n)$
- **Señales**:
    - $\text{entry}[t] = \text{close}[t] > \text{open}[t] + \text{vol}[t]$
    - $\text{exit}[t] = \text{close}[t] < \text{open}[t] - \text{vol}[t]$

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: entry (close supera open + k*ATR)
    Largo --> Plano: exit (close cae por debajo de open - k*ATR)
    Largo --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
|---|---|---|---|---|
| `n` | entero | 2..10000 | 14 | Periodo del ATR para medir la volatilidad |
| `k` | flotante | 0.1..10.0 | 1.0 | Multiplicador del ATR para el umbral de ruptura |

## Limitaciones

Al depender del precio de apertura de cada barra, es muy sensible a gaps de apertura y a la volatilidad intrabarra.
