# Validación ejecutada — 16 noviembre 2026

**Histórico del incremento 1.** Para la versión 0.2, consulte [TLS/WebSocket](INCREMENT2.md) y [validación del incremento 2](VALIDATION-2.md). Las limitaciones TLS/WS de esta página describen la entrega 0.1.

Entrega del incremento 1, no certificación productiva ni integración completa.

| Comprobación | Resultado y evidencia |
|---|---|
| Host | macOS ARM64; rustc/cargo 1.88.0; dependencias fijadas en Cargo.lock |
| Formato y lint | cargo fmt --check, clippy --locked --all-targets -- -D warnings aprobados; check-results.txt |
| Unitarios Rust | 4 aprobados; check-results.txt |
| Integración nativa | 19 aprobados, cero skip; check-results.txt |
| Integración Linux ARM64 release | 19 aprobados, cero skip; linux-integration-results.txt; binario extraído de imagen final |
| JSON Schema | Eventos reales de la suite validados con Draft202012 y format checker; schema-results.txt y test de esquema |
| Docker ARM64 | Build multistage aprobado, ejecución UID/GID 10001; docker-final-build.txt y docker-smoke.json |
| Docker amd64 | Build cruzado aprobado; ejecución por emulación Colima: config check, UID 10001 y seis respuestas balanceadas; amd64-smoke.json y docker-amd64-cross-build.txt. No probado sobre hardware x86 nativo ni suite completa amd64 |
| Uvicorn/Node | Seis GET alternados 200 y SSE incremental; docker-smoke.json. Uvicorn con 2 workers en Compose |
| Disco lleno | Linux /dev/full genera ENOSPC real sin llenar filesystem: 20/20 respuestas 200 y errores de log >0; disk-full.json |
| Rendimiento | Smoke abierto contra NGINX estable 1.30.6, bruto/percentiles/estadísticas en ../bench/; alcance limitado explicado allí |
| Connectivity | Lectura de código local/remoto y HEAD público 200; sin contrato de ingesta/login/namespaces verificado; CONNECTIVITY.md |

Los tests usan sockets TCP/Unix y procesos reales: no mocks del pool ni del transporte.
Cubren framing/hop headers, spoofing Forwarded, Host/path, SSE, upload chunked antes de
terminar el body, respuesta temprana con upload incompleto, descarte por cancelación,
stream timeout, sobrecarga, espera de pool, idle/edad, recarga válida/inválida, apagado
acotado, destinos que cierran, round-robin/least-inflight, redacción, esquema, rotación,
fallo IO y uploads inválidos que no retiran backends sanos.

El primer intento de build amd64 bajo emulación falló con SIGSEGV de cc enlazando
serde_core; docker-amd64-build.txt conserva el fallo. El Dockerfile final compila en
BUILDPLATFORM y cruza con gcc/sysroot hacia TARGETARCH. La construcción final y smoke
amd64 sí se ejecutaron, sin inferir que equivalen a hardware amd64 real.

Un caso TE+CL es canonicalizado por Hyper 1.12 eliminando CL antes del handler, no
rechazado estrictamente; se comprobó framing único al backend. Variantes de fuzzing,
clientes que no leen a escala, DNS autoritativo/cambio de IP, TLS, H2, WebSocket, gRPC,
colector/OIDC y benchmarks completos siguen pendientes. Gunicorn no fue ejecutado.
Los antiguos integration-results.txt y docker-build-results.txt registran iteraciones
anteriores; check-results.txt, linux-integration-results.txt y builds finales son la
evidencia aplicable a la entrega. No se crearon GitHub Actions ni se publicó un repositorio.
