# Especificación base de la suite `financial_tools`

## Estructura de Directorios de la Suite

```text
financial_tools/
├── AUTHORS.md       # Autores del proyecto
├── COPYING.md       # Licencia GPLv3
├── README.md        # Descripción general de la suite y dependencias
├── pyproject.toml   # Administra las dependencias de Python globales (vectorbt, backtrader, etc.)
├── ChangeLog        # Historial de cambios detallado
├── INSTALL.md       # Instrucciones genéricas de instalación
├── CMakeLists.txt   # El "orquestador": Busca CUDA/PyBind11 y añade las subcarpetas de src/
│
├── .venv/           # Un único entorno virtual de python para todo el proyecto (oculto/ignorado en .gitignore)
│
├── bin/                        # Destino final de los ejecutables o wrappers autogenerados
│   ├── ft_setup -> ../src/ft_setup/main.py (enlace simbólico o wrapper generado por pyproject.toml)
│   ├── ft_backtestopt_spectre  # Binario compilado directamente por CMake
│   └── ...
├── build/           # Directorio temporal para las compilaciones de CMake y NVCC
│
├── etc/             # Archivos de configuración global (.conf, .json, .yaml)
│   ├── main.conf    # Configuración de API keys de Binance, rutas de datos, etc.
│   └── strategies/  # Parámetros por defecto de las estrategias
│
├── src/                   # CÓDIGO FUENTE (Organizado por UTILIDAD)
│   │
│   ├── common/            # Lógica COMPARTIDA inter-utilidades
│   │   ├── python/        # Utilidades comunes de Python (ej. manejo de base de datos)
│   │   └── cpp/           # Estructuras de datos comunes en C++ / Cabeceras .hpp
│   │
│   # 1. Utilidades de Configuración y Datos
│   ├── ft_setup/
│   │   └── main.py        # Script Python de inicialización del entorno
│   ├── ft_market_data_binance/
│   │   └── main.py        # Descargador API Binance
│   ├── ft_market_data_yfinance/
│   │   └── main.py        # Descargador Yahoo Finance
│   ├── ft_market_data_manager/
│   │   └── main.py        # Gestión de archivos locales (SQLite/Parquet)
│   │
│   # 2. Utilidades de Estrategias y Backtesting Tradicional
│   ├── ft_strategy_manager/
│   │   └── main.py        # CRUD y gestión de estrategias del usuario
│   ├── ft_vectorbt/
│   │   └── main.py        # Orquestador que importa e invoca vectorbt
│   ├── ft_backtesting_py/
│   │   └── main.py
│   ├── ft_backtrader/
│   │   └── main.py
│   ├── ft_bt/
│   │   └── main.py
│   ├── ft_ffn/
│   │   └── main.py
│   │
│   # 3. La utilidad Híbrida/C++ (ft_backtestopt_spectre)
│   ├── ft_backtestopt_spectre/
│   │   ├── CMakeLists.txt # Compila este componente específico
│   │   ├── main.cpp       # Punto de entrada si se ejecuta standalone en C++
│   │   ├── hpp/           # Cabeceras locales C++ / CUDA
│   │   ├── cu/            # Kernels de CUDA (.cu)
│   │   └── bindings.cpp   # Código PyBind11 para exponer los kernels a Python si fuera necesario
│   │
│   # 4. Utilidades de Ejecución (Bots)
│   ├── ft_bot_paper_binance/
│   │   └── main.py        # Bot en modo simulación
│   ├── ft_live_bot/
│   │   └── main.py        # Bot en modo real
│   │
|   # 5. Server / Daemon
│   └── ft_api_service/            Tool que actúa como Server / Daemon
│       ├── main.py                # Punto de entrada CLI para levantar el servidor Uvicorn
│       ├── app.py                 # Instancia de FastAPI() y middleware
│       └── routers/               # Mapeo modular hacia el resto de las utilidades
│           ├── market_data.py     # Llama a ft_market_data_binance, yfinance, etc.
│           ├── backtest.py        # Llama a ft_vectorbt, ft_backtrader, y Spectre (C++)
│           └── bots.py            # Controla el ciclo de vida de ft_live_bot
│
├── doc/             # Documentación (Manuales en Texinfo, man pages, Markdown)
│   └── man/         # Páginas de manual (man1 para ejecutables)
│
├── lib/             # Bibliotecas internas compartidas u objetos binarios (.so, .a)
├── include/         # Archivos de cabecera (.h, .hpp) públicos de la suite
├── share/           # # Destino de datos históricos descargados localmente (bases de datos SQLite, Parquet, CSV, etcétera)
└── tests/           # Pruebas indexadas por utilidad (ej. test_ft_vectorbt.py)
```

## Funcionamiento dual consola/servicio

Para lograr que la suite `financial_tools` funcione de manera dual (como utilidades efímeras de consola y como un servicio web unificado con **FastAPI**), no vamos a fragmentar el código ni duplicar la lógica de FastAPI en cada tool. La clave está en diseñar un patrón de orquestación centralizado explotando la estructura modular de directorios planteada.

A continuación, se presenta la arquitectura recomendada, el diseño del orquestador y cómo mapear las tools.

### 1. Enfoque Arquitectónico: Desacoplamiento por "Controlador"

Para evitar que FastAPI dependa de scripts que imprimen en consola o que los scripts de consola dependan de un servidor web, se aplicará una separación de responsabilidades en cada subcarpeta de `src/ft_*`:

1. **Lógica de Negocio (`core.py` o funciones exportables):** El archivo `main.py` de cada utilidad no debe ejecutar código directamente al ser importado. Debe exponer funciones o clases limpias (ej. `run_backtest(params)`, `download_data(ticker)`).

2. **Interfaz de CLI (`main.py` o `__main__.py`):** El punto de entrada efímero. Usa una biblioteca como `argparse`, `click` o `typer` para parsear argumentos de la terminal e invocar la lógica de negocio.

3. **Interfaz de API (`FastAPI`):** Un servicio centralizado que importa las funciones de la lógica de negocio de cada utilidad y las expone en _endpoints_ HTTP.

### 2. Dónde integrar FastAPI en la estructura

Para mantener el estándar GNU/Linux y la limpieza del árbol, se agrega la utilidad del servicio web dentro de `src/` como una utilidad de ejecución más:

```text
src/
├── ft_api_service/            # Utilidad que actúa como Server / Daemon
│   ├── main.py                # Punto de entrada CLI para levantar el servidor Uvicorn
│   ├── app.py                 # Instancia de FastAPI() y middleware
│   └── routers/               # Mapeo modular hacia el resto de las utilidades
│       ├── market_data.py     # Llama a ft_market_data_binance, yfinance, etc.
│       ├── backtest.py        # Llama a ft_vectorbt, ft_backtrader, y Spectre (C++)
│       └── bots.py            # Controla el ciclo de vida de ft_live_bot
```

### 3. Ejemplo de Implementación Práctica del Patrón

#### Paso A: Preparar una utilidad efímera (Ej: `ft_market_data_binance`)

Modificar el archivo para que sea tanto importable como ejecutable.

```python
# src/financial_tools/src/ft_market_data_binance/main.py
import argparse

def download_binance_candles(ticker: str, timeframe: str, limit: int):
    # --- LÓGICA DE NEGOCIO REAL AQUÍ ---
    # Esto es lo que llamará tanto la CLI como FastAPI
    return {"status": "success", "data": f"Historial de {ticker} en {timeframe}"}

def main():
    # --- EJECUCIÓN EFÍMERA (CLI) ---
    parser = argparse.ArgumentParser(description="Descargador efímero de Binance")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--timeframe", default="1h")
    args = parser.parse_args()
    
    print(f"Ejecutando modo efímero para {args.ticker}...")
    resultado = download_binance_candles(args.ticker, args.timeframe, 100)
    print(resultado)

if __name__ == "__main__":
    main()
```

#### Paso B: Crear el Router en FastAPI (`ft_api_service`)

FastAPI simplemente importará la lógica de negocio como si fuera una biblioteca interna. Gracias al uso de un único `.venv` en la raíz, Python resolverá las rutas perfectamente si se agrega el _path_ raíz o se instala la suite en modo editable (`pip install -e .`).

```python
# src/financial_tools/src/ft_api_service/routers/market_data.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Importación directa desde tus otras utilidades hermanas
from ft_market_data_binance.main import download_binance_candles

router = APIRouter(prefix="/market-data", tags=["Data"])

class DownloadRequest(BaseModel):
    ticker: str
    timeframe: str = "1h"

@router.post("/binance/download")
async def api_download_binance(payload: DownloadRequest):
    try:
        # Ejecución estilo servicio (FastAPI bloquea o corre en threadpool)
        data = download_binance_candles(
            ticker=payload.ticker, 
            timeframe=payload.timeframe, 
            limit=100
        )
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### 4. Integración Crítica: ¿Qué pasa con C++ / CUDA (`ft_backtestopt_spectre`)?

Tenemos dos formas sumamente eficientes de exponer los binarios de alto rendimiento de C++ a la dualidad CLI/Servicio:

1. **A través de `PyBind11` (Altamente recomendado para FastAPI):**

   El archivo `bindings.cpp` compila un objeto compartido (`.so` o `.pyd`) en el directorio `lib/`. Al levantar FastAPI, se puede importar directamente el binario compilado:
   
   ```python
   import spectre_cuda_backend  # El módulo generado por PyBind11
   # ... dentro del endpoint de FastAPI:
   results = spectre_cuda_backend.run_backtest(matrix_params)
   ```
2. **A través de Subprocesos Asíncronos (Modo Aislado / No Bloqueante):**

   Si la simulación en CUDA tarda minutos o consume demasiada VRAM y puede que congele el loop de FastAPI, el servicio web puede invocar al ejecutable compilado en `bin/ft_backtestopt_spectre` usando tareas en segundo plano (_BackgroundTasks_ de FastAPI) o Celery/Redis:
   
   ```python
   import asyncio
   @router.post("/backtest/spectre")
   async def run_spectre_service(config_path: str):
       # Lanza el binario compilado por CMake de forma asíncrona
       process = await asyncio.create_subprocess_exec(
           "./bin/ft_backtestopt_spectre", "--config", config_path,
           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
       )
       return {"status": "dispatched", "pid": process.pid}
   ```

### 5. Configuración de `pyproject.toml` para la distribución

Para que el sistema operativo reconozca los comandos efímeros globalmente (enlaces dentro de `bin/`), configurar los `project.scripts` en el `pyproject.toml`. Así, al ejecutar `pip install .` se generarán los accesos automáticos:

```toml
[project.scripts]
ft_setup = "ft_setup.main:main"
ft_fetch_binance = "ft_market_data_binance.main:main"
ft_api_server = "ft_api_service.main:main" # Levanta FastAPI vía uvicorn
```

## Procesar tareas pesadas de forma no bloqueante

Para procesar tareas pesadas de forma no bloqueante sin añadir la complejidad de herramientas externas como Celery o Redis, la mejor opción nativa en FastAPI es utilizar **`BackgroundTasks`** combinado con un **almacenamiento de estado en memoria** (o una base de datos embebida como SQLite, aprovechando que ya la tienes en tu estructura).

Dado que tus utilidades de Python, C++ y CUDA son altamente consumidoras de recursos, el servidor web recibirá la petición, registrará la tarea, lanzará el proceso en un hilo o subproceso independiente y responderá **inmediatamente** con un `task_id`. El cliente luego usará ese ID para consultar el progreso en un endpoint de _polling_.

A continuación, te presento el diseño completo de esta arquitectura adaptada a tu suite corporativa.

### 1. El Administrador de Estados Centralizado (`common/python/`)

Para que tus utilidades puedan reportar su progreso y FastAPI pueda leerlo, necesitas un gestor de estados. Crearemos un singleton simple en memoria dentro de tu lógica compartida.

```python
# src/financial_tools/src/common/python/task_manager.py
import uuid
from typing import Dict, Any

class TaskManager:
    def __init__(self):
        # En producción avanzada, esto podría ser una tabla en tu SQLite local
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create_task(self, name: str) -> str:
        task_id = str(uuid.uuid4())
        self._tasks[task_id] = {
            "name": name,
            "status": "pending",
            "progress": 0.0,
            "result": None,
            "error": None
        }
        return task_id

    def update_task(self, task_id: str, status: str, progress: float, result: Any = None, error: str = None):
        if task_id in self._tasks:
            self._tasks[task_id].update({
                "status": status,
                "progress": progress,
                "result": result,
                "error": error
            })

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._tasks.get(task_id, {"status": "not_found"})

# Instancia única compartida
task_manager = TaskManager()
```

### 2. Adaptando una Utilidad Pesada (Ej: `ft_vectorbt`)

Para que la herramienta reporte su avance, su lógica de negocio debe aceptar opcionalmente el task_id. Si se ejecuta de forma efímera en CLI, simplemente imprime en pantalla; si viene de FastAPI, actualiza el `task_manager`.

```python
# src/financial_tools/src/ft_vectorbt/main.py
import time
from common.python.task_manager import task_manager

def run_heavy_backtest(strategy_id: str, pairs: list, task_id: str = None):
    """Lógica de simulación pesada (vectorbt)"""
    try:
        if task_id:
            task_manager.update_task(task_id, "running", 10.0)

        # --- Simulación del pipeline de procesamiento ---
        total_steps = len(pairs)
        for index, pair in enumerate(pairs):
            # Aquí vendría tu carga real de vectorbt/backtrader
            time.sleep(2) 
            
            # Calcular progreso dinámico
            progreso = ((index + 1) / total_steps) * 100
            if task_id:
                task_manager.update_task(task_id, "running", round(progreso, 2))
            else:
                print(f"[CLI] Procesando {pair}: {progreso}%")

        # Guardar resultado final
        final_metrics = {"sharpe": 2.1, "max_drawdown": -0.12, "status": "completed"}
        
        if task_id:
            task_manager.update_task(task_id, "completed", 100.0, result=final_metrics)
        return final_metrics

    except Exception as e:
        if task_id:
            task_manager.update_task(task_id, "failed", 100.0, error=str(e))
        raise e
```

### 3. El Endpoints en FastAPI (`ft_api_service`)

El router de FastAPI expondrá dos rutas clave: **Disparar (`/run`)** y **Consultar (`/status/{task_id}`)**.

Usaremos **`BackgroundTasks`** de FastAPI, que ejecuta la función en un _threadpool_ dedicado para no bloquear el bucle de eventos asíncrono del servidor web.

```python
# src/financial_tools/src/ft_api_service/routers/backtest.py
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from common.python.task_manager import task_manager

# Importamos la lógica de la utilidad hermana
from ft_vectorbt.main import run_heavy_backtest

router = APIRouter(prefix="/backtest", tags=["Backtesting"])

class BacktestRequest(BaseModel):
    strategy_id: str
    pairs: list[str]

@router.post("/vectorbt/run")
async def start_vectorbt_backtest(payload: BacktestRequest, background_tasks: BackgroundTasks):
    # 1. Crear el registro de la tarea e ID único
    task_id = task_manager.create_task(name=f"VectorBT-{payload.strategy_id}")
    
    # 2. Delegar al threadpool de FastAPI de inmediato
    background_tasks.add_task(
        run_heavy_backtest, 
        strategy_id=payload.strategy_id, 
        pairs=payload.pairs, 
        task_id=task_id
    )
    
    # 3. Responder de inmediato al cliente con el ID de rastreo
    return {
        "status": "accepted",
        "task_id": task_id,
        "poll_url": f"/backtest/status/{task_id}"
    }

@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str):
    # Consultar el estado actual en el almacén de datos común
    task_info = task_manager.get_task(task_id)
    
    if task_info["status"] == "not_found":
        raise HTTPException(status_code=404, detail="La tarea especificada no existe.")
        
    return task_info
```

### 4. ¿Cómo escalar esto para C++ y CUDA (`ft_backtestopt_spectre`)?

Si vas a lanzar tu ejecutable binario nativo compílado por CMake, `BackgroundTasks` sigue funcionando, pero dentro de la lógica de negocio utilizarás la biblioteca **`subprocess`** de Python.

Para reportar el progreso desde un binario de C++, la forma más limpia es hacer que tu programa en C++ imprima el progreso en formato JSON o texto plano por `stdout` (ej: `{"progress": 45.2}`), y que el hilo de Python lea esa salida línea por línea en tiempo real para actualizar el `task_manager`:

```python
# Mapeo conceptual en Python para envolver tu ejecutable binario de C++
import subprocess
import json

def run_spectre_binary_wrapper(task_id: str, arguments: list):
    task_manager.update_task(task_id, "running", 0.0)
    
    # Lanza el binario compilado de C++ / CUDA
    process = subprocess.Popen(
        ["./bin/ft_backtestopt_spectre"] + arguments,
        stdout=subprocess.PIPE,
        text=True
    )
    
    # Lee el stdout del binario línea por línea en tiempo real
    for line in process.stdout:
        if line.startswith("PROGRESS:"):
            # Ej: C++ imprime "PROGRESS:53.2"
            progreso = float(line.replace("PROGRESS:", "").strip())
            task_manager.update_task(task_id, "running", progreso)
            
    process.wait()
    if process.returncode == 0:
        task_manager.update_task(task_id, "completed", 100.0, result="Optimización CUDA exitosa")
    else:
        task_manager.update_task(task_id, "failed", 100.0, error="El proceso C++ falló")
```

### Flujo de Trabajo del Cliente Técnico (Frontend / Scripts externos)

1. El cliente envía un `POST` a `/api/backtest/vectorbt/run`.
2. Recibe una respuesta `200 OK` instantánea: `{"status": "accepted", "task_id": "fe39b..."}`.
3. El cliente inicia un ciclo (ej: cada 2 segundos) haciendo un `GET` a `/api/backtest/status/fe39b...`.
4. El endpoint responde la metadata actual: `{"status": "running", "progress": 45.0, ...}`.
5. Al llegar al `100.0` y estado `"completed"`, el cliente rompe el ciclo y recupera el objeto `"result"`.

---

## Ejemplo de implementación de `TaskManager`

Consideremos implementar el `TaskManager` utilizando **SQLite** de forma integrada desde el inicio para que los estados sobrevivan a un reinicio del servicio de **FastAPI**.

Para persistir el estado de tus ejecuciones efímeras y de servicios en la suite `financial_tools`, lo ideal es implementar el **`TaskManager` utilizando SQLite**. Dado que ya cuentas con una base de datos local contemplada en tu directorio `share/`, crearemos un gestor de tareas robusto e inmune a los reinicios de FastAPI.

Para garantizar un rendimiento óptimo y evitar bloqueos en la base de datos durante escrituras masivas concurrentes (muy común en backtests intensivos), configuraremos SQLite en **modo WAL (Write-Ahead Logging)** e implementaremos un _pool_ con hilos seguros.

Aquí tienes la implementación completa integrada en tu arquitectura:

### 1. El Administrador de Tareas Persistente (`common/python/task_manager.py`)

Este componente se ubicará en tu lógica compartida. Utiliza el módulo nativo `sqlite3` de Python, abstrayendo toda la persistencia mediante un diseño limpio.

```python
# src/financial_tools/src/common/python/task_manager.py
import sqlite3
import json
import uuid
import os
from typing import Dict, Any, Optional

class SQLTaskManager:
    def __init__(self, db_path: str = "share/tasks.db"):
        self.db_path = db_path
        # Asegurar que el directorio share/ exista
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        # check_same_thread=False es seguro porque abrimos una conexión por operación/hilo
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # Habilitar modo WAL para permitir lecturas concurrentes mientras se escribe el progreso
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL,
                    result TEXT,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def create_task(self, name: str) -> str:
        task_id = str(uuid.uuid4())
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, name, status, progress) VALUES (?, ?, ?, ?)",
                (task_id, name, "pending", 0.0)
            )
            conn.commit()
        return task_id

    def update_task(self, task_id: str, status: str, progress: float, result: Optional[Any] = None, error: Optional[str] = None):
        result_json = json.dumps(result) if result is not None else None
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE tasks 
                SET status = ?, progress = ?, result = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
            """, (status, progress, result_json, error, task_id))
            conn.commit()

    def get_task(self, task_id: str) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            
        if not row:
            return {"status": "not_found"}
            
        return {
            "task_id": row["task_id"],
            "name": row["name"],
            "status": row["status"],
            "progress": row["progress"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }

# Instancia global compartida por toda la suite
# Asumiendo que se ejecuta desde la raíz del proyecto para resolver 'share/'
task_manager = SQLTaskManager()
```

### 2. Actualización del Endpoint en FastAPI (ft_api_service)

Tu servicio de FastAPI no sufre cambios en su firma de métodos, ya que la interfaz del task_manager se mantiene idéntica, pero ahora los datos se guardan directamente en el archivo físico .db.

```python
# src/financial_tools/src/ft_api_service/routers/backtest.py
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from common.python.task_manager import task_manager
from ft_vectorbt.main import run_heavy_backtest  # Tu utilidad core

router = APIRouter(prefix="/backtest", tags=["Backtesting"])

class BacktestRequest(BaseModel):
    strategy_id: str
    pairs: list[str]

@router.post("/vectorbt/run")
async def start_vectorbt_backtest(payload: BacktestRequest, background_tasks: BackgroundTasks):
    # Genera la tarea y la guarda de inmediato en SQLite
    task_id = task_manager.create_task(name=f"VectorBT-{payload.strategy_id}")
    
    # Se despacha al threadpool nativo de FastAPI
    background_tasks.add_task(
        run_heavy_backtest, 
        strategy_id=payload.strategy_id, 
        pairs=payload.pairs, 
        task_id=task_id
    )
    
    return {
        "status": "accepted",
        "task_id": task_id,
        "poll_url": f"/backtest/status/{task_id}"
    }

@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str):
    # Lee directamente desde SQLite de forma ultra veloz
    task_info = task_manager.get_task(task_id)
    
    if task_info["status"] == "not_found":
        raise HTTPException(status_code=404, detail="La tarea no existe.")
        
    return task_info
```

### 3. Ventajas técnicas de este enfoque para tu Suite

1. Persistencia Post-Crash: Si el servidor de FastAPI experimenta un OOM (Out Of Memory) debido a la voracidad de vectorbt o se reinicia a mitad de la noche, las tareas mantendrán su estado registrado como "running" o "pending" en el disco. Puedes implementar fácilmente una rutina de inicio en FastAPI que verifique e identifique tareas huérfanas:

   ```python
   # Al levantar FastAPI:
   # UPDATE tasks SET status = 'interrupted' WHERE status = 'running';
   ```

2. Historial de Auditoría de Cargas: Al registrar created_at y updated_at, puedes construir un endpoint de telemetría para medir cuánto tiempo promedio le toma a cada utilidad (ft_backtrader, ft_backtestopt_spectre, etc.) completar sus simulaciones.

3. Lecturas no bloqueantes: Gracias a PRAGMA journal_mode=WAL;, los endpoints /status/{task_id} pueden ser consultados masivamente mediante polling constante por el cliente sin causar bloqueos de base de datos (database locked) en los hilos que procesan el backtest.

Para dejar listo el manejo de fallos estructurales, ¿te gustaría que agreguemos un script o endpoint de recuperación ante reinicios que cambie automáticamente el estado de las tareas que se quedaron colgadas en "running" a "failed" o "interrupted" cada vez que se encienda el servidor?
