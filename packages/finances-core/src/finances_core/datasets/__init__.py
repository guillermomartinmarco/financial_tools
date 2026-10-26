"""Recetas de datasets reales reproducibles (núcleo de ``ft_datasets``, ADR-D01).

Receta y *lock* (:mod:`.recipes`), hash de contenido canónico ``ftds-v1``
(:mod:`.hashing`), lectura del almacén Hive (:mod:`.store`) y verificación
(:mod:`.verify`). Viven en el núcleo para que los tests ``real_data`` de cualquier
paquete verifiquen datos sin importar ``ft_datasets`` (Art. 4). El *helper* de tests
:mod:`.testing` no se importa aquí: ``finances-core`` no depende de ``pytest``.
"""
