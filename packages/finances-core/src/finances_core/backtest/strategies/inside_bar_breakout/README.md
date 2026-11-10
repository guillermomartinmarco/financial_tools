# `inside_bar_breakout` — Ruptura de barra interior (Inside Bar)

Estrategia integrada (`origin = builtin`); largo/plano por defecto, largo/corto con
`allow_short=True` (Enmienda G3).

## Lógica de negocio

Se basa en el patrón de acción de precio "Inside Bar". Una barra es considerada *inside bar* si su máximo es menor que el máximo de la barra anterior y su mínimo es mayor que el mínimo de la barra anterior.

La estrategia confirma un movimiento cuando la barra inmediatamente posterior a un *inside bar* rompe el rango de dicho *inside bar*:
- **Entrada**: El cierre de la barra actual es superior al máximo de la barra anterior, habiendo sido esta última un *inside bar*.
- **Salida**: El cierre de la barra actual es inferior al mínimo de la barra anterior, habiendo sido esta última un *inside bar*.

## Matemática

- **Inside Bar**: $Inside_t = (High_t < High_{t-1}) \land (Low_t > Low_{t-1})$
- **Entrada**: $Inside_{t-1} \land (Close_t > High_{t-1})$
- **Salida**: $Inside_{t-1} \land (Close_t < Low_{t-1})$
- **Con `allow_short=True`**: la ruptura a la baja abre corto en vez de solo aplanar;
  la ruptura al alza cierra el corto, con una barra en plano al revertir (Enmienda
  G2b/`position_from_long_short`).

## Diagrama

```mermaid
stateDiagram-v2
    [*] --> Plano
    Plano --> Largo: Breakout Alza Inside Bar
    Largo --> Plano: Breakout Baja Inside Bar
    Plano --> Corto: Breakout Baja Inside Bar, con allow_short=True
    Corto --> Plano: Breakout Alza Inside Bar, con allow_short=True
    Largo --> Plano: fin de datos (liquidación final)
    Corto --> Plano: fin de datos (liquidación final)
```

## Diccionario de parámetros

| Parámetro | Tipo | Rango | Defecto | Descripción |
|---|---|---|---|---|
| `allow_short` | booleano | — | `false` | La ruptura a la baja abre corto en vez de aplanar |

Sin `allow_short`, esta estrategia no tiene parámetros ajustables (patrón fijo de
accion de precio).

## Limitaciones

- **Memoria corta**: La señal solo se dispara si el breakout ocurre exactamente en la barra siguiente al *inside bar*. No se recuerda el rango del *inside bar* para breakouts que ocurran varias barras después.
- No considera la tendencia general del mercado.
