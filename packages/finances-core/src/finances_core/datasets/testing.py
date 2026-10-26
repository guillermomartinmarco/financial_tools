"""*Helper* de tests ``real_data`` (plan §3.6, RF-10).

:func:`require_dataset` verifica **solo** el ítem pedido contra el *lock* y devuelve su
``LazyFrame``; si el ítem no está ``OK``/``OK_WITH_WARNINGS`` el test **falla** con el
detalle de la verificación (nunca se omite en silencio). ``pytest`` se importa de forma
perezosa: ``finances-core`` no depende de ``pytest`` en *runtime*.
"""

from __future__ import annotations

import polars as pl

from finances_core.config import ConfigError, FinancesSettings
from finances_core.datasets import store
from finances_core.datasets.errors import DatasetsError
from finances_core.datasets.recipes import find_item, load_lock, load_recipe
from finances_core.datasets.verify import verify_item

_ACCEPTED = frozenset({"OK", "OK_WITH_WARNINGS"})


def require_dataset(
    recipe: str, key: str, settings: FinancesSettings | None = None
) -> pl.LazyFrame:
    """``LazyFrame`` del ítem ``key`` de la receta ``recipe``, verificado contra su *lock*.

    Usa ``FT_DATASETS_RECIPES_DIR`` y ``FT_DATASETS_DIR`` de ``settings`` (por defecto,
    ``FinancesSettings()``). Cualquier fallo (receta o ítem inexistente, *lock* inválido,
    datos ausentes o distintos) termina en ``pytest.fail``.
    """
    import pytest  # noqa: PLC0415 - perezoso (plan §3.6): pytest no es dependencia de runtime

    cfg = settings or FinancesSettings()
    try:
        datasets_dir = cfg.require_datasets_dir()
        loaded = load_recipe(cfg.DATASETS_RECIPES_DIR, recipe)
        item = find_item(loaded, key)
        lock = load_lock(cfg.DATASETS_RECIPES_DIR, recipe)
        expected = next((i for i in lock.items if i.key == key), None) if lock else None
        verification = verify_item(datasets_dir, item, expected)
    except (DatasetsError, ConfigError, OSError) as exc:
        pytest.fail(f"require_dataset({recipe!r}, {key!r}): {exc}", pytrace=False)
    if verification.status not in _ACCEPTED:
        pytest.fail(
            f"require_dataset({recipe!r}, {key!r}): ítem {verification.status} en "
            f"{datasets_dir}. Materializa con `ft_datasets materialize {recipe}`.\n"
            f"{verification.model_dump_json(indent=2)}",
            pytrace=False,
        )
    return store.scan_item_lazy(datasets_dir, item)
