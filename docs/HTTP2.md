# HTTP/2 y gRPC — incrementos 4 a 6

El listener admite HTTP/1.1 y HTTP/2. Con TLS negocia mediante ALPN; en claro
reconoce el preface HTTP/2 (prior knowledge). No implementa Upgrade h2c,
CONNECT, RFC8441, gRPC-Web ni HTTP/3. WebSocket sigue usando HTTP/1.1 en ambos lados.
La administración permanece HTTP/1.1 en loopback.

## Configuración

Los archivos existentes siguen siendo válidos: los upstreams predeterminados usan
HTTP/1.1 y el listener admite ambos protocolos. Ejemplo de opciones globales:

```toml
[http2]
enabled = true
max_concurrent_streams = 128
initial_stream_window_bytes = 65535
initial_connection_window_bytes = 1048576
max_send_buffer_bytes = 65536
max_pending_accept_reset_streams = 32
max_local_error_reset_streams = 32
```

`enabled` controla entrada; el protocolo del backend es explícito e independiente.
Cada upstream añade `protocol = "http2"` y `streams_per_connection = 128`.
Su `address` sigue siendo `host:puerto` o `unix:/ruta.sock`; no es una URL.
Sin `tls` se usa prior knowledge. Con `tls = { server_name = "backend.example",
ca_file = "/etc/cauce/ca.pem" }` se verifica CA/nombre/SNI y se exige ALPN `h2`.
Un backend sin h2 falla cerrado; no se repite ni se degrada la solicitud a HTTP/1.1.
Los ejemplos completos están en [examples/http2](../examples/http2/).

El `host` de ruta corresponde a `:authority`, incluido puerto cuando está presente.
Se rechaza conflicto entre Host y `:authority`. El proxy construye la URI upstream
con el `host` configurado, preserva path/query y asigna `:scheme` según TLS real.
No obtiene destinos de campos proporcionados por el cliente.

HTTP ordinario admite traducción HTTP/1.1 ↔ HTTP/2. Para gRPC, ambos lados deben
ser HTTP/2: entrada HTTP/1.1 produce 505; una ruta gRPC hacia HTTP/1.1 produce 502.
El cliente debe enviar su autoridad al host de ruta exacto, no a una IP alternativa.

## Cuotas y ciclo de vida

| Opción | Alcance |
|---|---|
| `limits.clients` | Sockets entrantes, incluidos TLS/idle |
| `limits.requests` | Solicitudes globales; permanece ocupado mientras viva cualquiera de sus cuerpos |
| `http2.max_concurrent_streams` | SETTINGS por conexión entrante; 1–10000 |
| `upstreams[].streams_per_connection` | Solicitudes admitidas/esperando por socket upstream; 1–10000 |
| SETTINGS del backend | Límite adicional de streams abiertos en wire; Hyper espera capacidad |
| `limits.connections`, upstream `connections` | Conexiones físicas, incluyendo activas, idle y transportes que drenan |
| `limits.connecting` | Aperturas simultáneas |
| `initial_stream_window_bytes` | Ventana de recepción por stream, 16 KiB–16 MiB |
| `initial_connection_window_bytes` | Ventana de recepción por conexión, 65535–64 MiB |
| `max_send_buffer_bytes` | Buffer de envío por stream, 16 KiB–1 MiB |
| Límites de reset | Cola de resets pendientes y errores locales, cada uno 1–1024 |

Las opciones de ventanas/buffers/reset se aplican al servidor y cliente HTTP/2;
las ventanas adaptativas están desactivadas. Se mantienen límites de headers,
trailers, bytes de body, espera de pool y solicitudes. Las cuotas son estructurales,
no una promesa de RSS exacto: use el límite de memoria del contenedor.

El pool multiplexa streams sobre sockets existentes. Una conexión nueva espera
los primeros SETTINGS antes de enviar streams; si el peer omite el límite, rige
la cuota local. Esto evita exceder el límite remoto durante una ráfaga en frío. Una cancelación RST_STREAM
libera su stream al finalizar ambas mitades, conserva otros streams y no retira
el endpoint por ese error aislado. Fallos de conexión al abrir sí activan retirada
pasiva. `least_inflight` cuenta streams, no sockets. No hay reintentos automáticos.

GOAWAY impide nuevos streams en la conexión retirada; un reemplazo necesita un
hueco en las cuotas físicas. Con `connections = 1`, puede esperar a que drenen los
streams anteriores hasta `acquire_ms`. Una carrera con GOAWAY puede fallar una
solicitud; no se reproduce su body. HUP separa generaciones, y SIGTERM envía GOAWAY
al frontend y permite completar streams hasta `shutdown_ms`.
Cambiar `[http2]` o límites globales requiere reinicio. Cambiar protocolo, cuotas
por upstream o TLS de un backend permite HUP con la generación anterior drenando.
La reducción de conexiones HTTP/2 idle se aplica mediante sweep cada 250 ms;
una conexión recién liberada puede reutilizarse durante ese intervalo. Con cuota
idle cero se retira al liberar el último stream. Las cuotas de conexiones físicas
se respetan durante esa gracia.

## Semántica gRPC y tiempos

Se transportan unary, client streaming, server streaming y bidi sin interpretar
Protobuf. Metadata inicial/final, trailers, `grpc-status`, `grpc-message`, datos
binarios y framing comprimido pasan como bytes/headers; no se añade compresión.
`TE: trailers` se conserva. Los trailers de framing, routing o salto se rechazan.

`grpc-timeout` acepta 1–8 dígitos y H/M/S/m/u/n; se aplica el menor entre el plazo
del cliente y `request_ms`. Antes de enviar se reescribe con el presupuesto
restante, redondeado hacia arriba a la unidad representable. No se registra ese
header ni los mensajes. Los plazos locales siguen aplicándose a streams largos:
ajuste `request_ms`, `stream_idle_ms` y `max_body_bytes` a la aplicación.

Al vencer el plazo gRPC, antes de headers se responde HTTP 200 con `grpc-status: 4`
(trailers-only); durante el body se emite ese status en trailers. Se reserva una
gracia máxima de 100 ms para entregar el status terminal, sin extender el plazo
de trabajo ni el `grpc-timeout` enviado al backend.

El executor impone el plazo absoluto de cada stream entrante aunque Hyper deje de
pedir body por una ventana a cero. Si no puede entregar el status, lo termina con
reset al acabar esa gracia, sin cerrar hermanos. Para HTTP ordinario no hay gracia. El
timeout de inactividad de body se observa al poll; no es un reloj independiente
para cada ventana bloqueada. El wrapper IO limita escrituras bloqueadas a nivel
de socket, y por tanto un fallo de transporte afecta todos sus streams.

Un watchdog adicional conserva el plazo upstream incluso si una respuesta final
temprana termina antes que su upload. Hyper no expone un handle para resetear
ese upload cuando ya entregó los headers: al vencer se retira la conexión de
admisión, se permite terminar a los streams hermanos aún vigentes y se cierra el
transporte cuando todos terminaron o vencieron. Los permisos físicos y de stream
permanecen retenidos hasta destruirlo; la limpieza puede esperar el presupuesto
restante de esos hermanos. No se aborta una solicitud vigente para recuperar el
slot de otra ni se acumulan uploads abandonados sin límite temporal.

### HEADERS incompletos: plazo de entrada

Desde 0.5, `limits.client_headers_ms` limita el preface inicial, los headers
HTTP/1.1 y cada bloque HEADERS/CONTINUATION HTTP/2 de entrada, incluidos trailers.
Un observador transparente lee el framing después de TLS; conserva solo nueve
bytes del frame header y contadores, sin copiar payloads ni interpretar HPACK.
Hyper sigue validando el protocolo y el tamaño de los campos.

El reloj empieza al observar el primer byte de los nueve del frame header. Es un
plazo absoluto: recibir más bytes no lo reinicia. Un HEADERS mantiene ese mismo
plazo durante todo su payload, los intervalos entre CONTINUATION y sus payloads,
hasta recibir el último byte del frame con END_HEADERS del mismo stream.
END_STREAM no termina el bloque. END_HEADERS por sí solo tampoco basta si falta
payload (incluidos prioridad/padding). Un frame header incompleto también vence,
aunque todavía no se conozca su tipo.

No hay reloj de headers entre frames completos sin bloque abierto. Una vez leído
un frame header completo de DATA u otro tipo sin bloque abierto, se desactiva el
reloj de headers durante su payload: los plazos de body/solicitud siguen siendo
independientes. Este cambio no añade un timeout idle general a conexiones HTTP/2.
El reloj usa el instante en que el proxy lee los bytes, no su llegada física al NIC.

Un watchdog independiente continúa aunque Hyper deje de leer por backpressure;
también sigue activo durante el drenaje por SIGTERM. Vencer cierra todo el
transporte TCP/TLS y cancela las tareas de sus streams, incluidos los hermanos.
No se promete GOAWAY ni respuesta 408/`grpc-status` para un bloque incompleto.
Esto difiere de un deadline de solicitud o RST_STREAM aislado: la compresión y
las CONTINUATION del bloque comparten estado/orden de conexión
([RFC 9113 §4.3](https://www.rfc-editor.org/rfc/rfc9113.html#section-4.3)).
Los demás sockets permanecen operativos. La liberación upstream conserva los
límites del pool y su watchdog descritos arriba; no se afirma limpieza instantánea
de uploads bloqueados que Hyper ya hubiera separado de una respuesta terminada.

El contador Prometheus `<name>_http2_header_timeouts_total` registra un cierre por
conexión vencida (incluye frame headers parciales después del preface). No se
fabrica un evento de acceso para una solicitud que nunca pudo parsearse. Los
streams hermanos ya admitidos conservan su contabilidad normal de cancelación.

Otros errores antes de headers producen HTTP 4xx/5xx; los clientes gRPC los
convierten según sus reglas. Después de headers se cancela el stream ante errores
de transporte o límites: no se fabrican trailers de éxito ni un segundo status
HTTP. Un `grpc-status` no cero del backend se conserva. El timeout de body gRPC
que puede observarse al poll también se entrega como status 4.
Los logs registran status HTTP, no éxito del RPC; contrato nuevo 1.1 en [LOGGING](LOGGING.md).

## Observación de recursos desde 0.6

El endpoint administrativo autenticado `/metrics` expone gauges sin etiquetas
por solicitud. El prefijo corresponde al `name` configurado:

| Gauge | Qué retiene el valor |
|---|---|
| `<name>_clients_active` | Permisos de sockets entrantes; incluye TLS, idle y WebSocket |
| `<name>_requests_active` | Permisos globales mientras viva cualquiera de los cuerpos |
| `<name>_upstream_streams_active` | Holds de streams HTTP/2, incluidos uploads separados de su respuesta y conexiones que drenan |
| `<name>_http2_tasks_active` | Futuros de streams entrantes programados por Hyper, hasta su destrucción, incluso si se abortan antes de ejecutarse |
| `<name>_upstream_connections_in_use` | Permisos físicos del pool: apertura, uso, idle y limpieza |
| `<name>_upstream_connections_idle` | Conexiones reutilizables actualmente idle |

Son lecturas independientes, no una transacción: durante una transición pueden
no coincidir solicitudes, streams y tareas. La cancelación se comprueba esperando
que se liberen los permisos dentro de los plazos configurados. Las conexiones
idle permanecen hasta `idle_connection_ms` y el siguiente sweep. Los descriptores,
threads y RSS del proceso se miden por separado; el allocator no tiene obligación
de devolver inmediatamente toda memoria al sistema operativo.

El incremento 6 incorpora un target libFuzzer que compila el mismo observador
usado por el proxy, con AddressSanitizer y cobertura, y clientes de estrés en
Docker sobre TCP y TLS. La campaña de red comprueba respuestas, límites y
recuperación de capacidad con backends reales. La configuración, seeds, comandos,
resultados y límites de estas mediciones están en [VALIDATION-6](VALIDATION-6.md).

## Prueba reproducible

```sh
docker build --platform linux/arm64 -t cauce:0.6-arm64 .
python3 scripts/test-http2-docker.py --image cauce:0.6-arm64 --platform arm64 --report-prefix increment6-http2
python3 scripts/test-http2-headers-docker.py --image cauce:0.6-arm64 --platform arm64 --report-path docs/increment6-http2-headers-arm64.json
```

El script inicia Colima si Docker no responde, crea certificados temporales,
construye imágenes y ejecuta clientes HTTP/2 y grpcio contra backends Node/http2
y grpcio reales. Usa un proyecto Compose aislado y conserva resultados en
`docs/increment6-http2-arm64.json` con el comando anterior (o el sufijo
`arm64`/`amd64` con `--platform`; `--report-prefix` selecciona el nombre). Los certificados/claves quedan en `work/`, excluido del
paquete. Resultados históricos en [VALIDATION-4](VALIDATION-4.md); timeout y regresiones
de 0.5 en [VALIDATION-5](VALIDATION-5.md); campañas de 0.6 en [VALIDATION-6](VALIDATION-6.md).

Referencias primarias verificadas para la implementación:
[RFC9113](https://www.rfc-editor.org/rfc/rfc9113.html),
[protocolo gRPC sobre HTTP/2](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md),
[Hyper HTTP/2 server](https://docs.rs/hyper/latest/hyper/server/conn/http2/),
[Hyper HTTP/2 client](https://docs.rs/hyper/latest/hyper/client/conn/http2/),
[hyper-util auto](https://docs.rs/hyper-util/latest/hyper_util/server/conn/auto/struct.Builder.html).
Cargo.lock fija las versiones efectivamente usadas; estas pruebas no certifican
conformidad exhaustiva RFC, resistencia DDoS ni rendimiento en producción.
