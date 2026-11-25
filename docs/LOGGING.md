# Contrato JSONL 1.1

El proxy 0.4 emite `schema_version = "1.1"` y `protocol` indica el protocolo entrante (`HTTP/1.1` o `HTTP/2`). El esquema y colector 0.4 también aceptan eventos 1.0 HTTP/1.1; un colector 0.3 rechaza 1.1: actualice ambos ejecutables juntos.

Cada evento terminado ocupa una línea UTF-8; esquema estricto en
`schemas/access-log.schema.json`. Ejemplos reales en `docs/evidence/`. Son observaciones
del proxy, no spans internos de la aplicación. Esquema de eventos de acceso separado de
los mensajes operativos de arranque/recarga de stderr, también JSONL.

Identificadores UUID de evento, instancia y solicitud generados localmente; generación de
configuración monotónica por proceso. `connection_id` solo es único dentro de instance_id.
`traceparent` v00 se acepta solo con formato hexadecimal lowercase, IDs no nulos y longitud
exacta; otros valores se eliminan. No acredita identidad ni se confía para autorización.
No se registra tracestate, headers, Cookie, Authorization, queries ni cuerpos. Se reemplazan
los X-Forwarded-* y Forwarded recibidos, sin confiar en ningún peer en este incremento.

`path` es el prefijo configurado, no la URL concreta ni la query. Ruta desconocida: null.
Métodos no estándar se agrupan en OTHER. IDs de ruta/upstream configurados y acotados;
no hay etiquetas Prometheus por request/trace. Incluso logs omitidos por saturación no
alteran el tráfico. Hay que proteger/rotar volúmenes: timestamps e IDs siguen siendo datos
operativos sensibles.

## Duraciones

Todas las duraciones numéricas usan `Instant` monotónico en milisegundos fraccionarios.
`timestamp` usa reloj de pared UTC al comenzar handler (puede corregirse por NTP).

| Campo | Intervalo medido |
|---|---|
| duration_ms | Entrada al handler hasta EOF observado, error o drop del body; no incluye lectura inicial de headers cliente ni confirmación TCP de entrega al cliente |
| attempts[].pool_wait_ms | Espera por capacidad/lista de conexiones; excluye creación de transporte y gate de apertura |
| connect_gate_ms | Espera por el semáforo de nuevas aperturas; solo conexiones nuevas |
| dns_ms | Gate DNS + consulta de caché/lookup del SO hasta conjunto disponible; solo conexiones TCP nuevas |
| tcp_ms | Inicio connect TCP hasta socket conectado; solo TCP nuevo y exitoso |
| tls_ms | Inicio handshake TLS upstream hasta conexión verificada; null para HTTP en claro, reutilización o fallo sin timing disponible |
| headers_ms | Inicio send_request hasta headers finales upstream; puede incluir upload, red, scheduling, colas, workers y aplicación |
| transfer_ms | Recepción de headers upstream hasta EOF/error/drop del body observado por proxy |

Cero significa medido y casi instantáneo (puede redondear a cero); nunca rellena un valor
faltante. Null significa no aplicable o desconocido: `reused=true` implica DNS/TCP/TLS no
aplicables; `reused=null` implica adquisición fallida sin conexión conocida. Unix implica
DNS/TCP no aplicables. Un fallo de conexión conserva intervalos parciales como desconocidos
en esta versión, no reconstruye timings; `reason` identifica fase cuando se conoce.
La espera total en un error no se presenta falsamente como espera de pool.

Un único intento máximo. `status` registra el status enviado en headers; una transferencia
200 incompleta sigue teniendo status 200 con outcome `cancelled`, `body_error`, `body_limit`
o `stream_timeout`. `outcome=complete` indica EOF en proxy, no recibo confirmado por cliente.
`request_bytes` y `response_bytes` cuentan payload observado en frames; pueden incluir el
último frame que excedió el límite, y no son wire bytes ni ACKed bytes.
`backpressure_observations=null`: backpressure se implementa, pero no se instrumenta un
contador que distinguiría correctamente red lenta de scheduling. No se inventa un cero.
Errores de transporte/headers usan motivos de cardinalidad acotada, sin URLs o errores OS
que puedan contener datos sensibles. `error_direction=upstream_or_downstream` reconoce
que no se identifica siempre el lado causante del fallo. No atribuir TTFH solo a aplicación
ni deducir saltos de Internet a partir de estos tiempos HTTP.

## Escritura y pérdidas

Cola de `logs.queue` eventos acotada; `try_send` descarta el nuevo evento al llenarse,
incrementando `*_log_dropped_total`. Un hilo dedicado realiza batches de hasta 128 y flush
tras batch o 100 ms idle. Ningún lock de petición abarca operaciones de disco.
Se rota antes de exceder `rotate_bytes`, conservando archivo actual y hasta
`retained_files` archivos numerados. Errores de abrir/escribir/rotar: contador IO, descarte
sin bloquear tráfico, reintento al próximo lote. Nunca se sigue ampliando deliberadamente
un archivo cuya rotación falló. Una línea puede exceder la cuota si se configura por debajo
del tamaño del evento; el mínimo es 4096 y los campos están acotados, pero no es una cuota
exacta de filesystem. Retención por tamaño/conteo, no días.

Desde **0.9.0**, al reabrir el archivo se conserva hasta el último salto de línea
completo y se elimina únicamente el fragmento final incompleto. Tras un error de
escritura/flush se descarta el buffer sin reintento implícito en su destructor;
la siguiente apertura repara el fragmento y entrega ese inode a la rotación
antes de escribir en un archivo nuevo. Así el colector puede terminar la
generación reparada sin confundirla con una reescritura del origen. La rotación
pendiente se mantiene si falla el rename; los retries no anexan al inode viejo.
También se rota el archivo existente una vez al arrancar el proxy, para cubrir
un reinicio entre reparación y rename. Se respeta `retained_files`, incluido
su descarte si vale cero; fallos/reinicios repetidos pueden consumir retención.
Una reapertura ordinaria sin fragmento incompleto no provoca otra rotación.
El tamaño se vuelve a leer del archivo reparado. Esto evita concatenar JSON posterior con
una escritura parcial de `ENOSPC`. Un flush fallido previo a rotación impide
rotar ese lote. El archivo necesita permisos de lectura y escritura y un único
escritor propietario; no compartir su path entre proxies ni usar copytruncate.
No se alteran registros completos ni se promete reparar corrupción externa.
Prueba con disco lleno real, reproducción del defecto 0.7.0 y recuperación en
[VALIDATION-9](VALIDATION-9.md). El uso de `BufWriter::into_parts` evita el flush
del destructor, según la [documentación de Rust](https://doc.rust-lang.org/std/io/struct.BufWriter.html#method.into_parts).

No fsync por evento; un flush no garantiza supervivencia a caída eléctrica. Errores de
flush pueden perder un lote cuyo número exacto no se conoce: IO errors lo señalan, el
contador dropped es una cota inferior, no reconciliación exacta. Apagado espera hasta
500 ms al logger; pendientes pueden perderse. Un disco que cuelga puede inmovilizar el
hilo escritor, no las peticiones: cola acaba saturándose, con pérdidas y memoria acotada.
La cuota cgroup/volumen es responsabilidad del operador. Colector opcional disponible en [COLLECTOR](COLLECTOR.md); deduplicación de un receptor real pendiente. La persistencia del colector no recupera eventos perdidos antes de escribir el JSONL.

Peticiones rechazadas por el parser de Hyper antes del handler y sockets rechazados antes
de parsear no producen este evento de acceso completo: no hay una solicitud estructurada
fiable. La instrumentación de eventos de conexión/parser más detallada queda pendiente.

Cancelaciones antes de recibir headers upstream pueden no emitir evento de acceso en v0.1, aunque el drop del futuro sí cancela la adquisición/transporte. Añadir un guard de observación para todas las fases está pendiente.

## Túneles WebSocket

Los campos de túnel permanecen compatibles con 1.0; los eventos nuevos usan versión 1.1. Un evento con `status=101` se emite al terminar
el túnel. `protocol=HTTP/1.1` describe el handshake; `duration_ms` incluye su vida,
`transfer_ms` va desde headers 101 hasta terminación. Los bytes de estos eventos cuentan
bytes de frames escritos a cada transporte, incluidos headers de frames y control;
no equivalen al payload descomprimido, TLS en wire ni bytes confirmados por ACK.
`complete` indica EOF de ambos transportes, no certifica intercambio de Close RFC6455.
Timeouts usan `stream_timeout` con reason websocket_idle_timeout/websocket_max_age;
un aborto de apagado usa cancelled/tunnel_cancelled. Fallos IO tienen body_error.
El tiempo TLS de entrada no se mezcla con attempts[].tls_ms (siempre upstream).
Un handshake TLS de entrada fallido ocurre antes de una solicitud HTTP y no genera
un evento de acceso; la instrumentación detallada de conexiones sigue pendiente.

## HTTP/2 y gRPC

Varios eventos concurrentes pueden compartir `connection_id`: es el socket upstream, no el stream. `reused=true` incluye multiplexación simultánea. `headers_ms` puede incluir espera del límite SETTINGS del peer; `pool_wait_ms` mide la cuota local. No se registra el ID de stream HTTP/2 ni metadata/trailers gRPC. `status` sigue siendo HTTP: un RPC fallido puede tener HTTP 200 y `grpc-status` no cero. `outcome=complete` describe la transferencia, no éxito del RPC. Un deadline duro de HTTP/2 puede aparecer como `cancelled` al descartar el stream; no se inventa un status tras enviar headers.

Un plazo gRPC rechazado antes de headers usa HTTP 200 con `grpc-status: 4`; el
evento conserva `outcome = rejected` y `reason = request_deadline`, por lo que
HTTP 200 no significa éxito. Un timeout durante el cuerpo queda como
`stream_timeout`; con ventana bloqueada el cierre duro puede quedar como
`cancelled`. No se registra el mensaje gRPC ni su metadata.
