# Benchmarks

Los laboratorios vigentes son [increment8/](increment8/README.md) para HTTP/2 y gRPC
y [increment7/](increment7/README.md) para HTTP/1.1, TLS frío/caliente, SSE y WebSocket.
Ambos conservan límites comprobados, repeticiones y recursos de proceso/cgroup.
Resultados: [incremento 8](../docs/VALIDATION-8.md) e [incremento 7](../docs/VALIDATION-7.md).

La sección siguiente conserva la metodología del smoke inicial; sus resultados
son históricos y no se mezclan con la campaña del incremento 7.

# Mediciones locales y reproducción

El generador es open-loop: la tasa ofrecida sigue un reloj independiente de respuestas.
La latencia se mide desde la llegada **programada**, incluyendo espera en la cola del
generador. Cola acotada a 2 × concurrencia; cada llegada descartada genera fila propia.
Los percentiles de respuestas nunca sustituyen los contadores de drops/errores. Esto evita
ocultar retrasos al frenar el cliente como un generador closed-loop, pero no hace que un
host sobrecargado sea un generador de referencia. Conservar queue_ms y verificar jitter.

```sh
docker compose -p cauce-local -f compose.yaml -f bench/compose.yaml up --build -d
python3 bench/run.py
# Generador independiente, configurable:
python3 bench/load.py http://localhost:8080/ --rate 100 --seconds 10 --concurrency 16 --output bench/results/custom.jsonl
# Volver al ejemplo con logs activos y ambos backends:
docker compose -p cauce-local up -d
```

La configuración de benchmark monta `bench/cauce.toml`, usa solo Node, sin logs de acceso;
NGINX también sin access logs, HTTP/1.1 keepalive, buffering de request/response desactivado,
16 conexiones idle, timeout de connect 1 s, read/send 15 s, retries desactivados, body máximo
16 MiB. Ambos contenedores tienen cgroup 2 CPUs / 256 MiB. Cauce usa dos threads Tokio,
NGINX un worker; no implican arquitecturas equivalentes ni procesos backend adicionales.

NGINX estable consultado el 16-11-2026:
[descargas oficiales, 1.30.6](https://nginx.org/en/download.html). El tag e información de
la imagen realmente descargada se conservan junto a los resultados. No se usa como motor
del producto: es un contenedor opcional de comparación.

**Límite de comparación:** `max_conns` y keepalive NGINX no equivalen a la contabilidad de
conexiones activas+idle del pool propio, ni tienen la misma cola de adquisición. Los límites
configurados se mantienen lejos de saturación en este smoke; no es una prueba con límites
equivalentes bajo sobrecarga. No hay TLS, WebSocket ni carga de streaming en este benchmark.
No interpretar estos números como comparación integral pedida para fases posteriores.

Las muestras docker stats son CPU de contenedor y memoria de cgroup, **no RSS exacto del
proceso** ni medición continua. Los percentiles vienen de cada fila raw del generador;
solo se guardan dos muestras de stats por corrida corta. Se necesita medición sostenida
de CPU/RSS, repeticiones y alternancia de orden para conclusiones de rendimiento.

Pendientes: TLS frío/caliente, logs on/off con equivalencia, SSE/WS, backend/cliente lento,
saturación, conexiones nuevas de ambos productos con trazas/tcpdump, fallos, reload/drenaje
y perfiles de contención/allocator. El test de disco lleno es funcional, separado de carga.
No se declara mejora ni se ocultan resultados desfavorables.
