# Matriz de compatibilidad

Estado se refiere a este incremento, no al plan completo.

| Capacidad | Estado | Evidencia / alcance |
|---|---|---|
| HTTP/1.1 cliente → proxy → HTTP/1.1 upstream | Probado | Integración sockets reales, GET/POST, Content-Length y chunked |
| Pool compartido global/per endpoint | Probado | Cliente nuevo reutiliza mismo connection_id upstream; espera acotada, idle/edad y cancelación |
| Host exacto, prefijo path con frontera, round-robin | Probado | Host inválido, prefijos, secuencia de destinos |
| Least-inflight | Probado | Solicitud lenta desvía otra al destino libre |
| SSE sin buffering completo | Probado | Primer evento antes de completar stream |
| Upload/download streaming y backpressure | Implementado y probado parcialmente | Frames, echo chunked, cancel, idle; prueba de cliente sin leer a gran escala pendiente |
| Unix sockets HTTP | HTTP/1.1 probado; HTTP/2 implementado | Backend Unix HTTP/1.1 real y reutilización; fixture Unix HTTP/2 pendiente |
| Uvicorn con dos workers HTTP | Probado | Docker ARM64 y smoke amd64 emulado; VALIDATION |
| Node.js HTTP | Probado | Docker ARM64 y smoke amd64 emulado; VALIDATION |
| Gunicorn sirviendo HTTP | Pendiente de prueba | Mismo contrato HTTP; no se declara prueba ejecutada |
| TLS inbound / TLS upstream, SNI y CA | Probado | rustls TLS1.2/1.3; ver pruebas y alcance en INCREMENT2 |
| mTLS / ACME | Pendiente | Sin certificados cliente ni renovación automática |
| Health checks activos | Pendiente, fase 2 | Retirada pasiva temporal por fallos de transporte implementada |
| WebSocket ws/wss | Probado | Upgrade validado, frames bidireccionales, cierre y límites; pruebas en INCREMENT2 |
| Prometheus /live /ready | Probado | Auth metrics; readiness no implica salud de cada backend |
| Config validada / reload atómico / drenaje | Probado | SIGHUP válido/inválido, SIGTERM acotado; cambios de límites requieren restart |
| Logs JSONL / esquema / redacción / rotación / fallo IO | Probado | Evidencia y validación JSON Schema; política con posibles pérdidas |
| DNS caché de TTL administrativo | Implementado | Resolver SO, single-flight; TTL autoritativo y drenaje por cambio DNS pendientes |
| HTTP/2 downstream y upstream | Implementado, incremento 4 | ALPN h2 y claro prior knowledge, multiplexación, SETTINGS, límites, GOAWAY; ver HTTP2 y VALIDATION-4 |
| HEADERS/CONTINUATION HTTP/2 incompletos | Implementado, incremento 5 | Plazo absoluto por bloque, frame headers parciales y trailers; cierre de conexión y cancelación; ver VALIDATION-5 |
| Fuzzing y estrés HTTP/2 | Implementado, incremento 6 | libFuzzer/ASan y campañas Docker TCP/TLS; cuotas y recuperación de recursos; ver VALIDATION-6 |
| Benchmark comparativo NGINX/Cauce | Implementado, incremento 7 | Runner Docker HTTP/1.1 con escenarios normal, delayed, saturation, TLS warm/cold, SSE, slow-reader y WebSocket; logs on/off; mide CPU/cgroup, memoria, RSS, threads, FDs y conexiones. Límites y resultados en VALIDATION-7.md; sin conclusión de superioridad general |
| Benchmark HTTP/2 y gRPC | Implementado, incremento 8 | Nueve escenarios en Docker nativo; HTTP/2 claro/TLS en ambos tramos y cuatro modalidades gRPC, presupuestos comunes y gates de 16 streams/32 solicitudes. Evidencia y límites en VALIDATION-8.md |
| Corrección bajo fallos, reload, disco lleno y spool saturado | Probado, incremento 9 | Docker real con tmpfs acotado, recuperación de logs, checkpoints e integridad SQLite; ver VALIDATION-9.md |
| Rendimiento bajo fallos y rampas de capacidad | Pendiente | Las pruebas funcionales del incremento 9 no son un benchmark de capacidad o latencia |
| gRPC, trailers y streaming gRPC | Implementado, incremento 4 | HTTP/2 en ambos lados; unary, client/server streaming y bidi; metadata, status y deadline; ver VALIDATION-4 |
| Colector local, spool, checkpoints, exportación | Implementado, fase 3 | Proceso opcional offline; SQLite transaccional; ver COLLECTOR y VALIDATION-3 |
| Transporte HTTPS del colector | Experimental, proposal-1 | Receptor local de pruebas, ACK validado, reintentos y validación TLS |
| Ingesta, namespaces y login Connectivity | Contrato local documentado; integración independiente y despliegue pendientes | Adaptador proposal-1 con token de máquina/namespace servidor según sus documentos; Cauce offline por defecto. Ver CONNECTIVITY.md |
| FastCGI / uwsgi / TCP / UDP | Pendiente, fase 4 | No son lo mismo que Gunicorn/Uvicorn sirviendo HTTP |
| Caché / archivos estáticos | Pendiente, fase 4 | No hay cacheado de respuestas |
| HTTP/3 | Fuera del incremento; evaluación independiente | No está prometido por la elección de Rust |
| Equivalencia con todos los módulos NGINX | Fuera de alcance | No se afirma |
