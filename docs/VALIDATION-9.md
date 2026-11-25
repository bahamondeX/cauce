# Incremento 9 — fallos y recuperación en Docker

Campaña final **COMPLETE**, ejecutada el **17 de noviembre de 2026, 07:56:44–07:57:24, hora de Perú**. Pasaron las tres suites: proxy, logger y colector. [Manifiesto y hashes](../tests/resilience/results/increment9-20261117T125644Z-0917c9/manifest.json), [comandos reproducibles](../tests/resilience/README.md) y [verificación adicional](increment9-verification.json).

Se construyó **Cauce 0.9.0**, ARM64 nativo, imagen `sha256:36735941c0e2ea131efc032004d3dcd238313606f204fbc377668d3c6543cf21`. Docker Engine 27.4.0 sobre Colima, 4 CPU y 5.77 GiB de memoria. El runner resuelve el tag a un ID inmutable antes de ejecutar las suites. El cambio de runtime se limita a `src/telemetry.rs`, además de la versión en Cargo.toml/Cargo.lock. Los benchmarks de incrementos 7 y 8 siguen siendo resultados históricos de **0.7.0**; no se han repetido ni atribuido al motor 0.9.0. Para repetirlos con el motor original use las fuentes de `cauce-increment8.tar.gz`: los comandos de construcción históricos no convierten este árbol 0.9.0 en 0.7.0 por elegir otro tag.

## Defecto encontrado y corregido

La prueba de `ENOSPC` reprodujo en 0.7.0 una escritura parcial seguida de JSON concatenado sobre el fragmento incompleto. Las solicitudes HTTP continuaban, pero el archivo dejaba de ser JSONL válido y podía detener al colector. Se conserva la [reproducción fallida esperada](evidence/increment9-logger-before/increment9-logger-regression-before.json), el [archivo dañado](evidence/increment9-logger-before/increment9-logger-regression-before-enospc-logs.jsonl) y la evidencia de `dd` con cero bloques disponibles en un tmpfs de 4 MiB.

0.9.0 descarta el buffer fallido sin un flush implícito del destructor. Al reabrir, elimina únicamente el fragmento posterior al último salto de línea y rota ese inode antes de escribir en un archivo nuevo. Conserva la intención de rotar si falla el rename; tampoco rota un lote cuyo flush falló. Al arrancar rota una vez el archivo existente, cubriendo un reinicio entre reparación y rename. Las reaperturas ordinarias sin fragmento incompleto no provocan rotaciones adicionales.

La entrega del inode reparado es necesaria para el colector: éste puede haber observado el tamaño del fragmento sin haberlo checkpointado. Reutilizar el mismo inode con una línea de igual tamaño podría parecer una reescritura prohibida. La prueba integrada verifica el paso al inode nuevo sin debilitar las comprobaciones del colector contra copytruncate. La retención configurada sigue aplicándose, incluso el descarte con `retained_files=0`; reinicios/fallos repetidos pueden consumirla. El log requiere lectura/escritura y un único escritor. Más detalles en [LOGGING](LOGGING.md).

## Proxy: fallo de backend y recarga bajo carga

[Reporte completo](../tests/resilience/results/increment9-20261117T125644Z-0917c9/proxy_failures.json), [eventos reales](../tests/resilience/results/increment9-20261117T125644Z-0917c9/proxy_failures-access.jsonl).

| Comprobación | Resultado observado |
|---|---|
| Backend detenido con SIGKILL | Una de ocho solicitudes devolvió 502 al descubrir el fallo; las otras siete llegaron al destino sano. El destino fallido fue retirado temporalmente |
| Backend restaurado | Tras arrancarlo y vencer su backoff de 5000 ms, volvió a recibir solicitudes |
| POST con respuesta cortada | El cliente recibió headers 200 y luego error de cuerpo; el backend registró exactamente una recepción de `/post/drop`, sin reintento |
| Recarga con tráfico activo | **499 solicitudes, cero errores**: cuatro al backend A y 495 al B; generación 2 → 3 |
| Solicitud en vuelo de generación anterior | Se mantuvo retenida mediante una barrera del backend durante ambos HUP; terminó desde A con generación 2 después de atender nuevas solicitudes desde B |
| Configuración inválida | El HUP fue rechazado; se conservó la generación 3 y el backend B |
| HTTP/2 posterior | Una solicitud HTTP/2 llegó correctamente al nuevo backend |
| Recuperación de recursos | Clientes, requests, conexiones upstream, streams y tareas HTTP/2 en cero; 32 slots de request disponibles. FDs observados: 13 antes, 12 después |

El backend nuevo se comprueba antes de recargar. La carga empieza antes del HUP y registra respuestas de ambas generaciones. Los logs identifican la generación de la solicitud retenida. El test de POST demuestra una recepción en esta ejecución, no una garantía de idempotencia de la aplicación. El primer 502 es esperado: el proxy no repite automáticamente la solicitud que descubre un fallo.

## Logger: disco lleno y fallo de rotación

[Reporte y métricas](../tests/resilience/results/increment9-20261117T125644Z-0917c9/log_disk.json). Proxy limitado a 1 CPU, 256 MiB, 128 PIDs y nofile 1024, UID/GID 10001, rootfs de solo lectura y cola de logs de 16 eventos. Cada caso tiene un tmpfs de **4 MiB**. La prueba llena exclusivamente ese montaje o crea un directorio donde debería estar el archivo de rotación.

| Fallo inducido | HTTP durante el fallo | Contador IO observado | Recuperación |
|---|---:|---:|---|
| `ENOSPC` real | 256/256 respuestas 200 | 226 | Se elimina sólo el filler; las dos solicitudes posteriores aparecen como JSONL válido |
| Rename de rotación bloqueado | 256/256 respuestas 200 | 253 | Se retira sólo el directorio incompatible; las dos solicitudes posteriores se registran |

En ambos casos el proceso siguió activo, sin OOM ni reinicio, y sus gauges volvieron a cero; FDs 12 → 12. Se conservaron siete eventos válidos y como máximo dos archivos en cada caso. **No se promete conservar los 256 eventos durante el fallo**: el diseño permite pérdidas de logs para mantener el tráfico. El contador IO mide errores, no eventos perdidos; `log_dropped_total` sigue siendo una cota inferior ante fallos de flush.

## Colector: spool, ENOSPC, SIGKILL y log reparado

[Reporte con IDs, checkpoints e integridad SQLite](../tests/resilience/results/increment9-20261117T125644Z-0917c9/collector_storage.json). Casos de almacenamiento con red `none`, UID/GID 10001, 128 MiB, 0.5 CPU, 32 PIDs y tmpfs de **8 MiB**; la prueba integrada añade backend Node en una red interna sin puertos publicados.

- **Spool saturado:** cuota de 4096 bytes; se estabilizó en cuatro de 24 eventos y 3616 bytes. El siguiente evento no cabía. Tras reiniciar se conservaron IDs de eventos/lotes y checkpoint. Al ampliar explícitamente las cuotas y reiniciar, se recuperaron los 24 IDs exactos y el checkpoint llegó a los 19,454 bytes del origen.
- **Disco lleno real:** `df` confirmó 8192 KiB usados, cero disponibles. El colector terminó con error SQLite `database or disk is full`. El primer evento y su lote se conservaron; el checkpoint permaneció en **810 bytes**. Tras retirar sólo el filler y reiniciar, se recuperó el segundo evento y el checkpoint llegó a **1620 bytes**.
- **SIGKILL al daemon:** se mató el proceso vivo cuando estaba pausado por la cuota, con cuatro eventos ya guardados. La base quedó íntegra y preservó sus IDs/checkpoint. Tras reiniciar con más cuota aparecieron los 24 eventos exactos. No se presenta como prueba de un SIGKILL en cada punto de una transacción.
- **Logger y colector juntos:** dos respuestas reales del proxy. El colector había checkpointado el primer evento y observado un fragmento incompleto de 53 bytes. Al reiniciar sólo el proceso proxy, el logger reparó y rotó el inode; el colector continuó, conservó los dos IDs exactos y registró checkpoints de ambas generaciones.

SQLite devolvió `integrity_check=ok` en las comprobaciones antes/después correspondientes. Los casos de saturación, SIGKILL y ENOSPC usan fixtures deterministas del esquema; el caso integrado usa eventos del proxy. El tmpfs permanece montado mientras se reinician procesos. No se prueba persistencia después de destruir ese montaje, perder alimentación o reiniciar la VM. La recuperación de ENOSPC requiere liberar espacio y reiniciar el colector; se documenta su operación con supervisor en [COLLECTOR](COLLECTOR.md). Exportar el spool offline no elimina lotes ni libera cuota.

## Regresiones, limpieza y alcance

Pasaron **32 tests de Rust**, `cargo fmt --check` y Clippy de todos los targets con warnings denegados, sobre Rust 1.88.0 en Docker. Tres tests nuevos cubren reparación del tail, descarte del buffer sin flush adicional y retry de rename preservando el inode. [Salida completa](increment9-rust-checks.log) y [Dockerfile de validación ARM64](increment9-check.Dockerfile), ejecutable desde la raíz con `docker build --platform linux/arm64 --target checks -f docs/increment9-check.Dockerfile .`.

La [regresión HTTP/2/gRPC](increment9-http2-regression-arm64.json) pasó en claro y con TLS: límites de streams, multiplexación, trailers, GOAWAY, cuatro modalidades gRPC, cancelación/deadlines y HUP/TERM con solicitud activa. Se validaron por JSON Schema **85 eventos** de esa regresión y **528 eventos** de la campaña de resiliencia. La [demo offline](increment9-demo.json) también pasó sobre 0.9.0: cuatro respuestas reales, cuatro eventos únicos en [dos envelopes validados](increment9-demo-export.jsonl), reinicio del colector y tráfico mientras estaba detenido.

Se conservaron los seis contenedores de demo previos, con los mismos IDs y fechas de arranque. La campaña dejó **cero contenedores y redes de prueba**; se retiraron también los dos volúmenes temporales de la regresión HTTP/2 por sus nombres y proyecto verificados. No se llenó el disco del Mac ni el de Colima, ni se hizo prune global. El margen libre del host varió durante la ejecución; no se atribuye esa variación a una limpieza de este incremento.

Una campaña preliminar falló por una aserción del arnés que contaba la propia consulta `/stats` como activa cuando el fixture ya la había descontado. Se corrigió y se añadió una barrera explícita; ese piloto está excluido del paquete y de los resultados. La reproducción de 0.7.0 con JSONL dañado se conserva deliberadamente como fallo esperado. Ver [verificación](increment9-verification.json).

Estas pruebas comprueban recuperación funcional en escenarios locales y acotados. No son benchmarks bajo fallo, campañas prolongadas, pruebas de corrupción arbitraria o una demostración universal de ausencia de fugas/durabilidad. El colector no recupera registros perdidos antes de escribir JSONL. **No se contactó Connectivity** ni se acreditó ingesta remota; las limitaciones de su contrato documentadas en [CONNECTIVITY](CONNECTIVITY.md) permanecen vigentes.

```sh
python3 scripts/test-resilience-docker.py --image cauce:0.9-final-arm64
```

El [README del laboratorio](../tests/resilience/README.md) indica construcción, imágenes fijadas, recursos necesarios, ejecución individual y ubicación de los reportes.
