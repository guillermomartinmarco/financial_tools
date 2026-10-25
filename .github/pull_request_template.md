## Resumen

<!-- Qué cambia y por qué. Enlazar specs/ft_<nombre>/ afectados. -->

## Matriz de validación cruzada (Art. 20 de la Constitución)

Herramienta(s): `ft_...`

- [ ] **Funcional**: 100% de los requisitos EARS cubiertos por tests (Art. 7)
- [ ] **Contractual**: `spec.md` / `plan.md` / `tasks.md` actualizados y coherentes con el código
- [ ] **Arquitectural**: dualidad CLI/API verificada (Art. 1); sin imports entre herramientas `ft_*` (Art. 4)
- [ ] **Datos**: float32/int64 (Art. 5); validación Pydantic (Art. 13)
- [ ] **Concurrencia**: sin bloqueo del event loop (Art. 3); idempotencia (Art. 12); hilos seguros (Art. 16)
- [ ] **Observabilidad**: logs duales CLI/servicio (Art. 9); métricas VRAM si aplica (Art. 19)
- [ ] **Resiliencia**: rate-limit, backoff y reintentos donde haya red (Art. 14)
- [ ] **Calidad**: CI verde (ruff + ruff format + mypy --strict + pytest); cobertura ≥ 90% (Art. 17)
- [ ] **Desempeño**: benchmarks de regresión dentro de umbral (o "N/A" justificado)
- [ ] **Despliegue**: despacho asíncrono + polling funcional (Art. 18)

Firma Tech Lead: <!-- @usuario y fecha -->
