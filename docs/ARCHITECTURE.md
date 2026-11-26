# Arquitectura y decisiones

## Componentes

Cliente → TLS opcional/rustls → Hyper → políticas/routing → lease de pool → HTTP/1.1 o HTTP/2 sobre TCP/TLS/Unix → backend.
La respuesta retiene lease y permiso de solicitud mientras se transmite. Telemetría:
productor `try_send` → cola sync acotada → hilo escritor → JSONL local.
Connectivity no participa en el dataplane.

`config.rs` valida; `proxy.rs` aplica políticas; `pool.rs` controla transporte/capacidad;
`body.rs` mueve frames y aplica límites; `transport.rs` limita escrituras bloqueadas;
`tls.rs` carga certificados/CA; `websocket.rs` valida upgrades y controla túneles; `telemetry.rs` persiste; `main.rs` contiene listeners, señales, generaciones y drenaje.

Tokio multihilo ejecuta tareas ligeras. Threads del runtime no son pools por worker.
Cada socket cliente tiene una tarea, cada conexión upstream un driver; ambos tienen límites.
Los workers Gunicorn/Uvicorn son procesos propios del backend y nunca los inicia el proxy.
`TOKIO_WORKER_THREADS` controla threads sin fragmentar capacidad upstream.

## Selección de virtual host y ruta

La preparación de una generación arma el índice de autoridades exactas y,
cuando la configuración los usa, índices separados para comodines DNS y
rutas literales. La precedencia selecciona el host primero y luego la ruta.
Las coincidencias exactas preservan el puerto y el índice radix conserva el
límite de segmento para prefijos. Una generación inválida se rechaza antes
del intercambio. [ADR 0009](adr/0009-virtual-host-and-location-precedence.md).

Las rutas planas pueden combinar coincidencia exacta, prefijos literales,
prefijos que omiten expresiones regulares y expresiones regulares ordenadas.
Los patrones se compilan con límites antes de publicar la generación. Un host
sin patrones omite la evaluación de expresiones regulares. La ruta única con
prefijo segmentado conserva el acceso directo.

Las políticas de encabezados se compilan por ruta. Se aplican después
de retirar los campos de salto. La configuración protege los campos de
identidad, framing, reenvío y negociación de protocolos. Tanto la política
como el mapa resultante tienen presupuestos de tamaño.

## Pool y transporte

Un pool global en memoria, semáforo global de conexiones y semáforo por endpoint.
El driver y la conexión comparten ownership de ambos permisos durante uso, idle y limpieza tras abortar; se
liberan al destruir el transporte, antes de despertar adquirentes. El conjunto JoinSet de
sockets está limitado incluyendo resultados terminados pendientes de recoger. Un gate independiente limita
aperturas simultáneas. Un mutex breve protege listas idle; no se mantiene durante DNS,
connect, envío ni lectura. El refresco DNS usa un permiso de single-flight por endpoint,
no un mutex de datos sobre red. La búsqueda/retirada idle es O(n), acotada por configuración;
optimizar solo después de perfiles de contención. No se declara contención eliminada.

La clave es `(generación, upstream_id)`; cada generación posee una configuración inmutable
completa (address, Host, protocolo HTTP/1.1 o HTTP/2, isolation y TLS opcional con server_name/CA).
Cada nueva generación carga su propio material de confianza y clientes rustls antes del
intercambio. Aunque cambie el contenido de un PEM en la misma ruta, un HUP crea generación
nueva y drena la anterior. Los distintos upstream_id nunca comparten sockets ni sesiones TLS.
mTLS todavía no está admitido. Configuraciones anteriores sin TLS conservan HTTP en claro.

TCP keepalive detecta pares muertos en conexiones TCP, no reutiliza solicitudes HTTP.
HTTP keepalive permite reutilizar transportes ya abiertos. Una conexión TCP nueva sigue
necesitando handshake. Las métricas de conexiones creadas/reutilizadas son exclusivamente
proxy→backend, independientes del keepalive cliente→proxy.

En HTTP/1.1 una conexión vuelve al pool solo tras EOF completo upstream y upload completo. Cancelación,
body limit, timeout, cierre de transporte o upload incompleto destruyen el lease y abortan
su driver. Los bodies no se drenan silenciosamente para recuperar una conexión dudosa.
HEAD/204 y cuerpos vacíos completan explícitamente sin quedar esperando un frame inexistente.

Límites activos+idle globales y por endpoint; idle global/endpoint adicionales. Caducidad
idle y edad total se verifican al adquirir y mediante sweep cada 250 ms. La edad máxima
retira transportes al liberarlos, no corta streams activos. La retirada de destinos por
fallo puede usar backoff fijo o circuitos por endpoint con jitter y recuperación
half-open. Las sondas HTTP, HTTP/2 y gRPC se aplican en forma independiente.
Los reintentos optativos para solicitudes vacías se limitan al establecimiento
de un nuevo transporte antes de enviar la solicitud.

Sin `proxy.dns`, los upstreams HTTP usan el resolver del SO y una caché con
TTL administrativo. Con `proxy.dns`, Hickory resuelve los registros A, AAAA y
CNAME, comparte las consultas concurrentes y retira las conexiones idle que
pierden su dirección. La configuración TLS conserva su identidad original.
[Configuración de DNS y resiliencia](DNS-AND-RESILIENCE.md).

## Backpressure, tiempos y límites

Sin buffers completos de request/response: Hyper pide frames según capacidad de escritura.
Uploads/downloads cuentan bytes y rechazan después del límite; un Content-Length grande
se rechaza antes de conectar. Un exceso detectado tras enviar headers cierra el stream,
no inventa un nuevo status 413 dentro de una respuesta 200 ya iniciada.

`acquire_ms`: capacidad del pool y, por separado, gate de apertura.
`connect_ms`: límite separado de resolución/gate DNS y conexión TCP/Unix; no es un único
presupuesto combinado. `headers_ms`: desde send_request hasta headers finales, incluye
upload si el backend espera el cuerpo. `request_ms` limita adquisición+headers y las
lecturas de body desde aceptación del handler. `stream_idle_ms` limita pausas de body y
escrituras downstream bloqueadas (wrapper IO independiente). `client_headers_ms` limita
lectura de headers HTTP/1.1, el preface y cada bloque HEADERS/CONTINUATION HTTP/2, incluidos trailers. El plazo absoluto del framing se observa independientemente del IO y cierra la conexión completa con sus streams; no aplica al payload DATA ni al idle entre frames sin bloque abierto (ver HTTP2). El servidor termina un downstream que no consume cuando la
escritura deja de progresar. En HTTP/1.1 el deadline de body se evalúa al poll, no es un reloj duro
independiente durante tiempo en buffers del kernel; una escritura atascada queda acotada
por el timeout IO. Un cliente idle también queda sujeto al límite total de sockets.

El cliente que desconecta hace que Hyper descarte el futuro de servicio/body, liberando
el lease; en HTTP/1.1 se aborta su driver, en HTTP/2 se cancela el stream conservando otros streams. Los tests cubren desconexión durante respuesta,
no todos los casos de upload half-close. HTTP/1.1 half-close se desactiva conservadoramente.

Memoria acotada por sockets, solicitudes, conexiones, buffers de headers, frames de Hyper,
cola de eventos y configuración (máximo 1 MiB, 1024 endpoints/rutas). Esto es una cota
estructural, no un límite exacto de RSS del allocator/SO. Use el límite de memoria cgroup
de Compose. Sin tareas por petición en cola ilimitada; no hay buffers de payload para retry.
Resolución del SO puede continuar en un hilo bloqueante de Tokio tras timeout: la espera
asíncrona está acotada; no se promete cancelación del resolver del SO. El runtime tiene un máximo de 16
hilos bloqueantes y shutdown_timeout de 500 ms para no esperar indefinidamente su finalización. La lectura de configuración se limita a 1 MiB+1 y parse/validación se aíslan en
spawn_blocking. La reconstrucción de generaciones está acotada a 1024 rutas/endpoints.

## Dependencias verificadas

Consultadas el 16-11-2026: [Hyper 1.12](https://docs.rs/hyper/1.12.0/hyper/client/conn/http1/),
[Tokio 1.54.1](https://docs.rs/tokio/1.54.1/tokio/) y
[rustls 0.23.46](https://docs.rs/rustls/0.23.46/rustls/).
Hyper es parser/transporte HTTP mantenido, no motor de proxy; se utiliza su API conn para
controlar leases, apertura y pool. Tokio aporta runtime/IO. rustls implementa la
terminación y conexiones TLS mediante tokio-rustls, proveedor criptográfico ring. Cargo.lock fija todas las versiones; `uuid=1.18.1` y
`time=0.3.44` mantienen compatibilidad con Rust 1.88 del host. `time` y `toml` no son las
últimas ramas disponibles; actualización pendiente de revisar MSRV, avisos y pruebas.

El Dockerfile compila en BUILDPLATFORM y usa toolchain/linker cruzado para TARGETARCH.
El primer intento de compilar todo bajo emulación amd64 falló por SIGSEGV de cc al
enlazar serde_core; se conserva el registro. La estrategia final evita ejecutar el
compilador/linker x86 sobre QEMU, sin atribuir ese fallo al proxy.

## TLS y WebSocket en el incremento 2

Lectura y parsing de certificados/claves/CA ocurren en spawn_blocking al inicio/recarga;
no durante un handshake. Ficheros PEM acotados a 1 MiB. `tls_handshake_ms` limita cada
handshake de entrada o upstream. El de entrada ocurre dentro de la cuota de clientes.
El de upstream conserva las cuotas de conexión y apertura; el deadline de solicitud
puede terminarlo antes. La validación rustls incluye cadena, vigencia, identidad y firma.
No se desactiva la validación; confianza explícita por upstream, ALPN elegido según el protocolo configurado.

Hyper con `with_upgrades()` transfiere ambos transportes y bytes ya leídos al túnel.
El driver HTTP puede finalizar al hacer upgrade: la conexión del lease sigue reteniendo
los permisos global/endpoint mediante Arc hasta que se destruye el transporte del túnel.
Un JoinSet acotado por la cuota de solicitudes rastrea los túneles; no quedan tareas de
upgrade desligadas del apagado. El permiso de cliente también vive hasta terminar.
La recarga permite que WebSockets existentes terminen con su generación anterior;
vida máxima y apagado siguen acotados. Los nuevos túneles usan la nueva generación.

Copy bidireccional con 8 KiB por sentido: datos recibidos/escritos reinician el reloj
común de inactividad. Cualquier error, cancelación, idle o vida máxima descarta ambos
transportes. EOF propaga half-close y espera el otro sentido dentro de estos límites.
Close/ping/pong/fragmentación pasan transparentemente; el proxy no valida payloads ni
aplica políticas de Origin/autorización de aplicación. Compresión/extensiones desactivadas.
No se mide un cierre RFC6455 completo: outcome complete significa EOF del transporte.
Referencias: [Hyper upgrades](https://docs.rs/hyper/1.12.0/hyper/upgrade/),
[RFC6455](https://www.rfc-editor.org/rfc/rfc6455.html).

## Colector independiente en el incremento 3

`cauce-collector` se distribuye junto al proxy, pero tiene su propio proceso,
configuración y estado. El proxy no enlaza un cliente de ingesta en su ruta de
solicitudes y funciona sin iniciar el colector. La lectura local usa lotes y líneas
acotados; el offset se confirma en la misma transacción SQLite que los eventos.
La entrega posterior retira un lote solo después de un ACK válido.

El volumen persistente y la retención de JSONL determinan la ventana de recuperación.
Cuota llena pausa el tail, mientras el proxy continúa su política de rotación. El
transporte experimental usa un único intento en curso con deadline y backoff; no
crea una tarea o cola ilimitada por evento. Exportar permite acceder a los lotes
sin ejecutar un servicio remoto. Contrato, límites y operación en [COLLECTOR](COLLECTOR.md).

La elección de rollback journal evita el mantenimiento de un WAL independiente.
SQLite documenta las garantías de [synchronous EXTRA](https://www.sqlite.org/pragma.html#pragma_synchronous)
y la cuota de páginas [max_page_count](https://www.sqlite.org/pragma.html#pragma_max_page_count).
Una prueba de SIGKILL de proceso no equivale a una prueba de corte de energía de
la VM o del almacenamiento. No se declara una garantía superior a la del sistema
de archivos y su implementación de fsync.

## HTTP/2 y gRPC en el incremento 4

Hyper auto selecciona HTTP/1.1 o HTTP/2 cleartext por el preface; con TLS manda ALPN. Un wrapper impone el plazo del preface incluso durante el reconocimiento inicial. El executor de streams aplica un reloj independiente a `request_ms`, que `grpc-timeout` puede acortar: al vencer termina solo ese stream aunque la ventana de salida esté a cero. Para gRPC intenta entregar status 4 y reserva hasta 100 ms para enviarlo antes del reset duro; no extiende el trabajo ni el plazo reenviado. Los timers de inactividad de body siguen dependiendo del poll; no se declara un timer independiente de idle por ventana.

HTTP/2 comparte un sender por socket. Un permiso por stream y el contador least-inflight viven hasta terminar ambas mitades mediante un guard compartido entre upload y respuesta. Cancelar una no aborta el driver compartido. Conexiones retiradas por GOAWAY, edad o generación dejan de recibir nuevos streams; los streams existentes retienen el transporte y sus permisos físicos hasta finalizar. El pool mantiene cuotas físicas separadas de las cuotas de streams. No reintenta una petición al recibir GOAWAY.

Las ventanas de recepción son fijas, el buffer de envío por stream está acotado y la ventana del peer limita el envío en wire. El límite local por conexión también acota solicitudes que Hyper puede esperar por SETTINGS. Headers y trailers tienen límites de cantidad y bytes; se rechazan trailers de framing/routing/hop. gRPC conserva metadata, framing de mensajes y status trailers sin deserializar Protobuf ni descomprimir. Detalles, opciones y exclusiones en [HTTP2](HTTP2.md).

Cada hold HTTP/2 tiene también un watchdog de plazo upstream, cancelado al destruir el hold y sin referencia fuerte circular. Si vence con un upload detenido tras respuesta temprana, retira el socket de admisión y espera a que terminen o venzan los otros holds antes de abortar el driver. Este fallback conserva streams hermanos vigentes y evita retener capacidad indefinidamente cuando Hyper no expone cancelación individual del upload ya despachado.

## Instrumentación y pruebas adversariales en el incremento 6

Los permisos de clientes y solicitudes se observan a partir de los semáforos
reales. Un guard RAII cuenta los futuros HTTP/2 desde su programación hasta Drop;
se crea antes de spawn para incluir abortos de tareas que nunca llegaron a poll.
El contador de streams upstream vive exactamente lo que `StreamHold`, compartido
entre upload y respuesta. No se decrementa solo porque una respuesta temprana
haya terminado ni porque el watchdog retire lógicamente el stream de admisión.

El harness libFuzzer incluye `src/transport.rs` y activa únicamente los hooks de
prueba mediante la feature `fuzzing`; no mantiene otra copia del parser y no se
incluye en la imagen de producción. Los controles Docker combinan gauges del
proxy, actividad observada por el backend y recursos del proceso/contenedor.
Las mediciones y sus presupuestos finitos constan en [VALIDATION-6](VALIDATION-6.md).

## Índice inmutable de rutas

Cada generación construye un mapa de autoridades exactas. Un árbol radix comprimido por
autoridad vincula los prefijos al vector de rutas de la misma generación. La búsqueda
conserva el prefijo válido más largo y aplica el límite de segmento. Se mantiene la ruta
rápida de comparación directa cuando la configuración contiene una sola ruta. Véase
[ADR 0001](adr/0001-compiled-routes.md) y la [evidencia de rendimiento](PERFORMANCE.md).

La preparación de recarga y el mantenimiento del pool se ejecutan fuera del bucle de
accept. El bucle activa únicamente candidatos completos. Un barrido concurrente usa
la generación observada como límite inferior. [ADR 0002](adr/0002-prepared-reload.md).

## Registros de acceso opcionales

El dataplane construye eventos y diagnósticos por intento cuando el escritor JSONL
está habilitado. Todas las solicitudes generan un ID de correlación y aplican la
misma validación de `traceparent`. La contabilidad de métricas y los permisos de
solicitud, conexión y stream viven fuera del evento. La finalización de HTTP y los
túneles conservan esa separación. [ADR 0003](adr/0003-optional-access-events.md).

## Instrumentación de rendimiento

El generador Rust del laboratorio mantiene conexiones HTTP/1.1 y HTTP/2
persistentes y programa llegadas con un reloj monotónico. La cola de despacho
es acotada. El generador conserva hora programada, llegada, despacho,
finalización y resultado de cada oferta. Los controles directos validan
generador y backend, y el muestreo Linux utiliza un proceso separado.
[Metodología](BENCHMARK-METHODOLOGY.md).

Para un upstream HTTP/2, un cuerpo que Hyper ya considera terminado se
envía con `END_STREAM` en los HEADERS iniciales. Los cuerpos con datos o
trailers pendientes conservan el guard de subida y sus límites. El lease
de respuesta y el watchdog siguen abarcando el intercambio.
[ADR 0005](adr/0005-http2-empty-upstream-uploads.md).

## Políticas HTTP compiladas

La preparación de cada generación valida los rangos de confianza y compila
las rutas, las acciones y los grupos de balanceo. El dataplane separa la
dirección del socket de la identidad de cliente. Los saltos internos tienen
un límite fijo y la ruta final aplica los controles de acceso y admisión.
Los grupos de balanceo mantienen un estado independiente por ruta.

Las zonas de admisión pertenecen al proceso y conservan su estado al
recargar las rutas. La autorización concurrente dura hasta la finalización
de la subida y de la respuesta, incluso para flujos HTTP/2 y túneles.
[Orden y límites de las políticas](adr/0011-compiled-gateway-policies.md).

## Identidad TLS y salud de upstreams

Cada generación prepara sus certificados, raíces de confianza y estado
de reanudación TLS antes de publicarse. SNI selecciona un certificado de
nombre exacto o el certificado predeterminado. La identidad HTTP sigue
determinando la ruta. [ADR 0012](adr/0012-gateway-tls-identity.md).

Las comprobaciones activas usan conexiones independientes y un
planificador limitado por generación. El modo gRPC verifica el mensaje
y el estado de cierre dentro del plazo de la comprobación. No se
inicia un planificador sin comprobaciones configuradas.
[Configuración de salud](CONFIGURATION.md#active-upstream-health).

## Contenido estático, caché y codificación

La ruta compilada selecciona contenido local o el proxy. Los controles
de acceso y admisión preceden al contenido. Los descriptores de directorio
confinan la entrega estática y la lectura conserva el permiso hasta
terminar, incluso si se cancela la solicitud.

Cada generación posee las cachés de sus rutas. La política HTTP evalúa
privacidad, frescura y variantes. El almacén publica únicamente cuerpos
completos y comparte su contenido entre respuestas. La codificación
se aplica después de seleccionar la representación.
[ADR 0013](adr/0013-content-and-cache.md).

## Listeners de capa 4

`stream/` prepara sockets y grupos TCP/UDP antes de aceptar tráfico.
Cada listener controla sus sesiones y su drenaje. TCP preserva el cierre
por dirección y admite upstreams Unix. UDP conserva límites de datagrama
y usa un socket conectado por asociación. PROXY y ClientHello se procesan
en el listener TCP antes de seleccionar el upstream. Las rutas HTTP
siguen su propio dataplane. [ADR 0014](adr/0014-bounded-stream-listeners.md).

## HTTP sobre TCP y QUIC

`http_tcp.rs` concentra Hyper, TLS opcional y los límites HTTP/1.1 y
HTTP/2. `http3/` adapta Quinn y h3 al mismo motor de políticas con cuerpos
que conservan frames DATA y trailers. El listener QUIC comparte admisión
y certificados con HTTP, y sus ventanas, sesiones, tareas y deadlines
se controlan independientemente. La entrega Alt-Svc se decide al
establecer una conexión TCP. [ADR 0015](adr/0015-quinn-http3-transport.md).

## Gateways de aplicación y correo

Cada generación compila el protocolo del upstream. Las rutas HTTP envían
FastCGI, uWSGI o SCGI por un adaptador con framing propio. Estos adaptadores
reutilizan la selección de endpoints, las cuotas del pool y el filtrado de
respuestas HTTP. FastCGI retorna un socket exclusivo al pool tras completar
el cuerpo de entrada y validar `END_REQUEST`. uWSGI y SCGI cierran el
socket al terminar cada intercambio. Las rutas HTTP/1.1 y HTTP/2 conservan
sus transportes. [ADR 0016](adr/0016-application-upstream-leases.md).

Los listeners SMTP, IMAP y POP3 compilan sus políticas y certificados
al iniciar. Cada sesión negocia TLS, consulta un servicio HTTP de
autenticación y abre una conexión exclusiva autorizada. Tras verificar
el login del backend, el relay compartido de TCP mantiene los límites
de deadline y backpressure. El cierre deja drenar sesiones autenticadas
dentro del plazo global. [ADR 0017](adr/0017-tls-gated-mail-listeners.md).

## Resolución DNS compartida

Con `proxy.dns`, HTTP y los gateways de aplicación comparten Hickory con
las sondas, Stream y el servicio HTTP de autenticación de correo. Las
conexiones del pool retienen un token de dirección. Una publicación DNS
retira los tokens de las direcciones que salieron del conjunto. Los
arriendos ya activos conservan su socket y su configuración TLS.
[Configuración y límites](DNS-AND-RESILIENCE.md).
