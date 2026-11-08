# Optimizá los parámetros de tu estrategia

> Guía paso a paso, para principiantes, de cómo **buscar automáticamente** los mejores
> parámetros de una estrategia con `ft_vectorbt optimize`: en vez de probar `fast` y `slow`
> a mano, dejás que el optimizador pruebe muchas combinaciones y te diga cuál anduvo mejor
> **sin hacer trampa con el pasado**.
>
> Es la continuación natural de [«De cero a tu primer backtest»](tutorial-backtesting.md).
> Si todavía no lo hiciste, hacelo primero: acá damos por hecho que ya tenés el entorno
> listo y las velas de **BTCUSDT 1h** descargadas (dataset `binance/spot/BTCUSDT/1h`).
>
> Como siempre: no hace falta programar. Vas a crear **un archivo de texto** y copiar y
> pegar comandos. Lo que aparece con `<entre_angulos>` es un valor que te devolvió un
> comando anterior y que tenés que reemplazar por el tuyo.

---

## Qué vas a lograr

Al terminar vas a haber:

1. Entendido qué es **optimizar parámetros** y por qué es tan fácil engañarse (sobreajuste).
2. Entendido el **walk-forward anclado**: la técnica que hace que el resultado sea honesto.
3. Escrito un **espacio de búsqueda** (`sweep.json`) con varios parámetros a la vez.
4. Corrido `ft_vectorbt optimize` y leído su salida (mejores parámetros, score, folds).
5. Tomado los mejores parámetros y corrido un backtest normal para **cerrar el círculo**.

Tiempo estimado: 15–20 minutos.

---

## Antes de empezar

Necesitás haber completado el [primer tutorial](tutorial-backtesting.md), o al menos tener:

- El entorno instalado y activado (los comandos `ft_vectorbt`, `ft_strategy_manager`, … disponibles).
- El dataset `binance/spot/BTCUSDT/1h` descargado. Para comprobarlo:

```bash
ft_data_viewer summary -e binance -m spot -s BTCUSDT -t 1h
```

Si ves la cantidad de velas y el rango de fechas, estás listo. Si no, volvé al primer
tutorial, **Paso 2**.

> **Convención de este tutorial.** Los comandos se ejecutan desde la **raíz del proyecto**
> (la carpeta que contiene `README.md` y `packages/`).

---

## La idea: optimizar sin engañarse

En el primer tutorial elegiste `fast = 20` y `slow = 100` **a dedo**. Pero ¿y si `fast = 15`
y `slow = 120` hubieran andado mejor? Probar combinaciones a mano es lento y, peor todavía,
**peligroso**: si probás mil combinaciones sobre los mismos datos y te quedás con la que dio
el mejor número, casi seguro encontraste una que funcionó **de pura casualidad** sobre ese
pasado puntual. Eso se llama **sobreajuste** (*overfitting*): la estrategia «memoriza» el
pasado y después fracasa con datos nuevos.

`ft_vectorbt optimize` ataca las dos cosas:

- **Busca por vos** con [Optuna](https://optuna.org) (un optimizador inteligente que
  aprende de los intentos anteriores en vez de probar al azar).
- **No te deja engañarte**, porque puntúa cada combinación con **walk-forward anclado**.

### Walk-forward anclado, en criollo

Imaginá tu histórico partido en tramos cronológicos. Para cada **fold** (tramo de prueba):

- El optimizador **entrena/mira** desde el principio del histórico hasta cierto punto
  (la parte *in-sample*, «dentro de muestra»). «Anclado» quiere decir que esa parte
  **siempre arranca al inicio** y va creciendo.
- Y después **puntúa** la combinación en el tramo **siguiente, que nunca vio** (la parte
  *out-of-sample* u **OOS**, «fuera de muestra»).

El número que te muestra el optimizador es el **score OOS** (promedio de los folds). Es
decir: **te dice qué tan bien anduvo la estrategia en datos que no usó para elegir los
parámetros.** Eso es lo que querés: un resultado que tenga chance de repetirse en el futuro,
no un truco de magia sobre el pasado.

> **Tres perillas clave** (las vas a ver como opciones del comando):
> - `--folds`: en cuántos tramos OOS se corta el histórico.
> - `--in-sample-ratio`: qué proporción se usa para mirar vs. puntuar.
> - `--fold-agg`: cómo se combinan los folds (`mean` promedio, `min` el peor, `median` la mediana).
>   Usar `min` es más **conservador**: premia estrategias que andan bien en *todos* los tramos.

---

## Paso 1 — Escribir el espacio de búsqueda (`sweep.json`)

El **sweep** describe qué parámetros variar y en qué rango. Creá un archivo de texto llamado
**`sweep.json`** en la raíz del proyecto, con este contenido exacto:

```json
{
  "axes": [
    { "param": "fast", "type": "int", "low": 5,  "high": 40 },
    { "param": "slow", "type": "int", "low": 50, "high": 200, "step": 10 }
  ]
}
```

Qué dice esto:

- **`axes`** es la lista de parámetros (ejes) a optimizar. Acá optimizamos **dos a la vez**:
  `fast` y `slow`.
- Cada eje tiene un **`param`** (el nombre exacto del parámetro de la estrategia), un
  **`type`** y su rango:
  - `type: "int"` → números enteros, con `low` y `high` (incluidos).
  - `step` (opcional) → el salto entre valores. Acá `slow` va de 10 en 10 (50, 60, 70, …).
- También existen `type: "float"` (con `low`/`high` y, si querés, `step` o `log: true` para
  escala logarítmica) y `type: "categorical"` (con una lista `choices`, por ejemplo
  `{ "param": "modo", "type": "categorical", "choices": ["a", "b"] }`).

> **¿Por qué `fast` 5–40 y `slow` 50–200?** `sma_cross` exige `fast < slow`. Elegimos rangos
> que **no se pisan** para que toda combinación sea válida. Si los rangos se solaparan, las
> combinaciones imposibles (`fast >= slow`) simplemente se **descartan** sin cortar la
> búsqueda (ver el Paso 3).

---

## Paso 2 — Correr el optimizador

Vamos a optimizar la estrategia integrada `sma_cross` sobre el dataset de Bitcoin:

```bash
ft_vectorbt optimize -d binance/spot/BTCUSDT/1h --kind sma_cross --sweep sweep.json
```

> Usamos `--kind sma_cross` (la estrategia integrada, que aporta los valores por defecto como
> base). Si preferís optimizar una estrategia **que ya registraste**, usá `--strategy-id <id>`
> en vez de `--kind`. Tenés que indicar **exactamente uno** de los dos.

La primera vez tarda un poco más (Optuna y el motor se «calientan»). Vas a ver una tabla con
el resultado. Lo importante:

- **`objective`**: la métrica que se optimizó y su dirección. Por defecto es
  **`calmar (max)`**: el ratio de Calmar (rendimiento anual sobre la peor caída); cuanto más
  alto, mejor. Equilibra ganancia y riesgo, por eso es un buen objetivo por defecto.
- **`best_params`**: 🎯 **los mejores parámetros encontrados** (por ejemplo, `{"fast": 15, "slow": 120}`).
- **`best_score`**: el score **OOS** de esos parámetros (el número honesto del que hablamos).
- **`n_trials`** / **`n_pruned`**: cuántas combinaciones probó y cuántas se **descartaron**
  (inválidas o sin suficientes operaciones).
- **`seed`**: la semilla de aleatoriedad.
- Una tabla de **folds** con el score *in-sample* y *oos* de cada tramo para los mejores
  parámetros, para que veas si fue parejo o si hubo un tramo flojo.

---

## Paso 3 — Entender lo que pasó (y ajustar)

### La semilla: resultados reproducibles

Volvé a correr **el mismo comando**. Vas a obtener **exactamente los mismos**
`best_params` y `best_score`. Eso es por `--seed` (42 por defecto): la búsqueda es
**determinista**. Cambiá la semilla y explorás otro camino:

```bash
ft_vectorbt optimize -d binance/spot/BTCUSDT/1h --kind sma_cross --sweep sweep.json --seed 7
```

### Más intentos, más folds

Por defecto hace **50 intentos** (`--n-trials`) y **4 folds** (`--folds`). Más intentos
exploran mejor el espacio; más folds dan un score OOS más robusto (pero cada uno necesita
suficientes velas). Probá:

```bash
ft_vectorbt optimize -d binance/spot/BTCUSDT/1h --kind sma_cross --sweep sweep.json \
  --n-trials 100 --folds 5 --in-sample-ratio 0.7 --fold-agg min
```

> Con `--fold-agg min` el score es el del **peor** fold: así preferís estrategias que andan
> bien de forma **consistente**, no las que brillan en un tramo y se hunden en otro.

### La restricción de operaciones mínimas

`--min-trades` (10 por defecto) **descarta** las combinaciones que hacen muy pocas operaciones
en el histórico completo. Una estrategia que hizo 1 sola operación y «ganó» no probó nada:
fue suerte. Si ves muchos `n_pruned`, puede ser esta restricción (o combinaciones inválidas).

### Combinaciones inválidas y trials descartados

Si una combinación es inválida (por ejemplo `fast >= slow` en `sma_cross`) o falla, el
optimizador la **marca como descartada y sigue**: nunca corta la búsqueda por un intento
malo. Por eso podés definir rangos amplios sin miedo.

> **Nota:** `optimize` **no guarda** cada intento en disco (a diferencia de `run`, que
> persiste cada corrida). La búsqueda ocurre **en memoria**; lo que te importa es el ganador.

---

## Paso 4 — Cerrar el círculo: backtest con los mejores parámetros

El optimizador te dice **cuáles** son los mejores parámetros, pero no guarda una corrida. Para
quedártelos y analizarlos como cualquier backtest, registrá una estrategia con esos valores y
corré un `run` normal (esto es lo mismo que hiciste en el primer tutorial).

Creá un **`params_opt.json`** con los `best_params` que te dio el Paso 2 (reemplazá por los
tuyos):

```json
{
  "kind": "sma_cross",
  "params": { "fast": 15, "slow": 120 }
}
```

Registrá la estrategia optimizada y corré el backtest:

```bash
ft_strategy_manager create btcusdt_sma_opt --params params_opt.json --desc "sma_cross optimizada"
ft_vectorbt run -d binance/spot/BTCUSDT/1h --strategy-id <strategy_id>
```

Ahora podés comparar sus métricas con las de tu estrategia original (`fast 20 / slow 100`) y
revisar sus operaciones y su curva de capital con `ft_vectorbt show / trades / equity`, igual
que en el primer tutorial.

> **Ojo con la lectura.** El `total_return` de este `run` es sobre **todo** el histórico; el
> `best_score` del optimizador es el score **OOS** (más conservador y más honesto). No son el
> mismo número ni deberían serlo: el segundo es el que mejor anticipa el futuro.

---

## Resumen del flujo

```text
sweep.json  (lo creás vos)                     # 1. qué parámetros variar y en qué rango
ft_vectorbt optimize -d ... --kind ... --sweep #    2. buscar -> best_params + best_score (OOS)
params_opt.json  (con los best_params)         # 3. fijar el ganador
ft_strategy_manager create ... --params ...    #    registrarlo -> strategy_id
ft_vectorbt run -d ... --strategy-id           # 4. backtest normal para analizarlo a fondo
```

Opciones útiles de `optimize` (todas tienen valor por defecto):

```text
--objective        métrica a maximizar/minimizar del catálogo (def: calmar)
--n-trials         cantidad de intentos de Optuna (def: 50)
--folds            tramos del walk-forward anclado (def: 4)
--in-sample-ratio  proporción de datos para mirar vs. puntuar (def: 0.75)
--fold-agg         mean | min | median  (cómo combinar los folds, def: mean)
--min-trades       operaciones mínimas en el histórico (restricción, def: 10)
--seed             semilla -> reproducibilidad (def: 42)
--initial-cash --size-fraction --fees-bps --slippage-bps   caja y costos (como en run)
--start --end                                             acotar el rango temporal (ISO 8601, ms epoch o 'now')
```

## Qué leer después

- [«De cero a tu primer backtest»](tutorial-backtesting.md) — el flujo base, si te salteaste algo.
- [`README.md`](../../README.md) — mapa completo de la suite; la sección de `ft_vectorbt`
  tiene la referencia del subcomando `optimize` y el esquema del `sweep.json`.
- [`strategy-authoring.md`](strategy-authoring.md) — cómo escribir tu propia estrategia en
  Python (y después optimizarla con esta misma guía).

¡Listo! Ya sabés buscar buenos parámetros **sin engañarte con el pasado**: el paso que separa
un backtest de juguete de uno en el que se puede confiar un poco más.
