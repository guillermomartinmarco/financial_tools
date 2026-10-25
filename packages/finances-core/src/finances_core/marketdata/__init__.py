"""Núcleo de descarga de datos de mercado compartido por las herramientas ``ft_market_data_*``.

ADR-Y06 / Art. 4: las herramientas hermanas no se importan entre sí; el escritor Hive,
el inventario local, la persistencia de planes, el ciclo de vida de tareas, los
reintentos y el rate-limit genéricos viven aquí, sin nada específico de un exchange.
"""
