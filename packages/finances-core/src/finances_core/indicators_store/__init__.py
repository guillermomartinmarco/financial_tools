"""Núcleo compartido por ``ft_indicators`` y ``ft_ta_lib`` (plan de ``ft_indicators`` §1.0).

Art. 4: las dos herramientas viven en ``finances-analytics`` pero no se importan entre
sí. Todo lo común se parametriza por espacio de nombres (``Namespace``):

- :mod:`.request`: gramática ``name[:k=v,...]``.
- :mod:`.namespaces`: prefijos de ``series_id``, manifiesto y bloqueo por herramienta.
- :mod:`.source`: carga y validación (Art. 13) de la fuente Hive de los descargadores.
- :mod:`.store`: almacén por serie y mes bajo ``FT_INDICATORS_DIR``.
- :mod:`.manifest`: manifiesto atómico y bloqueo ``O_EXCL`` por dataset y herramienta.
- :mod:`.view`: vista ancha para exportar.
- :mod:`.runner`: orquestación de tareas ``PENDING → PLANNING → COMPUTING → …``.
"""
