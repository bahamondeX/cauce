# Incrementos

1. **Entregado en este árbol:** HTTP/1.1, pool propio, Docker, logs, TCP/Unix, routing,
   balanceo, streaming/SSE, límites y recarga con pruebas. Esta fase incluye algunas piezas
   adelantadas de fase 2. Consulte VALIDATION para separar pruebas ejecutadas de scripts.
2. **TLS/WebSocket implementados:** rustls inbound/upstream, aislamiento por generación y SNI/CA, timeout TLS,
   WebSocket bidireccional, cierre y cuotas retenidas. Evidencia en INCREMENT2.
   **Pendiente:** mTLS, ACME, health checks HTTP/2/gRPC,
   jitter/circuit breaker, DNS con TTL autoritativo/drenaje y allowlists de proxies confiables.
   Ampliar el clock duro por stream de HTTP/2 a HTTP/1.1; añadir parser-level
   telemetry, offload adicional de recargas y fuzzing diferencial HTTP. Stress test de clientes
   lentos, uploads half-close y churn de configuración. Aún no afirmar versión productiva completa.
3. **Colector local implementado:** proceso opcional, tail JSONL, checkpoints y spool
   transaccional acotado, rotación por rename, exportación offline y transporte experimental
   HTTPS con reintentos. Pruebas y garantías en COLLECTOR y VALIDATION-3.
   **Integración local independiente completada:** ocho casos con adaptador real,
   autenticación y sink sobre Moto. **Pendiente:** despliegue cloud, login y demo
   completa de dashboard; no se afirma AWS real. Ver COMBINED-VALIDATION.
4. **HTTP/2 y gRPC implementados:** ALPN/prior knowledge, multiplexación real, cuotas
   de streams, GOAWAY/drenaje, trailers y las cuatro modalidades gRPC. Límites y pruebas
   reproducibles en HTTP2 y VALIDATION-4. **Pendiente:** FastCGI, uwsgi, TCP/UDP,
   estáticos/caché según prioridad. Cada protocolo requiere código, interoperabilidad y pruebas.
   Evaluación independiente de HTTP/3. Optimizar pools/sharding solo con perfiles y resultados.

5. **Timeout HEADERS HTTP/2 implementado:** reloj absoluto de entrada para frames
   parciales y bloques HEADERS/CONTINUATION, incluidos trailers, después de TLS
   o en claro. Cierre y cancelación por conexión; pruebas Docker en VALIDATION-5.
6. **Fuzzing y estrés HTTP/2 implementados:** target libFuzzer sobre el observador
   de producción y pruebas Docker con cuotas pequeñas, saturación, cancelación,
   errores de framing y recuperación de recursos. Gauges de permisos y tareas;
   evidencia y comandos en VALIDATION-6.
   **Pendiente:** campañas más largas y distribuidas, comparación diferencial con
   otros stacks HTTP/2 y cargas de producción. No se afirma certificación RFC
   exhaustiva, ausencia universal de fugas ni resistencia DDoS.
7. **Benchmark HTTP/1.1 comparativo implementado:** runner Docker reproducible para Cauce
   y NGINX estable, un proxy por ejecución, mismos fixtures y cargas con logs apagados y
   encendidos. Ocho escenarios: normal, delayed, saturation, TLS warm, TLS cold, SSE,
   slow-reader y WebSocket. Registra CPU del cgroup, memoria del cgroup, RSS, threads,
   file descriptors, conexiones y reutilización. La configuración de referencia limita
   ambos proxies a 1 CPU, 256 MiB y 32 solicitudes/conexiones upstream; conserva la
   diferencia entre cuota global de solicitudes activas de NGINX y límites del pool Cauce.
   Resultados, recursos, imágenes y límites de interpretación se documentan en
   [VALIDATION-7.md](VALIDATION-7.md). El benchmark no demuestra superioridad general.

8. **Benchmark HTTP/2 y gRPC implementado:** NGINX con `proxy_http_version 2` y `grpc_pass`, nueve escenarios, tres repeticiones, límites de entrada 16 streams por sesión y 32 requests globales; raw offers, RPCs, recursos, contadores backend y gates de recuperación. TLS valida CA/nombre en ambos tramos. El runtime 0.7.0 permanece igual. Ver [VALIDATION-8.md](VALIDATION-8.md) y [demo offline](DEMO.md). Esa entrega no verificó el adaptador local posterior de Connectivity; el estado actualizado se documenta en [CONNECTIVITY.md](CONNECTIVITY.md).

9. **Pruebas de fallos y recuperación implementadas:** backend detenido y POST cortado sin reintento, recarga válida/inválida durante carga, liberación de permisos/FDs, disco lleno real y fallo de rotación del logger, spool saturado, SQLite sin espacio y reinicio del colector tras SIGKILL. Cauce **0.9.0** repara el tail JSONL incompleto y entrega su inode a rotación antes de escribir otra vez; prueba integrada del checkpoint. Resultados y límites en [VALIDATION-9.md](VALIDATION-9.md), comando reproducible en [tests/resilience](../tests/resilience/README.md).

Pendiente: benchmarks de rendimiento bajo fallos, rampas de capacidad, campañas
prolongadas y fallos de almacenamiento más amplios (I/O bloqueada, corrupción,
pérdida eléctrica y reinicio de VM). Los tmpfs de prueba se mantienen montados
al reiniciar procesos. Las mediciones locales no equivalen a producción ni
establecen superioridad fuera de sus cargas y límites.

**Parche 0.9.1 de contrato:** ACK HTTP 200 exacto y errores sin contenido del receptor; ejemplo de configuración offline con límites del adaptador local. Ver [CONNECTIVITY.md](CONNECTIVITY.md) y la [auditoría del pedido original](REQUIREMENTS-AUDIT.md). Las pruebas cliente con fixtures no sustituyen los gates de despliegue/servidor.

10. **Promoción local validada:** health checks HTTP/1 opt-in y protección del frontier observado del colector; 39 tests Rust y ocho casos locales de integración. Ver COMBINED-VALIDATION.
