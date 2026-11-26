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

## Programa de paridad NGINX OSS

| Milestone | Gate |
| --- | --- |
| Reference inventory and baseline | Generate the pinned module/directive registry, execute the existing test suites, and retain raw historical reproductions plus native results for both Linux architectures. |
| Routing and request-path cost | Compare the compiled matcher, event construction, config snapshots and maintenance scheduling independently. Preserve reload, path behavior, protocol limits and backpressure. |
| Versioned configuration | Validate YAML and TOML through the same configuration model. Publish editor schema and run SIGHUP rejection and rollback checks. Inventory unsupported NGINX migration semantics before emitting an equivalent config. |
| Endpoint pooling and balancing | Attribute contention with Linux runtime data, then compare sharding and group state while tracking global and endpoint capacity. |
| Protocols and modules | Validate TLS and listener extensions, stream and mail proxying, file serving and caching, gateways, policies, filters, and programmable extensions against pinned reference fixtures. |
| Release evidence | Reproduce SLO goodput, resources, recovery and stability with pinned NGINX, HAProxy and Pingora on the same native hosts. |

Milestone 2B established the diagnostics and benchmarks that Milestone 2C continues. Its retained
changes fix independently reproduced lease races and eliminate allocations
from pool bookkeeping and access log serialization. The original two-worker
transport error still requires attribution. Linux ARM64 and AMD64 capacity,
hardware profiles and competitive parity remain open. Use the isolated pool
layout study and the native runner to qualify the next dataplane experiment
before selecting a new pool, timer design or runtime setting.
[Milestone 2B results and open gates](PERFORMANCE-MILESTONE-2B.md).

Statuses and evidence live in [the compatibility registry](../compatibility/nginx-oss.yaml) and [program status](../STATUS.md).


Milestone 2C was the gate before functional expansion. The compact
pool remains the default with endpoint sharding available for experiments.
Native hardware and qualified competitive results remain pending. Resume
with the ARM64 and AMD64 campaigns, repeat the transport-tail diagnosis,
and use the native paired pool comparisons to decide whether to automate
strategy selection.
[Milestone 2C report](PERFORMANCE-MILESTONE-2C.md)
[Compatibility backlog ordered by migration impact](NGINX-COMPATIBILITY-BACKLOG.md).

## Milestone 3

The native qualification, functional parity and production readiness
tracks advance independently. The first functional slice implements
wildcard virtual hosts and literal exact locations, with NGINX socket
comparison and YAML reload/rollback validation. The migration preview
covers the corresponding literal subset and reports confidence. Continue
from the migration and protocol gaps in the source-derived registry.
[Functional parity report](FUNCTIONAL-PARITY.md)
[Native qualification](PERFORMANCE-NATIVE.md).

The AMD64 benchmark establishes one matched HTTP/1.1 comparison and
several confirmed capacity points. Resume ARM64 after independent
AWS cleanup verification. Requalify generator and backend headroom
before multicore scaling, sustain a clean stability run, and compare
the corrected compact and sharded implementations with seven
paired repetitions. The production-readiness queue includes richer
migration diagnostics, supply-chain inventory and protocol-boundary
validation. [Production status](../STATUS.md).

## Milestone 4

The gateway supports flat location precedence and static route-level
request and response header policies. The compatibility queue continues
with rewrites, redirects, trusted forwarding, weighted balancing and
traffic limits. Certificate selection and mutual TLS precede the next
web serving and caching slice. [Functional coverage](FUNCTIONAL-PARITY.md).

The revised auditor distinguishes warm-up and generator queue loss from
an attributable saturation boundary. The ARM64 artifacts are staged.
Control-plane reconciliation remains the prerequisite for any new
AWS run. [Performance and lifecycle](PERFORMANCE-MILESTONE-4.md).

## Milestone 5

The checkpoint covers trusted IPv4/IPv6 forwarding, bounded internal and
external route actions, weighted and hash balancing, and shared bounded
admission. Nested NGINX locations, full PCRE and variable semantics,
directive inheritance, exact hash compatibility and combined admission
zones remain open. [Gateway coverage](FUNCTIONAL-PARITY.md#milestone-5-production-gateway).

The next functional increments cover DNS TTL and connection retirement,
upstream backoff, active HTTP/2 and gRPC health, and SNI and mutual TLS.
Retries require separate request-transmission and replay-safety gates.
Native capacity and feature-overhead qualification resumes after AWS
identity, previous resources and charges are verified.
[Performance and lifecycle](PERFORMANCE-MILESTONE-5.md).
