# Modelo de amenazas

Activos: disponibilidad/memoria del proxy, aislamiento de destinos, credenciales de apps,
confidencialidad de logs y plano administrativo. Límites de confianza: cliente hostil,
configuración administrada localmente, backends explícitamente autorizados, sistema de
archivos, operadores del host. Connectivity es un servicio separado y el
colector mantiene su contrato de envío, alcance de permisos y acuse de recibo.

| Amenaza | Control actual | Riesgo residual / siguiente prueba |
|---|---|---|
| Proxy abierto / SSRF elegido por cliente | Origin-form HTTP/1.1, authority/scheme HTTP/2 validados, Host exacto, destinos solo de configuración, CONNECT deshabilitado | Configuración o DNS comprometidos pueden apuntar a redes internas; no existe filtro de egress/allowlist de IP |
| Request smuggling | Parser Hyper/httparse, framing nuevo al reenviar, sanitización hop-by-hop, rechazo de CL duplicado conflictivo, TE no soportado y Connection sobre framing | Hyper 1.12 elimina CL si viene junto a TE antes del handler: ese caso se canonicaliza, no se rechaza estrictamente. No afirmar cobertura de todas las variantes; fuzzing diferencial pendiente |
| Slowloris / headers gigantes | Timeout de headers, límites de cantidad/buffer, admisión de sockets | Clientes idle consumen una cuota finita; bloques HEADERS HTTP/2 incompletos tras el preface no tienen reloj propio; fairness/ratelimit por origen pendiente |
| Cuerpo infinito / cliente o backend lento | Bytes máximos, timeouts de body, deadline y write-idle downstream, backpressure | Streaming puede cortarse por duración/tamaño configurado; timeout de body al poll, no ACK deadline |
| Saturación / reconexiones | Semáforos, espera acotada y, para HTTP y gateways de aplicación, circuitos opcionales con jitter. El failover acotado sólo se aplica antes de transmitir solicitudes elegibles | La política avanzada queda pendiente en Stream y mail. La capacidad se comparte entre tenants |
| Envenenamiento de pool | HTTP/1.1 lease hasta EOF + upload completo; HTTP/2 permiso por ambas mitades, reset aislado y driver compartido; generaciones inmutables | Validación diferencial de parsers y cierres simultáneos todavía parcial |
| Spoofing Forwarded/traces | Reconstrucción de peer/proto, `traceparent` validado y política explícita de proxies confiables | La exactitud de identidad depende de la lista de confianza y del saneamiento de cabeceras por el balanceador autorizado |
| Secretos en evidencia | Ningún header/body/query, path reducido a prefijo, motivos fijos | Los operadores deben evitar secretos en IDs y prefijos configurados; timestamps e IDs requieren protección |
| Disco lleno o logger colgado | Hilo independiente, cola acotada, drops/errores medidos, flush/reintentos, rotación por rename | Sin garantía lossless; flush no es fsync; caída puede perder lotes |
| Administración expuesta | Bind obligatorio loopback, metrics con token local, no mutadores HTTP, señales por permisos SO | /live y /ready públicos al host; usuario con acceso al proceso/config puede controlarlo; proteger namespace del contenedor |
| Identidad TLS / intermediario | rustls, CA explícita, verificación de identidad y vigencia, handshakes acotados y mTLS configurable. ACME HTTP-01 probado con Pebble | Quedan pendientes OCSP, políticas de revocación e interoperabilidad ACME pública. La protección de las claves y el bundle CA depende del operador |
| Túneles WebSocket persistentes | Handshake RFC6455 validado, sin CONNECT ni upgrades arbitrarios; cuotas retenidas y vida/idle acotados | Backend debe autorizar Origin/usuario y limitar mensajes; compresión desactivada; no inspección de frames |
| Destino de ingesta o ACK falso | HTTPS validado, destino explícito, sin redirects, ACK asociado a batch_id y cantidad; sin transporte por defecto | El receptor autorizado puede mentir sobre persistencia; el contrato local de Connectivity existe; su despliegue y la verificación independiente del servidor siguen pendientes |
| Saturación o caída del colector | Proceso independiente, spool con cuota y checkpoint transaccional, reenvío de lote estable | La retención de JSONL puede vencer mientras se pausa lectura; no hay garantía sin pérdidas |
| Credencial o namespace cruzado | Token en archivo, nunca en spool; ningún campo de log otorga autorización | La autorización y deduplicación por identidad deben existir en un futuro servidor; fixture no prueba aislamiento real |
| Manipulación del estado local | Dueño único, volumen separado, exportación conservadora, detección de fuentes inválidas | Operador/host comprometido puede modificar o borrar eventos y checkpoints; no hay cifrado de spool integrado |
| Abuso HTTP/2: streams, resets y ventanas | SETTINGS y cuotas locales/globales, ventanas/buffers fijos, límites de reset, headers/trailers y plazo duro por stream | No hay rate limit por IP ni certificación contra Rapid Reset; HPACK y framing dependen de la librería h2 fijada en Cargo.lock |
| Degradación o confusión gRPC | HTTP/2 obligatorio en ambos lados, ALPN h2 verificado, trailers/status preservados, plazo del cliente acotado por plazo local | Metadata de aplicación requiere autenticación en el backend; el proxy no interpreta Protobuf ni asegura éxito de negocio |
| Dependencias / supply chain | Cargo.lock, CycloneDX, inventario de licencias, bases de contenedor fijadas por digest y evidencias de cargo-audit y cargo-deny | RUSTSEC-2026-0459 sigue abierto en SQLite del colector. El escaneo está acotado a la versión registrada de la base de avisos |

Las pruebas locales son evidencia de casos concretos, no una certificación de seguridad.

## DNS, cache persistence and certificate management

Configure a trusted recursive DNS service. DNS answers select an IP while
the original TLS hostname and trust store define backend identity.
Applications that permit configured DNS upstreams should constrain origin
network reachability at the deployment boundary.

Cache disk directories and ACME state must reside under service-owned or
operator-controlled parents. Keep directory access private and separate
these files from static document roots. A disk cache starts cold after an
unclean shutdown, preventing a previously persisted object from bypassing
interrupted invalidation. ACME account and certificate private keys are
stored separately from ordinary request data and logs.

The ACME HTTP-01 exception serves only an active order's token for its
exact authorized host. It runs after HTTP framing and path checks.
Managed renewal uses the configured HTTPS directory trust policy.
