# Modelo de amenazas — incremento 4

Activos: disponibilidad/memoria del proxy, aislamiento de destinos, credenciales de apps,
confidencialidad de logs y plano administrativo. Límites de confianza: cliente hostil,
configuración administrada localmente, backends explícitamente autorizados, sistema de
archivos, operadores del host. Connectivity es un servicio separado todavía no integrado.

| Amenaza | Control actual | Riesgo residual / siguiente prueba |
|---|---|---|
| Proxy abierto / SSRF elegido por cliente | Origin-form HTTP/1.1, authority/scheme HTTP/2 validados, Host exacto, destinos solo de configuración, CONNECT deshabilitado | Configuración o DNS comprometidos pueden apuntar a redes internas; no existe filtro de egress/allowlist de IP |
| Request smuggling | Parser Hyper/httparse, framing nuevo al reenviar, sanitización hop-by-hop, rechazo de CL duplicado conflictivo, TE no soportado y Connection sobre framing | Hyper 1.12 elimina CL si viene junto a TE antes del handler: ese caso se canonicaliza, no se rechaza estrictamente. No afirmar cobertura de todas las variantes; fuzzing diferencial pendiente |
| Slowloris / headers gigantes | Timeout de headers, límites de cantidad/buffer, admisión de sockets | Clientes idle consumen una cuota finita; bloques HEADERS HTTP/2 incompletos tras el preface no tienen reloj propio; fairness/ratelimit por origen pendiente |
| Cuerpo infinito / cliente o backend lento | Bytes máximos, timeouts de body, deadline y write-idle downstream, backpressure | Streaming puede cortarse por duración/tamaño configurado; timeout de body al poll, no ACK deadline |
| Saturación / reconexiones | Semáforos de clientes/requests/conexiones/aperturas, espera acotada, backoff pasivo, cero retries | No jitter exponencial ni circuit breaker completo; sin aislamiento de capacidad por tenant |
| Envenenamiento de pool | HTTP/1.1 lease hasta EOF + upload completo; HTTP/2 permiso por ambas mitades, reset aislado y driver compartido; generaciones inmutables | Validación diferencial de parsers y cierres simultáneos todavía parcial |
| Spoofing Forwarded/traces | Eliminar todo Forwarded/X-Forwarded-*, reconstruir peer/proto, validar traceparent | No hay lista de proxies confiables en v0.1; despliegues tras otro proxy perderán IP original |
| Secretos en evidencia | Ningún header/body/query, path reducido a prefijo, motivos fijos | Los operadores deben evitar secretos en IDs y prefijos configurados; timestamps e IDs requieren protección |
| Disco lleno o logger colgado | Hilo independiente, cola acotada, drops/errores medidos, flush/reintentos, rotación por rename | Sin garantía lossless; flush no es fsync; caída puede perder lotes |
| Administración expuesta | Bind obligatorio loopback, metrics con token local, no mutadores HTTP, señales por permisos SO | /live y /ready públicos al host; usuario con acceso al proceso/config puede controlarlo; proteger namespace del contenedor |
| Identidad TLS / intermediario | rustls, CA explícita, verificación de server_name/vigencia/firma; handshake limitado | mTLS, ACME, OCSP/revocación personalizada pendientes; proteger las claves y el bundle CA |
| Túneles WebSocket persistentes | Handshake RFC6455 validado, sin CONNECT ni upgrades arbitrarios; cuotas retenidas y vida/idle acotados | Backend debe autorizar Origin/usuario y limitar mensajes; compresión desactivada; no inspección de frames |
| Destino de ingesta o ACK falso | HTTPS validado, destino explícito, sin redirects, ACK asociado a batch_id y cantidad; sin transporte por defecto | El receptor autorizado puede mentir sobre persistencia; el contrato local de Connectivity existe; su despliegue y la verificación independiente del servidor siguen pendientes |
| Saturación o caída del colector | Proceso independiente, spool con cuota y checkpoint transaccional, reenvío de lote estable | La retención de JSONL puede vencer mientras se pausa lectura; no hay garantía sin pérdidas |
| Credencial o namespace cruzado | Token en archivo, nunca en spool; ningún campo de log otorga autorización | La autorización y deduplicación por identidad deben existir en un futuro servidor; fixture no prueba aislamiento real |
| Manipulación del estado local | Dueño único, volumen separado, exportación conservadora, detección de fuentes inválidas | Operador/host comprometido puede modificar o borrar eventos y checkpoints; no hay cifrado de spool integrado |
| Abuso HTTP/2: streams, resets y ventanas | SETTINGS y cuotas locales/globales, ventanas/buffers fijos, límites de reset, headers/trailers y plazo duro por stream | No hay rate limit por IP ni certificación contra Rapid Reset; HPACK y framing dependen de la librería h2 fijada en Cargo.lock |
| Degradación o confusión gRPC | HTTP/2 obligatorio en ambos lados, ALPN h2 verificado, trailers/status preservados, plazo del cliente acotado por plazo local | Metadata de aplicación requiere autenticación en el backend; el proxy no interpreta Protobuf ni asegura éxito de negocio |
| Dependencias / supply chain | Cargo.lock, lista de licencias, versiones registradas, no shell remoto en runtime, sin GitHub Actions | No se ejecutó auditoría externa de vulnerabilidades; imágenes base por tags pueden cambiar |

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
