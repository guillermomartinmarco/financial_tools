# Artículos de referencia

Referencias externas que guían el diseño de la suite. Los artículos se **enlazan, no se
redistribuyen**: el repositorio es público y sus licencias no permiten republicarlos. Si
tenés una copia local (por ejemplo, una traducción), guardala en esta carpeta; los PDF
están excluidos por `.gitignore`.

| Artículo | Uso en el proyecto |
|----------|--------------------|
| Zura Kakushadze, *101 Formulaic Alphas*, Wilmott Magazine 2016(84), 72–80. [arXiv:1601.00991](https://arxiv.org/abs/1601.00991) | Guía para estrategias tipo *alpha* formulaica (`formulaic_alpha`, tridente futuro; ver la nota de alcance en `specs/finances_backtester/spec.md`). Solo aplican los operadores de serie temporal: `rank` es transversal entre activos y `vwap`, `cap` y la neutralización por industria no están disponibles en el almacén OHLCV. |
