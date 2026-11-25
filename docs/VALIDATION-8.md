# Validación del incremento 8 — HTTP/2 y gRPC

Campaña final **COMPLETE**, 2026-11-17 06:31:16 a 2026-11-17 06:54:57, hora de Perú. **54 mediciones, 78,336 llegadas, 75,264 éxitos y 3,072 rechazos HTTP 503 esperados por saturación**. Cero errores de transporte, timeouts, pérdidas del scheduler o rechazos de cola aceptados. Nueve escenarios × Cauce/NGINX × tres repeticiones, ocho segundos de oferta y dos de calentamiento. El auditor recalculó resultados desde los registros de cada llegada.

El binario es Cauce **0.7.0**, sin modificaciones en Rust: se comprobaron hashes de 14 archivos de runtime/Cargo/Dockerfile contra la versión construida en incremento 7. Esta entrega añade benchmarks y pruebas, no atribuye mejoras a cambios de motor. NGINX es **1.30.6**; H2 Node **22.14.0**, gRPC **grpcio 1.85.1** y Python del backend **3.12.10**. Imágenes ARM64 nativas, sin emulación de CPU; Docker Engine 27.4.0 aarch64, 4 vCPU y 5.77 GiB disponibles en Colima. Proxy, backend, cliente y sampler usan CPUs distintas de la VM entre sí. Los cpusets no reservan esas CPUs frente a los seis contenedores de demo que permanecieron activos; su consumo no se muestreó. El host y la VM también comparten recursos físicos, de modo que la contención externa puede afectar latencia/CPU. No es bare metal ni evidencia de producción.

## Resultados medidos

Medianas de tres ejecuciones. p95 usa sólo respuestas/RPC exitosos y mide desde llegada **programada**, incluyendo lag y cola. Goodput usa oferta + drenaje reales. CPU 100% equivale a un core; cgroup incluye cache. Los rangos completos mínimo/máximo están en el [resumen JSON](../bench/increment8/results/increment8-20261117T113116Z-609910/summary.json), la [tabla por ejecución](../bench/increment8/results/increment8-20261117T113116Z-609910/runs.csv) y el gráfico. Una tasa fija no representa el throughput máximo.

| Escenario | p95 ms Cauce / NGINX | Éxitos/s Cauce / NGINX | CPU % Cauce / NGINX | Cgroup MiB Cauce / NGINX |
|---|---:|---:|---:|---:|
| h2-clear | 3.67 / 3.15 | 399.92 / 399.90 | 6.92 / 3.05 | 1.53 / 3.08 |
| h2-tls | 4.26 / 3.80 | 399.89 / 399.97 | 7.35 / 6.08 | 1.60 / 3.81 |
| h2-delayed | 106.84 / 106.43 | 63.31 / 63.28 | 2.27 / 1.69 | 1.82 / 2.93 |
| h2-saturation | 505.85 / 506.09 | 61.17 / 61.11 | 3.07 / 2.02 | 2.75 / 3.48 |
| grpc-unary-clear | 5.07 / 4.35 | 200.01 / 199.96 | 4.44 / 4.10 | 1.73 / 3.05 |
| grpc-unary-tls | 5.30 / 4.56 | 200.00 / 199.98 | 6.38 / 4.99 | 1.81 / 4.19 |
| grpc-client-stream | 5.62 / 5.26 | 80.00 / 79.99 | 4.11 / 2.59 | 1.50 / 3.03 |
| grpc-server-stream | 5.59 / 3.75 | 80.01 / 79.99 | 3.38 / 1.03 | 1.50 / 3.02 |
| grpc-bidi | 4.48 / 6.02 | 79.98 / 80.00 | 1.94 / 3.41 | 1.61 / 3.04 |

![Medianas y rangos](increment8-comparison.png)

## Límites, protocolo y recuperación

Ocho gates reales (dos proxies × H2/gRPC × claro/TLS) pasaron. SETTINGS anunció 16 streams; una sesión con 17 solicitudes nunca llevó más de 16 activas al backend. Dos sesiones mantuvieron 32 activas; la solicitud adicional recibió 503 y después se volvió a cero activas y se atendió otra solicitud correctamente. En saturación se observaron 32 activas en cada ejecución, sin sobrepasar la cuota. Cada medición cotejó el número de solicitudes/RPC del backend con los éxitos del cliente. Después de los gates se recrearon proxy, backend y sampler; todas las mediciones registraron cero conexiones y solicitudes antes del calentamiento. Una campaña anterior con pools residuales fue abortada y excluida.

Los contenedores de ambos proxies tienen 1 CPU, 256 MiB, PID 128, nofile 1024 y un worker. Los access logs están desactivados en ambos. HTTP/2 ordinario usa `proxy_http_version 2` en NGINX y upstream `protocol="http2"` en Cauce; gRPC usa `grpc_pass` y un servidor grpcio real. CA/hostname se validan en ambos tramos TLS y se exige ALPN h2. Las cuatro modalidades gRPC validan framing, mensajes y estado terminal. Son handlers de bytes, no benchmarks de serialización Protobuf ni de una aplicación de negocio.

La igualdad de presupuestos no implica pools idénticos: Cauce cuenta activas + idle en una cuota combinada de 32; NGINX separa `max_conns=32` activas y `keepalive=32` idle. Su `limit_conn` cuenta requests H2 con headers completos y no equivale al límite Cauce de 128 sockets frontend. La configuración fija `server_name` para que la clave global no sea vacía; el gate comprueba su efecto. Se conservan los conteos observados de conexiones y streams, sin atribuir las conexiones frontend al pool upstream.

| Escenario | Pico conexiones backend Cauce / NGINX (mediana de máximos) |
|---|---:|
| h2-clear | 1 / 4 |
| h2-tls | 1 / 8 |
| h2-delayed | 1 / 7 |
| h2-saturation | 2 / 32 |
| grpc-unary-clear | 1 / 2 |
| grpc-unary-tls | 1 / 3 |
| grpc-client-stream | 1 / 1 |
| grpc-server-stream | 1 / 1 |
| grpc-bidi | 1 / 1 |

Node cuenta eventos de sesión/stream. gRPC cuenta RPCs por peer exactamente y sondea TCP establecido cada 50 ms: sus picos físicos son muestreados y pueden omitir conexiones breves. RSS suma procesos y puede duplicar páginas compartidas de NGINX; no es PSS. El cargo de páginas compartidas/cache a un cgroup depende de su propietario, por lo que este dato tampoco representa RAM física exclusiva ([documentación del kernel](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-ownership)). Se conservaron seis servicios de demo previos: no se midió una VM vacía ni se vació la cache global. CPU usa contadores Engine con una pequeña ventana de sondeo alrededor de la carga; la serialización de archivos está fuera de la medición.

## Calidad y alcance de las conclusiones

Ninguna corrida superó el umbral de p99 del scheduler de 50 ms ni mostró throttling de CPU en proxy, cliente o backend. No hubo OOM ni reinicios. Esto valida la calidad dentro de los umbrales del laboratorio; no elimina variabilidad de la VM/host.

La tabla presenta también los resultados menos favorables a Cauce. Comparar medianas pequeñas sin sus rangos puede exagerar diferencias; no hay intervalos de confianza ni significancia estadística con sólo tres repeticiones cortas. Los resultados de HTTP/1.1, logs on/off, TLS frío, SSE y WebSocket permanecen separados en [incremento 7](VALIDATION-7.md). Este incremento no mide gRPC TLS frío, serializers Protobuf, streaming prolongado, capacidad máxima, disco lleno, fallos/reload bajo carga ni red distribuida.

## Limpieza previa y pruebas

Antes de construir/cargar se retiraron cachés regenerables locales y BuildKit antiguo sin registros en uso (último uso registrado: 2026-08-16T04:18:00.561679392Z). BuildKit informó **6,08 GB recuperados**; se retiraron además **21 contenedores detenidos** de samplers de pilotos del incremento 7, por nombres verificados. No se eliminaron volúmenes de aplicaciones, imágenes ni entregas anteriores. Colima se inició al encontrar el daemon apagado y se restauraron seis servicios de demo existentes. Evidencia: [limpieza](increment8-cleanup.json) y [comprobación final de recursos y servicios conservados](increment8-cleanup-postcheck.json).

Pasaron **6 pruebas del generador dentro de Node 22 en Docker**, **5 pruebas del auditor Python**, los **8 gates Docker** y las **54 mediciones auditadas**. Los tests cubren framing/trailers, timeout, overload, cola acotada, timestamps y barreras que impiden serializar durante el muestreo. Las pruebas del auditor fuerzan ofertas perdidas, conteos contradictorios, SETTINGS inválido, stream IDs duplicados, errores de gRPC, estado residual de gates y timestamps contradictorios. El código Rust no cambió; no se presenta una nueva ejecución de sus tests históricos como prueba de esta entrega. [Verificación de fuentes y tests](increment8-verification.json).

La [demo aislada](DEMO.md) también pasó en Docker: cuatro respuestas reales del proxy aparecen una sola vez en el export del colector tras reinicios; el proxy sigue sirviendo cuando el colector está detenido. Colector sin red y UID 10001, recursos temporales retirados al terminar. [Resultado](increment8-demo.json), [export JSONL](increment8-demo-export.jsonl).

Connectivity se revalidó por lectura del repositorio: el commit sigue sin contrato de ingesta/autorización para Cauce. No se enviaron logs ni se modificó su repositorio. La demo offline no acredita integración remota. [Contrato y bloqueo](CONNECTIVITY.md), [propuesta experimental del colector](COLLECTOR.md).

## Reproducir y revisar

```sh
python3 bench/increment8/run.py --image cauce:0.7-final-arm64
python3 bench/increment8/summarize.py bench/increment8/results/increment8-<ID>
python3 scripts/demo-offline-docker.py
```

Campaña seleccionada: [`increment8-20261117T113116Z-609910`](../bench/increment8/results/increment8-20261117T113116Z-609910/manifest.json). [Imágenes inmutables](../bench/increment8/results/increment8-20261117T113116Z-609910/images.lock.json), [fuentes y metodología](../bench/increment8/README.md), [pilotos excluidos](increment8-excluded-pilots.json). Se entrega sólo la campaña completa seleccionada dentro del archivo de distribución; los pilotos permanecen en el workspace.
