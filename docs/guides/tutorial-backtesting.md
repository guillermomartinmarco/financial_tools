# De cero a tu primer backtest

> Guía paso a paso, para principiantes, del flujo completo de `financial_tools`:
> **descargar datos → crear una estrategia → correr un backtest** y leer los resultados.
>
> No hace falta saber programar: vas a crear **un solo archivo de texto** y a copiar y
> pegar comandos. Cada comando va acompañado de una explicación de qué hace y qué vas a ver.
> Todo lo que aparece con `<entre_angulos>` es un valor que te devuelve un comando anterior y
> que tenés que reemplazar por el tuyo.

---

## Qué vas a lograr

Al terminar vas a haber:

1. Preparado el entorno de trabajo.
2. Descargado velas reales de Bitcoin (BTCUSDT, 1 hora) desde Binance.
3. Registrado una estrategia de cruce de medias móviles (`sma_cross`).
4. Corrido un backtest sobre esos datos y leído sus métricas (rendimiento, operaciones,
   drawdown).
5. Repetido la corrida para ver la caché, y consultado las operaciones una a una.
6. (Opcional) Hecho lo mismo desde la API HTTP.

Tiempo estimado: 15–20 minutos.

---

## Antes de empezar

Necesitás el proyecto instalado en modo editable y su entorno de Python activado. Si todavía
no lo hiciste, seguí la sección **«Instalación en modo editable»** del
[`README.md`](../../README.md) de la raíz. En resumen, dentro del entorno de Python del
proyecto tenés que tener disponibles estos comandos: `ft_setup`, `ft_market_data_binance`,
`ft_data_viewer`, `ft_strategy_manager` y `ft_vectorbt`.

Para comprobar que están, pedí la ayuda de uno:

```bash
ft_vectorbt --help
```

Si ves la lista de comandos (`run`, `kinds`, `show`, …), estás listo. Si el comando «no se
encuentra», volvé al README: falta activar el entorno o instalar los paquetes.

> **Convención de este tutorial.** Los comandos se ejecutan desde la **raíz del proyecto**
> (la carpeta que contiene `README.md` y `packages/`).

---

## Paso 1 — Inicializar el entorno

La primera vez hay que crear las carpetas de trabajo y los archivos de configuración. Eso lo
hace `ft_setup`:

```bash
ft_setup init
```

Esto prepara los directorios de datos, el archivo de variables de entorno y la base de
tareas. Hacelo **una sola vez** por proyecto.

Para verificar que todo quedó bien configurado:

```bash
ft_setup doctor
```

Vas a ver una tabla con las variables `FT_*` (dónde se guardan los datos, los resultados,
etc.). Si todas aparecen en verde / accesibles, seguimos.

---

## Paso 2 — Descargar datos de mercado

Vamos a bajar el histórico de **BTCUSDT** en velas de **1 hora** desde el 1 de enero de 2024.
La descarga corre en segundo plano: el comando te devuelve un **identificador de tarea**
(`task_id`) y vos consultás el progreso.

```bash
ft_market_data_binance download --symbol BTCUSDT --timeframe 1h --start 2024-01-01
```

Anotá el `task_id` que imprime. Para ver cómo va:

```bash
ft_market_data_binance status <task_id>
```

Repetí ese comando cada tanto hasta que el estado sea `COMPLETED`. Cuando termine, mirá el
resultado final:

```bash
ft_market_data_binance result <task_id>
```

### Comprobar que los datos están

`ft_data_viewer` te deja inspeccionar lo descargado. El dataset quedó identificado como
`binance/spot/BTCUSDT/1h` (intercambio / tipo de mercado / símbolo / timeframe):

```bash
ft_data_viewer summary -e binance -m spot -s BTCUSDT -t 1h
```

Vas a ver cuántas velas hay y el rango de fechas. Para espiar las primeras filas:

```bash
ft_data_viewer head -e binance -m spot -s BTCUSDT -t 1h -n 20
```

> **¿Qué es una «vela»?** Cada fila es una barra de tiempo (acá, 1 hora) con el precio de
> apertura, el máximo, el mínimo y el cierre (OHLC) más el volumen. Es la materia prima del
> backtest.

---

## Paso 3 — Crear tu estrategia

Una **estrategia** decide cuándo comprar y cuándo vender. En `financial_tools` las estrategias
viven en el núcleo y se usan registrándolas con un **nombre** y sus **parámetros**. Vamos a
registrar una `sma_cross` (cruce de medias móviles): compra cuando la media rápida cruza
hacia arriba a la media lenta, y vende cuando la cruza hacia abajo.

### 3.1 — Creá el archivo de parámetros

Creá un archivo de texto llamado **`params.json`** en la raíz del proyecto, con este
contenido exacto:

```json
{
  "kind": "sma_cross",
  "params": { "fast": 20, "slow": 100 }
}
```

- `kind` es el tipo de estrategia (acá, el cruce de medias integrado).
- `fast` y `slow` son las ventanas de la media rápida (20 velas) y la lenta (100 velas).

> **¿Qué otras estrategias hay?** Para ver las disponibles con sus parámetros:
> ```bash
> ft_vectorbt kinds
> ```
> Las integradas son `sma_cross`, `rsi_reversion` y `donchian_breakout`.

### 3.2 — Registrá la estrategia

```bash
ft_strategy_manager create btcusdt_sma_cross --params params.json --desc "Mi primer cruce de medias"
```

El comando imprime un **`strategy_id`** (un identificador largo tipo
`01a1b50f-2767-757d-...`). **Anotalo**: lo vas a necesitar para el backtest.

Para confirmar que quedó registrada:

```bash
ft_strategy_manager list
```

> **¿Querés escribir tu propia estrategia en Python?** Es posible, pero es un paso más
> avanzado. Cuando quieras, seguí la guía dedicada:
> [`strategy-authoring.md`](strategy-authoring.md). Para este tutorial alcanza con la
> estrategia integrada que acabás de registrar.

---

## Paso 4 — Correr el backtest

Ahora simulás la estrategia sobre los datos. Usá el `strategy_id` del paso anterior:

```bash
ft_vectorbt run -d binance/spot/BTCUSDT/1h --strategy-id <strategy_id>
```

Cuando termine (unos segundos), vas a ver una tabla con el resumen. Lo importante:

- **`run_id`**: el identificador único de esta corrida (lo vas a usar para consultarla).
- **`n_trades`**: cuántas operaciones hizo la estrategia.
- **`total_return`**: el rendimiento total (por ejemplo, `2.82` = +282 %).
- **`max_drawdown`**: la peor caída desde un pico (por ejemplo, `-0.65` = −65 %).
- **`final_equity`**: con cuánto capital terminó (la caja inicial por defecto es 10 000).

> **Si querés la salida como JSON** (útil para guardarla o procesarla), agregá `--format json`:
> ```bash
> ft_vectorbt run -d binance/spot/BTCUSDT/1h --strategy-id <strategy_id> --format json
> ```

### 4.1 — Repetir: la caché

Volvé a correr **exactamente el mismo comando**. Esta vez el resumen va a decir
`cached = true` y va a responder al instante: el sistema reconoce que ya calculó ese
resultado y no lo vuelve a simular. Si querés **forzar** el recálculo:

```bash
ft_vectorbt run -d binance/spot/BTCUSDT/1h --strategy-id <strategy_id> --force
```

El resultado es idéntico al anterior (el backtest es determinista).

### 4.2 — Mirar los resultados guardados

Con el `run_id` que te dio el backtest podés revisar la corrida:

```bash
ft_vectorbt show <run_id>
```

Las operaciones, una por página:

```bash
ft_vectorbt trades <run_id> --limit 100
```

La curva de capital (equity) a lo largo del tiempo:

```bash
ft_vectorbt equity <run_id> --limit 100
```

Y el listado de todas tus corridas:

```bash
ft_vectorbt runs --strategy-id <strategy_id>
```

> **Ajustar costos y tamaño.** El comando `run` acepta, entre otros,
> `--initial-cash` (caja inicial), `--size-fraction` (fracción de la caja por operación),
> `--fees-bps` (comisión) y `--slippage-bps` (deslizamiento). Probá cambiarlos y volvé a
> correr para ver cómo afectan el resultado.

---

## Paso 5 (opcional) — Lo mismo desde la API

Todo lo que hiciste por consola también está disponible por HTTP. Levantá el servidor:

```bash
ft_serve --port 8000
```

En **otra terminal**, lanzá un backtest (reemplazá `<strategy_id>` por el tuyo). La API
responde al instante con un `task_id` y las URLs para consultar el progreso:

```bash
curl -X POST http://localhost:8000/backtest/vectorbt/run \
  -H "Content-Type: application/json" \
  -d '{"dataset":{"exchange":"binance","market_type":"spot","symbol":"BTCUSDT","timeframe":"1h"},"strategy_id":"<strategy_id>"}'
```

Consultá el estado y, cuando esté terminado, el resultado:

```bash
curl http://localhost:8000/backtest/vectorbt/status/<task_id>
curl http://localhost:8000/backtest/vectorbt/result/<task_id>
```

Las estrategias disponibles:

```bash
curl http://localhost:8000/backtest/vectorbt/kinds
```

> La documentación interactiva de toda la API está en `http://localhost:8000/docs`.

Para apagar el servidor, volvé a su terminal y presioná `Ctrl+C`.

---

## Resumen del flujo

```text
ft_setup init                         # 1. preparar el entorno (una vez)
ft_market_data_binance download ...   # 2. descargar velas
params.json  (lo creás vos)           # 3. definir la estrategia
ft_strategy_manager create ...        #    registrarla -> strategy_id
ft_vectorbt run -d ... --strategy-id  # 4. correr el backtest -> run_id
ft_vectorbt show / trades / equity    #    leer los resultados
```

## Qué leer después

- [`README.md`](../../README.md) — mapa completo de la suite y todos los comandos.
- [`strategy-authoring.md`](strategy-authoring.md) — cómo escribir tu propia estrategia en
  Python.
- Nota técnica: el motor `ft_vectorbt` simula con VectorBT y confina la aritmética de doble
  precisión a su frontera (RFC-0004); las señales siempre las calcula el núcleo, así que
  cualquier estrategia registrada funciona sin tocar el motor.

¡Listo! Ya recorriste el flujo completo: de los datos crudos a un backtest con métricas.
