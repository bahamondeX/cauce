# Smoke local — 16 noviembre 2026

Medición histórica de Cauce 0.1. No mide TLS/WebSocket ni el rendimiento de Cauce 0.2.

Colima sobre macOS ARM64. Cliente Python en host, backends Node en red Compose,
proxy Rust release y NGINX 1.30.6 por puertos publicados. Dos segundos de warmup,
cinco segundos medidos, 100 solicitudes/s ofrecidas, concurrencia 16, logs off.
Latencia desde llegada programada. Versiones/recursos en results/environment.txt.

| Caso | Respuestas/ofrecidas | p50 ms | p95 ms | p99 ms | Errores de transporte | Drops del generador |
|---|---:|---:|---:|---:|---:|---:|
| cauce-logs-off | 500/500 | 5.45 | 8.03 | 14.32 | 0 | 0 |
| nginx-logs-off | 500/500 | 5.18 | 8.43 | 13.28 | 0 | 0 |

Datos brutos por llegada y muestras CPU/memoria de cgroup en results/. La primera
corrida de results-preliminary-overlap/ coincidió con pruebas Linux y queda excluida
de esta tabla; se conserva con sus datos brutos y la causa de exclusión.

Esta corrida corta no permite afirmar una mejora: no hay repeticiones/orden alternado,
los límites de adquisición de ambos productos no son equivalentes bajo saturación,
y macOS/Colima, el generador y docker stats introducen jitter. Se observan ambos
resultados, sin atribuir las diferencias al lenguaje. No hay RSS exacto, TLS ni
benchmark de SSE/WS. El resto de la matriz de rendimiento permanece pendiente;
consulte README.md de este directorio.
