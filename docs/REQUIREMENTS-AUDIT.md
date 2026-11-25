# Auditoría de requisitos

Auditoría documental del pedido inicial frente a `README.md`, `ROADMAP.md`,
`COMPATIBILITY.md` y la evidencia de validación disponible hasta el incremento 9.
No se ejecutaron builds ni campañas adicionales para este documento. Por ello,
“probado” abajo significa que el repositorio conserva un reporte local que declara
la ejecución; no significa que esta auditoría haya vuelto a ejecutar o revalidado
cada campaña histórica.

## Matriz

| Requisito original | Estado | Evidencia y alcance / límite |
|---|---|---|
| Proxy propio en Rust, Docker, sin motor NGINX/Envoy/Pingora; operar sin Connectivity | **Implementado; probado localmente** | `README.md`, `ROADMAP.md`, validaciones 1–9. Cauce tiene motor propio y funciona desconectado del servicio externo. No equivale a paridad con NGINX. |
| Runtime asíncrono multihilo; arquitectura separada; pool compartido entre tareas; límites | **Implementado; probado parcialmente** | Pool compartido, límites y drenaje están descritos en README/arquitectura y ejercitados en suites funcionales y benchmarks. Los límites son los configurados y observados en esas fixtures, no una capacidad universal. |
| HTTP/1.1, routing host/path, round-robin, least-inflight, balanceo y backends Uvicorn/Node | **Probado para Uvicorn y Node; Gunicorn pendiente** | `COMPATIBILITY.md`, VALIDATION e incrementos previos. Gunicorn aparece explícitamente pendiente de prueba. No se afirma compatibilidad con cualquier implementación HTTP sin su matriz de interoperabilidad. |
| Pools H1/H2, límites globales/por upstream, espera acotada, idle/edad, drenaje, aislamiento y TCP keepalive | **Implementado; cobertura de laboratorio** | Pool HTTP/1 y HTTP/2, reutilización, cuotas, aislamiento y drenaje tienen pruebas/reportes históricos. `src/pool.rs` aplica TCP keepalive al abrir sockets TCP, con `tcp_keepalive_secs`; esto detecta pares TCP inactivos y no equivale a reutilización HTTP. Semánticas no son idénticas a NGINX; VALIDATION-7/8 documentan diferencias de cuotas. DNS usa resolver del sistema/single-flight, pero TTL autoritativo y drenaje por cambio DNS están pendientes. |
| TLS entrante y upstream, CA/nombre/SNI; WebSocket; SSE y streaming/backpressure | **Probado en casos acotados** | Incremento 2 y `COMPATIBILITY.md`; benchmarks 7 ejercitan TLS caliente/frío, SSE, lector lento y WebSocket. WebSocket usa HTTP/1.1 y no implementa RFC8441/H2. No se ha demostrado comportamiento para toda combinación de clientes, certificados, carga o duración. mTLS y ACME pendientes. |
| HTTP/2 completo, gRPC, trailers, streaming, límites de streams y timeout de HEADERS incompletos | **Implementado y probado localmente** | VALIDATION-4/5/6 y regresión 0.9 citada por VALIDATION-9. Reporta H2/gRPC claro y TLS, modalidades streaming, límites, cancelación/GOAWAY y regresión. Fuzzing se limita al observador HTTP/2; no cubre HPACK, rustls ni todo el estado interno de Hyper. No es certificación RFC. |
| Health checks, retirada de destinos fallidos y prevención de tormentas | **Parcial** | Hay retirada pasiva/backoff; VALIDATION-9 ejercita SIGKILL, selección del destino sano y recuperación. Health checks HTTP/1 opt-in implementados y probados; HTTP/2/gRPC health, jitter/circuit breaker y prueba de tormentas prolongadas siguen pendientes. No hay reintentos automáticos de requests: incluso un GET falla en un intento; el caso POST cortado observó una sola recepción en esa ejecución. |
| Streaming con cancelación, shutdown ordenado, deadlines y sobrecarga acotada | **Implementado; probado parcialmente** | Pruebas H2, pool, saturación y drenaje reportadas; VALIDATION-9 observa gauges a cero y FD 13→12 en ese caso concreto. El logger prioriza servir HTTP y puede perder eventos. Los límites de muestreo no prueban ausencia matemática de fugas. |
| Parser robusto: framing ambiguo, hop-by-hop, smuggling, slowloris, backend que corta conexión | **Casos HTTP/1 probados; cobertura no exhaustiva** | `tests/integration.py` y `docs/linux-integration-results.txt` reportan pruebas de smuggling/framing y slowloris: CL+TE se canonicaliza al reenviar, mientras CL duplicado conflictivo, TE no soportado, `Connection` sobre framing y whitespace malformado se rechazan; header incompleto y header grande cierran o devuelven 408/431. HTTP/2 añade fuzz/estrés del observador y timeout de HEADERS. `THREAT-MODEL.md` mantiene riesgo residual y fuzzing diferencial pendiente; no es cobertura de todas las variantes. |
| `Forwarded`/`X-Forwarded-*` confiables solo desde proxies configurados; evitar proxy abierto | **Parcial** | README declara rutas explícitas, sin wildcard, CONNECT ni destino suministrado por cliente. ROADMAP deja pendientes allowlists de proxies confiables; por tanto, no se acredita todavía la política configurable de confianza requerida. |
| Logs JSONL versionados, esquema, tiempos monotónicos/semántica, no secretos, cardinalidad, writer acotado/rotación/ENOSPC | **Implementado; probado en escenarios definidos** | LOGGING, esquema y VALIDATION-9: JSON Schema, redacción, ENOSPC real en tmpfs de 4 MiB, fallo de rename y reparación del tail incompleto en 0.9.0. En el fallo se pierden eventos; los contadores de IO no cuantifican todas las pérdidas y `log_dropped_total` es cota inferior. Archivo requiere un escritor y permisos adecuados. |
| Colector opcional: seguimiento incremental, checkpoints, rotación, retries/backoff, batches y spool acotado | **Implementado; probado localmente** | COLLECTOR/VALIDATION-3 y suite de resiliencia: cuota saturada, checkpoint, ENOSPC, SIGKILL del proceso y recuperación/integridad SQLite. La recuperación requiere liberar espacio y reiniciar el colector; spool/export offline no promete recuperar logs que nunca llegaron a JSONL. |
| Contrato real de Connectivity, identidad/namespace autorizados, login y entrega remota | **Contrato y cliente proposal-1 implementados localmente; integración independiente pendiente** | `docs/CONNECTIVITY.md` ahora documenta un adaptador local para `proposal-1` y el cliente HTTPS opt-in de Cauce (ACK exacto, token desde archivo, TLS validado, sin redirects, spool retenido ante fallo). No hay endpoint desplegado; Cauce no inicia login ni emite/provisiona tokens. La prueba nueva del cliente usa solo un receptor HTTPS sintético. Montar el adaptador real del otro worktree con bootstrap/autorización local aún requiere un paquete/copia autorizada; por eso la integración Cauce→adaptador real no está probada por este repositorio. |
| Docker multistage/rootless, señales, amd64 y arm64, Compose Python/Node y operación | **Mayormente implementado; validar por plataforma** | README describe la experiencia y CI local; VALIDATION-9 es ARM64 nativo. Existen builds/smokes amd64 emulados en Colima, que no equivalen a rendimiento ni prueba en hardware amd64 nativo. No se publica una imagen en un registro como parte de estas validaciones. |
| Benchmarks reproducibles frente a NGINX con latencia, throughput, CPU, memoria, errores, límites y datos crudos | **Benchmarks implementados; resultados históricos con alcance acotado** | VALIDATION-7/8 conserva metodología, ejecuciones, datos, hashes y comparaciones con límites. Ambos benchmark son de Cauce **0.7.0**, no 0.9.0. Tasas fijas y ventanas cortas no miden capacidad máxima; hay interferencia del host/VM y diferencias de semántica de pool. No se concluye superioridad general. |
| Comparar fallos/reload/disco lleno bajo carga y rendimiento ante rampas de capacidad | **Recuperación funcional probada; benchmark bajo fallos pendiente** | VALIDATION-9 prueba fallos/reloads, ENOSPC, rotación y spool con Docker acotado. ROADMAP mantiene pendientes los benchmarks de rendimiento bajo fallo, rampas, campañas largas, I/O bloqueada, corrupción, pérdida eléctrica y reinicio VM. Los tmpfs se mantienen montados durante reinicios de proceso. |
| FastCGI, uwsgi, TCP/UDP, estáticos/caché, HTTP/3 y paridad completa NGINX | **Pendiente / fuera de alcance explícito** | ROADMAP/COMPATIBILITY sitúan estos protocolos y funciones en fase futura o evaluación separada; no hay afirmación de equivalencia con todos los módulos NGINX. |
| No usar GitHub Actions | **Conforme en los entregables examinados** | Los comandos descritos son scripts locales y Docker; no se encontró requisito de GitHub Actions en el plan entregado. |

## Estado de Connectivity: evidencia separada

Esta sección se limita a los tres documentos de Connectivity indicados por el
equipo externo: `PROXY_INGEST_CONTRACT.md`, `PHASE2B_DURABILITY.md` y
`PHASE2B_CONSUMER.md`, resumidos también en el nuevo `docs/CONNECTIVITY.md` de
Cauce. Sus resultados son **declaraciones del equipo externo sobre pruebas
locales**, no pruebas repetidas por esta auditoría ni confirmación de servicio
desplegado.

| Evidencia externa declarada | Qué respalda | Qué no respalda |
|---|---|---|
| Contrato/adaptador local y `make ci`: parser, privacidad, límites, ACK, token y sink fake; 44 tests específicos dentro de 229 Python + 3 frontend | Diseño local de contrato y cobertura local de su adaptador | Endpoint desplegado, autenticación de producción o interoperabilidad real por sí solo. El documento dice que el adaptador no está desplegado. |
| Sink boto3 y token verifier local; DynamoDB Local 3.3.2 para dos carreras; fake S3 atómico | Comportamiento local estrecho de recibos/reservas y fences DynamoDB | Contención real de S3, IAM/AWS, endpoint/rutas/tablas/políticas desplegadas, límites de coste productivos. |
| Colector Cauce existente contra API HTTPS local y Moto S3/DynamoDB; ACK oculto como 503, reintento del mismo UUID y spool drenado | Evidencia local, declarada por el equipo externo, de interoperabilidad de ese binario/hash, fixture y contrato; acredita trabajo de colector real, no solo parser | AWS real, todas las configuraciones, throughput sostenido o HTTP/2 en vivo; no prueba ingesta desplegada ni sustituye la prueba independiente del árbol Cauce contra el adaptador. |
| Consumidor/planes SQL, ejercicios con DuckDB y Parquet, y plan de borrado | Construcción y pruebas locales de SQL/forma, lógica de consumidor y controles descritos | Athena/Iceberg real, tabla/Glue/Lambda/DLQ desplegados o borrado físico de datos. El resultado de borrado se describe como `awaiting_iceberg_rewrite_and_retention`. |

Los documentos externos declaran sus suites finales locales con 263 y luego 298
tests Python, además de frontend/lint/typecheck, con opt-ins locales. Esos conteos
pertenecen a incrementos/repositorios externos y no deben sumarse a los tests de
Cauce. Los documentos describen el adaptador/sink/consumer local y una prueba
limitada del colector contra el adaptador local, pero coinciden en que no se
desplegó el servicio de ingesta. La prueba propia del cliente Cauce usa un receptor
sintético y no envía datos al servicio ni valida autorización real. El arranque
normal continúa offline por defecto.

## Conclusión y límites

El repositorio acredita una base funcional sustancial de proxy HTTP/1.1, TLS,
WebSocket, HTTP/2/gRPC, límites, collector offline y recuperación local en
escenarios Docker reproducibles. La evidencia más reciente de fallos corresponde
a Cauce 0.9.0 ARM64 sobre Colima, con cuotas y tmpfs explícitos. Los benchmarks
comparativos corresponden a 0.7.0; no son resultados del runtime 0.9.0.

El alcance pendiente más importante para satisfacer literalmente el pedido es el
despliegue y montaje independiente autorizado del adaptador de Connectivity,
aprovisionamiento/login real y validación de autorización/namespace; además de
health checks HTTP/2/gRPC, mTLS/ACME y Gunicorn probado. Hay casos HTTP/1 de
smuggling/slowloris, aunque la cobertura diferencial/exhaustiva sigue pendiente.
La política actual elimina/reconstruye `Forwarded` y `X-Forwarded-*` y no confía
en ellos; allowlists de proxies confiables siguen pendientes. También falta
caracterizar capacidad máxima y rendimiento durante fallos. Nada de lo anterior
permite afirmar disponibilidad, seguridad, durabilidad o rendimiento en producción.

## Actualización: integración local independiente completada

La revisión inicial anterior queda actualizada para el montaje autorizado del
adaptador y la autorización/namespace locales: ocho escenarios aprobados con
collector 0.9.1, servidor real copiado sin cambios y Moto.
[Pruebas y límites](VALIDATION-CONNECTIVITY-REAL.md). Permanecen pendientes el
despliegue, login/aprovisionamiento real y los demás requisitos señalados.
