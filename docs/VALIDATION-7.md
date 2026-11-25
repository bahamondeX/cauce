# Validación del incremento 7 — Cauce 0.7.0

Campaña `increment7-20261117T042921Z-21d7b8`: **COMPLETE**, 96 corridas y 139,392 llegadas programadas. Fechas UTC: 2026-11-17T04:29:21.829787+00:00 a 2026-11-17T04:50:42.860297+00:00.

Se ejecutaron tres repeticiones de ocho escenarios para Cauce y NGINX, con logs desactivados y activados. Cada corrida ofrece tráfico durante 8 s, tras 2 s de warmup salvo TLS frío, y drena las respuestas pendientes. Las cifras son mediciones locales a tasas fijas; no representan capacidad máxima ni una conclusión general de superioridad.

## Entorno y reproducibilidad

- Host: MacBook Air Mac14,2, Apple M2, 8 GB RAM; Colima con 4 vCPU y 6.20 GB decimales de memoria (5.77 GiB). Docker Engine 27.4.0, Compose 2.32.4, Linux ARM64 nativo.
- Proxy: 1 CPU y 256 MiB por contenedor; un worker Tokio o NGINX; 128 PID/threads y 1024 FD por proceso. CPU 0 para proxy, 1 backend, 2 generador, 3 sampler. Un solo proxy bajo carga a la vez.
- NGINX 1.30.6, Node 22.14.0 y Python 3.12.10; bases Docker fijadas por digest y dependencias Python por versión. Configuración efectiva y IDs en el manifiesto y `images.lock.json`.
- HTTP/1.1 en ambos saltos; keepalive upstream, streaming sin buffering completo, sin reintentos, 32 solicitudes activas y hasta 32 sockets upstream observados.
- TLS verifica CA y nombre en ambos saltos. El upstream de la fixture usa TLS 1.2 sin tickets ni caché de sesiones: TLS frío fuerza handshake completo, TLS caliente conserva sockets.

Ver [metodología y comandos](../bench/increment7/README.md), [manifiesto](../bench/increment7/results/increment7-20261117T042921Z-21d7b8/manifest.json), [imágenes](../bench/increment7/results/increment7-20261117T042921Z-21d7b8/images.lock.json), [hardware](increment7-host.json), [versión Compose comprobada después de medir](increment7-toolchain-postcheck.json) y [salida de la campaña](increment7-campaign.txt).

```sh
# Desde la raíz de Cauce, con Docker ARM64 y cuatro CPU disponibles:
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
docker build --platform linux/arm64 -t cauce:0.7-final-arm64 .
python3 bench/increment7/run.py --image cauce:0.7-final-arm64
# Se crean fixtures, certificados y un directorio nuevo de resultados.
# Sustituya FECHA-ID por el directorio que acaba de imprimir el runner:
python3 bench/increment7/summarize.py bench/increment7/results/increment7-FECHA-ID
```

La campaña entregada usó `--skip-build` después de construir las mismas fixtures identificadas en `images.lock.json`. En Linux ARM64 se puede usar el socket Unix local. Si Docker no responde, el runner intenta `colima start`.

## Escenarios

| Escenario | Tasa ofrecida | Contrato verificado |
|---|---:|---|
| HTTP normal | 500/s | Respuesta fija de 1024 bytes |
| Backend lento | 64/s | Demora de 250 ms; respuesta completa |
| Saturación | 128/s | Demora de 1500 ms, 64 workers cliente frente a cuota 32; exceso 503 |
| TLS caliente | 500/s | Sockets upstream reutilizados tras warmup |
| TLS frío | 100/s | Conexión cliente y upstream nuevas; handshake upstream no reanudado |
| SSE | 40/s | Cinco eventos a intervalos de 100 ms; primera entrega y fin separados |
| Lector lento | 20/s | Cuerpo de 1 MiB leído en bloques de 64 KiB, con pausa de 2 ms |
| WebSocket | 100/s | Upgrade 101, eco validado y cierre por sesión |

## Resultados

**Comparación de latencia limitada en 1 corrida(s):** el p99 de retraso del scheduler superó 50 ms. Los datos se conservan y se marcan en CSV/JSON; esas latencias incluyen interferencia del generador/entorno y no deben atribuirse exclusivamente al proxy. Corridas: `r2-cauce-logs-on-ws-n064`.

Mediana de las tres repeticiones, **logs desactivados**. Latencias p50/p95/p99 desde la llegada programada, únicamente respuestas útiles. Throughput útil = éxitos / tiempo total de carga y drenaje. CPU 100% = un núcleo; su intervalo usa timestamps del Engine que rodean la carga con un pequeño margen de polling. Memoria = mediana de los máximos muestreados por corrida; no es un pico continuo.

| Escenario | Proxy | Útiles/s | p50 ms | p95 ms | p99 ms | CPU % | Cgroup MiB | RSS suma MiB |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| normal | cauce | 499.97 | 1.25 | 2.22 | 2.43 | 2.87 | 1.55 | 4.88 |
| normal | nginx | 499.88 | 1.52 | 2.80 | 3.29 | 5.94 | 3.04 | 12.20 |
| delayed | cauce | 62.14 | 254.15 | 257.85 | 259.16 | 0.54 | 2.77 | 6.12 |
| delayed | nginx | 62.14 | 254.12 | 258.04 | 259.63 | 0.48 | 3.14 | 12.43 |
| saturation | cauce | 20.68 | 1503.45 | 1506.73 | 1510.10 | 0.46 | 2.73 | 6.00 |
| saturation | nginx | 20.67 | 1504.09 | 1506.63 | 1508.57 | 1.51 | 3.23 | 12.43 |
| tls-warm | cauce | 499.94 | 1.25 | 2.15 | 2.46 | 3.07 | 2.47 | 6.25 |
| tls-warm | nginx | 499.92 | 1.54 | 2.74 | 3.76 | 6.07 | 4.00 | 17.41 |
| tls-cold | cauce | 99.99 | 4.96 | 6.53 | 8.28 | 10.59 | 1.50 | 5.38 |
| tls-cold | nginx | 99.99 | 5.33 | 7.08 | 9.38 | 13.14 | 3.52 | 17.03 |
| sse | cauce | 37.68 | 510.32 | 520.22 | 523.70 | 1.77 | 2.55 | 5.75 |
| sse | nginx | 37.71 | 509.57 | 521.35 | 526.96 | 0.66 | 3.45 | 12.43 |
| slow-reader | cauce | 19.99 | 44.20 | 47.89 | 49.76 | 1.14 | 3.42 | 5.88 |
| slow-reader | nginx | 20.00 | 48.29 | 51.71 | 52.22 | 5.32 | 3.15 | 12.43 |
| ws | cauce | 99.97 | 2.12 | 3.53 | 4.65 | 2.50 | 1.34 | 4.62 |
| ws | nginx | 99.99 | 2.81 | 4.20 | 5.32 | 3.96 | 2.98 | 12.20 |

![Comparación local con rangos entre repeticiones](increment7-comparison.png)

Las barras muestran mediana y mínimo/máximo entre repeticiones. El panel de memoria usa cgroup, que incluye caché y evita comparar la suma RSS de dos procesos NGINX con un único proceso Cauce como si fueran magnitudes idénticas. RSS sigue disponible: la suma por PID puede contar páginas compartidas varias veces.

Variación al activar logs, misma carga HTTP normal de 500/s:

| Proxy | Logs | Útiles/s | p95 ms | p99 ms | CPU % | Cgroup MiB |
|---|---|---:|---:|---:|---:|---:|
| cauce | off | 499.97 | 2.22 | 2.43 | 2.87 | 1.55 |
| cauce | on | 499.95 | 2.16 | 2.39 | 3.40 | 7.85 |
| nginx | off | 499.88 | 2.80 | 3.29 | 5.94 | 3.04 |
| nginx | on | 499.91 | 2.18 | 2.39 | 2.32 | 4.69 |

Las 32 combinaciones agregadas, incluidas todas las variantes de logs, están en [COMPARISON.md](../bench/increment7/results/increment7-20261117T042921Z-21d7b8/COMPARISON.md). [CSV](../bench/increment7/results/increment7-20261117T042921Z-21d7b8/comparison.csv) conserva las 96 corridas y [JSON](../bench/increment7/results/increment7-20261117T042921Z-21d7b8/comparison.json) conserva medianas y rangos. Cada corrida tiene llegadas crudas JSONL, resumen, contadores antes/después y muestras de procesos/cgroup.

La comparación muestra costes distintos según escenario: en SSE sin logs, Cauce
registró CPU mediana de 1,77% frente a 0,66% de NGINX, con throughput prácticamente
igual. En lector lento, el máximo muestreado de memoria cgroup mediano fue
3,42 MiB para Cauce y 3,15 MiB para NGINX. Estas diferencias desfavorables a Cauce
se conservan junto con las favorables.

La dispersión de CPU también es material. En HTTP normal sin logs, Cauce varió
entre 2,51% y 6,95% y NGINX entre 2,11% y 6,24%. NGINX registró menos CPU mediana
con logs activados que desactivados; este experimento corto no permite atribuir
esa diferencia a una mejora causada por el logging. Los rangos se solapan: no
se afirma una reducción estable de CPU ni un ganador general.

## Comprobaciones de calidad y límites

- 129,408 respuestas útiles; 9,984 respuestas HTTP 503 de saturación. Errores de transporte/validación: 0; timeouts: 0; drops del scheduler: 0; rechazos de cola: 0. Los 503 no entran en el percentil de éxito ni en el throughput útil.
- 12 preflights independientes: 32 solicitudes retenidas, rechazo 503 de la número 33 y cero solicitudes activas después de liberar. Todas las corridas verificaron solicitudes y conexiones upstream ≤32, recuperación a cero solicitudes, ausencia de OOM y cero reinicios.
- TLS frío: 9,600 handshakes upstream completos y cero reanudados; uno por respuesta útil. TLS caliente verificó reutilización. Se distinguen conexiones cliente y upstream en los datos.
- 6 grupos con logs: 86,118 filas JSON válidas; recuento exacto de ofertas medidas + warmup completado + preflight. Logs en volúmenes Docker locales, exportados después de detener el proxy. Esta igualdad no prueba garantías de logging fuera de la carga observada.
- Mayor p99 de retraso del scheduler entre corridas: 53.57 ms. Corridas marcadas por el criterio del runner como limitadas por el generador (p99 >50 ms o pérdida de ofertas): 1. CPU máxima media por corrida del cliente: 22.97%; backend: 13.33%.
- CPU proxy máxima media por corrida: 13.61%. Máximo muestreado global de memoria cgroup: 10.35 MiB sobre cuota 256 MiB. Throttling acumulado de CPU (ns del contador Docker): proxy 0, cliente 0, backend 0.
- Auditoría independiente: vuelve a contar las llegadas, IDs y resultados; recalcula percentiles desde JSONL y CPU desde contadores/timestamps; verifica matriz completa, preflights, logs exportados, bytes, SSE, eco WS, handshakes y recursos.

La campaña inicial fue excluida por agotamiento del disco del host, que dejó Docker en modo de solo lectura. Se eliminaron únicamente 2,8 GiB de compilaciones regenerables del proyecto, se reinició Colima, se restauraron los servicios previos y se repitió toda la matriz. El runner ahora comprueba espacio libre y escritura en Docker antes de medir. La exclusión está registrada en [increment7-excluded-campaign.json](increment7-excluded-campaign.json); sus cifras no se mezclan con esta campaña.

## Pruebas y artefactos

- 29 pruebas Rust aprobadas (19 proxy y 10 colector), `cargo fmt --check` y `cargo clippy --all-targets --all-features -- -D warnings`: [unit](increment7-unit.txt), [clippy](increment7-clippy.txt), [fmt](increment7-fmt.txt).
- 10 pruebas del generador aprobadas, incluida cola acotada, omisión coordinada, percentiles, warmup, SSE, WS y barrera que separa captura de CPU de serialización: [evidencia](increment7-generator-tests.txt).
- Imágenes Cauce linux/arm64 y linux/amd64 construidas: [ARM64](increment7-build-arm64.txt), [AMD64](increment7-build-amd64.txt). Solo ARM64 nativo se usó para rendimiento. El runtime `src/**/*.rs` conserva los mismos bytes del incremento 6; este incremento añade el laboratorio y cambia versión de paquete. Las pruebas HTTP/2/gRPC, fuzzing y recursos de ese runtime están en [VALIDATION-6](VALIDATION-6.md).
- La limpieza se verificó en los 12 proyectos Docker; el runner incluye explícitamente los perfiles de cliente y sampler al desmontar, para retirar también sidecars detenidos: [evidencia](increment7-cleanup.json). Este ajuste de cleanup se ejercitó después de medir y no cambia las cargas ni los datos.
- El archivo de entrega conserva fuente, herramientas, una campaña completa y hashes en [increment7-release.json](increment7-release.json). Las claves privadas temporales de TLS se excluyen; el runner genera otras nuevas.

## Interpretación y límites

Tres ventanas cortas por combinación permiten reproducir este experimento y observar variabilidad; no dan una estimación robusta de colas extremas ni intervalos de confianza de producción. El generador usa llegadas programadas y conserva errores/colas para evitar ocultar latencia por omisión coordinada. Los resultados conservan throughput completado dentro de la ventana ofrecida y throughput con drenaje; no se deben confundir sus denominadores.

Los límites equivalentes son presupuestos verificables para esta fixture. NGINX limita conexiones activas y su caché idle por separado; Cauce cuenta ambas juntas. Con un worker, un destino y 32 solicitudes admitidas, se verificó el techo físico de 32. No se declara equivalencia de sockets idle de cliente, deadlines absolutos frente a inactividad, parsers, módulos o políticas generales de pool.

Logs on/off usan archivos JSON en el mismo tipo de volumen local, pero campos y política difieren: Cauce tiene cola asíncrona y más detalle; NGINX escribe su access log sin buffering explícito y `pool_wait_ms` queda `null`. Los archivos incluyen warmup y preflight además de la medida. No se inventa una medición de espera de pool de NGINX.

Memoria y CPU son observaciones del laboratorio: cgroup incluye caché; RSS suma procesos; el muestreo de Engine (~0,8 s) y `/proc` (~0,2 s) puede perder picos breves. El host macOS y la VM comparten recursos físicos y no se midieron frecuencia ni temperatura. Este trabajo no ejecutó builds, fuzzing ni otra carga de benchmark en paralelo durante la campaña.

Quedan pendientes una rampa de capacidad máxima, comparativas HTTP/2/gRPC, múltiples backends/workers, fallos/reloads, disco lleno, WAN y campañas largas. No se extrapola TLS frío upstream de esta fixture a TLS 1.3. Las regresiones y diferencias de coste deben evaluarse por escenario y rango de repeticiones; no se ocultan tras una puntuación única.
