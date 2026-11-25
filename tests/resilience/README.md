# Fallos y recuperación — incremento 9

Ejecuta pruebas reales contra Cauce en Docker, con backend Node y clientes en contenedores. Los casos de almacenamiento del colector usan registros fixture deterministas del esquema público; su prueba de recuperación del logger usa dos respuestas reales del proxy. Ninguna prueba contacta Connectivity. El runner publica cero puertos y elimina únicamente sus recursos temporales. Mantiene los contenedores preexistentes y comprueba sus IDs y fechas de arranque al terminar.

```sh
# Desde la raíz de Cauce, con Docker/Colima operativo y Python 3.10+.
# En macOS cada runner arranca Colima si Docker no responde.
docker build --platform linux/arm64 -t cauce:0.9-final-arm64 .
docker pull node:22.14.0-alpine3.21@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944
docker pull python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740
python3 scripts/test-resilience-docker.py --image cauce:0.9-final-arm64
```

En amd64 use una imagen nativa construida para `linux/amd64`. El runner registra imagen, arquitectura, hashes de fuentes, límites y evidencia; rechaza emulación. Respeta `DOCKER_HOST` y `DOCKER_CONFIG`. El runner principal selecciona el socket de Colima si existe; para otro Engine configure `DOCKER_HOST` o el contexto Docker. Requiere 2 GiB libres para resultados y margen operativo; no hace limpieza global ni llena discos del host/VM.

Los resultados van a `tests/resilience/results/increment9-<UTC>-<ID>/`. `manifest.json` sólo declara `COMPLETE` si las tres suites pasan, las fuentes no cambian durante la campaña, el cleanup termina y los servicios anteriores siguen activos. Puede elegir un directorio nuevo con `--report-dir`. Los JSON y stdout/stderr se conservan también al fallar. Ejecución individual:

```sh
python3 tests/resilience/proxy_failures.py --image cauce:0.9-final-arm64 --report work/proxy-failures.json
python3 tests/resilience/log_disk.py --image cauce:0.9-final-arm64 --report work/log-disk.json
python3 tests/resilience/collector_storage.py --image cauce:0.9-final-arm64 --report work/collector-storage.json
```

| Suite | Fallo y comprobación |
|---|---|
| Proxy | Caída del backend, POST con respuesta cortada sin reintento, recuperación de capacidad, HUP válido/inválido durante carga y drenaje de una solicitud anterior |
| Logger | `ENOSPC` real en tmpfs de 4 MiB y fallo de rename por destino incompatible; tráfico continúa, sube contador IO, se recupera JSONL y se liberan recursos |
| Colector | Cuota de spool de 4096 bytes, `ENOSPC` real en tmpfs de 8 MiB y `SIGKILL` al daemon; IDs/lotes/checkpoints estables e integridad SQLite antes/después. Además, recuperación integrada del log tras observar un fragmento incompleto |

El `tmpfs` tiene un proceso/contendor que mantiene el montaje durante reinicios del colector. La prueba comprueba reinicios del proceso sobre el mismo estado; no simula pérdida eléctrica, reinicio de la VM ni persistencia de tmpfs al eliminar un contenedor. La saturación del spool se resuelve aumentando su cuota explícitamente y reiniciando; el modo offline conserva todos los lotes y no los elimina al exportar.

La recuperación de logs sólo conserva registros completos: los eventos perdidos en la cola o durante un fallo de escritura no reaparecen. El archivo de log debe ser exclusivo de un proxy y permitir lectura/escritura; al abrirlo 0.9.0 elimina el fragmento final sin salto de línea y rota ese inode antes de continuar. También rota una vez al arrancar. No repara corrupción de líneas completas ni admite escritores externos concurrentes. Véase [LOGGING](../../docs/LOGGING.md) y [resultados/alcance](../../docs/VALIDATION-9.md). Estas son pruebas de corrección bajo fallos, no benchmarks de rendimiento ni garantías universales de durabilidad.
