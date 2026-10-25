# Spec — `finances-api` (raíz de composición del servicio HTTP)

## Contexto
Cada herramienta `ft_*` expone un `APIRouter` de FastAPI, pero ninguna app los monta y las specs referían a un `finances-backtester serve` inexistente. `finances-api` es el único punto que compone todos los routers en un servicio. No es una herramienta `ft_*`: por eso puede importar de todas sin violar el Art. 4 (las herramientas siguen sin importarse entre sí).

## Historias de Usuario
- **US-1**: Como operador, quiero levantar un único servicio HTTP con `ft_serve` para consumir todas las herramientas desde un solo host/puerto.
- **US-2**: Como desarrollador, quiero ver todos los endpoints en un único `/docs` (OpenAPI).
- **US-3**: Como orquestador externo, quiero un endpoint de salud para saber si el servicio está vivo.

## Requisitos Funcionales (EARS)

### RF-1 — Composición de routers (Ubicuo)
**EL SISTEMA DEBE** exponer una fábrica `create_app() -> FastAPI` que incluya los routers de:
- `ft_setup` (prefijo `/setup`)
- `ft_market_data_binance` (prefijo `/market-data/binance`)
- `ft_data_viewer` (prefijo `/data`)

### RF-2 — Comando de servicio (Ubicuo)
**EL SISTEMA DEBE** proveer el comando CLI `ft_serve` con opciones `--host`, `--port` y `--log-level`.
- **SI** `--host`/`--port` no se indican, **ENTONCES** usar `FT_API_HOST` / `FT_API_PORT`.
- **SI** `uvicorn` no está instalado, **ENTONCES** terminar con exit 3 y un mensaje indicando cómo instalarlo.

### RF-3 — Logging de servicio (Ubicuo)
**CUANDO** el servicio arranca, **EL SISTEMA DEBE** configurar logging JSON mediante `finances_core.logging.configure_logging("api", ...)` en el `lifespan` de la app, de modo que también aplique cuando la app se monta desde tests u otro servidor ASGI.

### RF-4 — Salud (Ubicuo)
**EL SISTEMA DEBE** exponer `GET /health` que responda `200` con `{"status": "ok", "version": "<versión del paquete>", "parquet_dir_accessible": <bool>}`.
- `parquet_dir_accessible` es `true` **SI** `FT_PARQUET_DIR` existe y es legible (requisito de `ft_market_data_binance` T8.3); su valor `false` no cambia el código `200` (el servicio sigue vivo).

### RF-5 — Aislamiento (No deseado)
**SI** algún módulo `ft_*` importa de `finances_api`, **ENTONCES** es una violación del Art. 4. `finances_api` solo importa los `router` públicos de cada herramienta.
