# Incremento 8: HTTP/2 y gRPC frente a NGINX

Laboratorio local reproducible. Mide HTTP/2 en ambos tramos, tanto prior knowledge en claro como TLS con CA y hostname verificados. El servidor gRPC real es grpcio 1.85.1: se prueban unary, client-streaming, server-streaming y bidi con handlers de bytes. No se incluye coste de serialización Protobuf. El cliente Node implementa framing gRPC sobre `node:http2` y comprueba cuerpos, mensajes y trailers; no usa el generador HTTP/1.1 del incremento 7.

El runtime Cauce sigue siendo **0.7.0**, sin cambios en Rust en este incremento. Se reutiliza la imagen nativa validada en el incremento 7. El incremento 8 añade el laboratorio, las pruebas y evidencia, no una nueva versión funcional del binario. Resultados observados en [VALIDATION-8](../../docs/VALIDATION-8.md).

## Ejecución

Requiere Docker con cuatro CPUs disponibles, al menos 3 GiB libres en el filesystem de resultados, Python 3, OpenSSL y Compose v2 con soporte de `healthcheck.start_interval`. Sin puertos publicados. En macOS el runner utiliza el socket Colima y ejecuta `colima start` si Docker no responde. Respeta `DOCKER_HOST` y `DOCKER_CONFIG` existentes.

```sh
# Desde la raíz de Cauce; compilar para la arquitectura nativa del Engine.
docker build --platform linux/arm64 -t cauce:0.7-final-arm64 .
python3 bench/increment8/run.py --image cauce:0.7-final-arm64
python3 bench/increment8/summarize.py bench/increment8/results/increment8-<ID>
# En amd64, construir una imagen amd64 y pasar su tag a --image.
# El runner rechaza imágenes cuya arquitectura no coincide con el Engine.
```

La ejecución completa usa tres repeticiones de nueve escenarios, dos proxies por escenario: **54 mediciones**. Son ocho segundos de oferta y dos de calentamiento por medición. La duración puede ampliarse con `--seconds`; la identidad inmutable de cada imagen se registra en `images.lock.json`. `--skip-build` reutiliza las fixtures locales; debe reservarse para imágenes construidas desde los mismos archivos. Las imágenes base y NGINX llevan digest; grpcio y typing-extensions están fijados. El runner copia hashes del código/configuración de laboratorio al manifiesto.

`run.py` registra `COMPLETE` sólo al superar todas las puertas de calidad. Las campañas FAIL/ABORTED conservan diagnóstico y no son aceptadas por `summarize.py`. El `finally` elimina únicamente el proyecto Compose de la medición, incluidos clientes y samplers de perfiles; no limpia servicios ajenos. Los certificados efímeros de prueba se generan por campaña, la clave CA se elimina tras firmar y las claves restantes no se incluyen en paquetes de entrega.

## Presupuestos y diferencias explícitas

| Recurso | Cauce | NGINX |
|---|---|---|
| CPU / memoria | 1 CPU, 256 MiB | 1 CPU, 256 MiB |
| Worker de ejecución | Tokio 1 worker | 1 worker NGINX |
| PID / nofile | 128 / 1024 | 128 / 1024 |
| Streams de entrada por sesión H2 | 16 | 16 |
| Solicitudes activas globales | 32 | `limit_conn` global 32 |
| Conexiones upstream | 32 activas + idle combinadas | `max_conns=32` activas; `keepalive=32` idle por worker |
| Streams por conexión upstream | 16, sujeto a SETTINGS remoto | Política nativa del módulo; se observa en backend |
| Presupuesto backend anunciado | SETTINGS 16 por conexión | Mismo backend y SETTINGS |
| Cuerpo | 16 MiB | 16 MiB |
| Acceso / reintentos / buffering | logs off, sin retry, streaming | logs off, retry off, buffering off |

La cuota NGINX de conexiones activas y la caché idle **no forman un límite total combinado**. No se afirma equivalencia exacta de pools: el runner comprueba el máximo observado de conexiones (32) y publica identidades y concurrencia reales. El gate mantiene 32 solicitudes para validar el presupuesto común. `limit_conn` cuenta cada request H2 después de recibir sus headers; Cauce limita además sockets entrantes a 128. Este ensayo no compara ataques de sockets idle ni encabezados incompletos.

Conexión: 1 s; headers upstream y envío/lectura: 15 s; idle upstream: 15 s. NGINX aplica varios de esos plazos entre operaciones y Cauce añade deadline absoluto de request de 15 s. Las cargas de referencia terminan antes de esos plazos. Los límites de buffers/headers y el control de flujo de cada implementación difieren; no se afirma que sus asignaciones internas sean idénticas.

Proxy CPU 0, backend CPU 1, generador CPU 2 y sampler CPU 3. Sólo un proxy y un backend del laboratorio están activos por medición. Backend y cliente tienen 1 CPU/256 MiB; backend PID 64 y cliente PID 128. El sampler es un sidecar de lectura con 0,1 CPU/32 MiB y PID namespace del proxy. Se registran límites efectivos del contenedor y se rechazan OOM, reinicios y muestras ausentes. No ejecutar otras cargas/builds simultáneos.

## Cargas y puertas de calidad

| Escenario | Tasa ofrecida | Trabajo |
|---|---:|---|
| h2-clear / h2-tls | 400 requests/s | 1024 bytes, ambos tramos H2 |
| h2-delayed | 64 requests/s | 1024 bytes con demora backend de 100 ms |
| h2-saturation | 128 requests/s | demora 500 ms, 4 sesiones × 16 slots |
| grpc-unary-clear / grpc-unary-tls | 200 RPC/s | un request y una respuesta |
| grpc-client-stream | 80 RPC/s | tres mensajes de entrada, una respuesta |
| grpc-server-stream | 80 RPC/s | un request, tres respuestas |
| grpc-bidi | 80 RPC/s | tres mensajes en cada sentido |

Dos sesiones persistentes × 16 slots en cargas normales. No es TLS frío: el calentamiento establece sesiones y pools. La tasa ofrecida es un calendario abierto, independiente de terminaciones. La cola tiene capacidad explícita (4 × slots totales); cada llegada recibe un registro incluso si se descarta por lag >250 ms o cola llena. Esos descartes invalidan la comparación. La latencia desde la llegada **planificada** incluye lag y espera, reduciendo la distorsión por coordinated omission. Se conservan también latencia de servicio, tiempo hasta headers, errores, tiempos relativos e identidad sesión/stream. Una respuesta 503 esperada sólo se admite en saturación y no entra en percentiles de éxito; permanece visible en todos los resultados crudos.

Antes de medir cada combinación stack/protocolo/TLS, `gate_h2.mjs` valida SETTINGS=16, envía 17 solicitudes por una sesión y observa exactamente 16 en el backend antes de completar la restante. Después mantiene 32 solicitudes en dos sesiones y exige 503 para una tercera sesión, 32 éxitos y recuperación hasta cero activas. La puerta usa H2 real y el backend correspondiente, también para gRPC. El generador nativo respeta SETTINGS; esta puerta no es fuzzing de un cliente que viola el protocolo (véase incremento 6).

Después de cada gate se destruyen y recrean proxy, backend y sampler para descartar conexiones idle y memoria retenida. Todas las mediciones registran un backend sin conexiones ni solicitudes antes del calentamiento. Así, las tres repeticiones parten del mismo estado y sólo calientan con su propia carga.

`load_h2.mjs` valida todos los cuerpos HTTP, framing gRPC, estado final `grpc-status=0` y mensajes esperados. El auditor independiente verifica cobertura de llegadas, IDs únicos, conteo del backend, ALPN, cuotas, recuperación, RPCs, errores, CPU y memoria. Publica flags si p99 del scheduler supera 50 ms o hay CPU throttling. No convierte mediciones afectadas en una afirmación de superioridad.

## Métricas

- `<run>.jsonl`: cada llegada ofrecida; `<run>.summary.json`: resumen del generador.
- `<run>.resources.json`: contadores acumulados Engine CPU, memoria cgroup, throttling y muestras `/proc` de RSS/threads/FDs. CPU 100% = un core. La ventana Engine incluye un pequeño margen de sondeo al inicio/final; la serialización de resultados queda fuera de ella.
- RSS es suma de procesos, no PSS. Puede contar páginas compartidas de NGINX más de una vez. Memoria cgroup incluye cache. Los picos son máximos **muestreados**, no máximos continuos garantizados.
- Backend Node registra eventos de sesión/stream; gRPC registra concurrencia exacta por `context.peer()` y sondea sockets TCP establecidos cada 50 ms. El sondeo puede omitir conexiones muy breves; no usar peers RPC como inventario de sockets idle.
- Goodput = éxitos / tiempo real de oferta + drenaje. RPC/s no equivale a mensajes/s. Se publica aparte el cociente por duración nominal de oferta. La tasa fija no busca el techo máximo del sistema.
- `runs.csv`: métricas por ejecución. `summary.json`: mediana, mínimo y máximo de tres ejecuciones por stack/escenario. Los percentiles agregados son medianas de percentiles por ejecución, **no** percentiles de muestras fusionadas.

## Pruebas del laboratorio

```sh
node --test bench/increment8/tests/test_load_h2.mjs
python3 -m unittest discover -s bench/increment8/tests -p 'test_*.py'
```

Los tests de Node pueden ejecutarse con la misma imagen fijada del generador. Las pruebas incluyen parser/framing, errores gRPC, multiplexación, cola acotada, overload y terminación de timers. Las pruebas Python fuerzan pérdida de ofertas, conteos inconsistentes, SETTINGS incorrecto, identidad de stream duplicada y estado gRPC inválido. Las puertas Docker complementan estas pruebas con tráfico por los proxies reales.

## Contratos consultados

NGINX admite upstream HTTP/2 mediante [`proxy_http_version 2`](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_http_version), incorporado en 1.29.4. gRPC utiliza el módulo separado [`grpc_pass`](https://nginx.org/en/docs/http/ngx_http_grpc_module.html#grpc_pass). Sus contratos de [`limit_conn`](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html#limit_conn), [`max_conns` y `keepalive`](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#keepalive) distinguen solicitudes H2, conexiones activas y cache idle. Consultados el 17 de noviembre de 2026.
