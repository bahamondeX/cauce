# Validación del incremento 3 — 16 noviembre 2026

Cauce 0.3 incorpora `cauce-collector` como proceso independiente. La validación
usa archivos reales, SQLite, certificados temporales, sockets HTTPS y contenedores
Docker sobre Colima. El receptor de pruebas implementa proposal-1; **no es una
integración con Connectivity**.

## Resultados ejecutados

| Comprobación | Resultado | Evidencia |
|---|---|---|
| Rust 1.88: fmt, Clippy sin warnings, build `--locked` y unitarias | PASS, 15 pruebas (5 proxy + 10 colector) | [Log Rust](increment3-rust-checks.txt) |
| Colector nativo, macOS ARM64 | PASS, 23 casos | [Informe](increment3-collector-native.json) |
| Almacenamiento nativo, macOS ARM64 | PASS, 13 casos | [Informe](increment3-collector-storage-native.json) |
| Colector Docker, Linux ARM64 | PASS, 23 casos | [Informe](increment3-collector-docker.json) |
| Almacenamiento Docker, Linux ARM64 | PASS, 13 casos | [Informe](increment3-collector-storage-docker.json) |
| Flujo real Compose, separación del proxy y aislamiento del colector | PASS, 4 comprobaciones | [Informe](collector-compose.json) |
| Regresión HTTP nativa | PASS, 19 pruebas | [Log](increment3-http-regression.txt) |
| Regresión TLS/WebSocket en Docker | PASS, 7 casos | [Informe](increment3-tls-regression.json) |
| Build Linux amd64 con cross-linker | PASS | [Log](increment3-amd64-build.txt) |
| Colector amd64 ejecutado bajo emulación | PASS, 3 casos de spool/reinicio/HTTPS/ACK | [Informe](collector-amd64.json) |
| Proxy amd64 ejecutado bajo emulación | PASS, configuración, UID y 6 respuestas HTTP | [Informe](increment3-amd64-http.json) |
| JSON Schema sobre la exportación real | PASS, 2 lotes y 20 eventos completos | [Log](increment3-schema.txt), [export](increment3-compose-export.jsonl) |

Los informes conservan nombres de casos y modo/arquitectura. Los 23 casos y los
13 de almacenamiento se repiten en dos plataformas; no representan 72 escenarios
distintos. Se actualizaron los avisos de 154 dependencias: [log](increment3-notices.txt).
Los hashes de fuentes, Cargo.lock, binarios ARM64 e imágenes quedan en
[increment3-release.json](increment3-release.json). La imagen Compose tiene otro
ID, pero los SHA-256 de ambos binarios coinciden con los de la imagen ARM64 probada.

Los fixtures crean CA/credenciales sintéticas; verifican TLS de forma estricta.
Para Colima sincronizan los archivos compartidos por hash y ejecutan rename/unlink
desde Linux: un rename iniciado en macOS sobre el montaje compartido puede cambiar
la identidad de inode observada por la VM. El perfil Compose usa volúmenes Docker
para logs y SQLite. Las pruebas no avalan garantías de SQLite sobre SSHFS/NFS.

## Repetición

Desde la raíz del proyecto, con Rust, Python 3, OpenSSL, uv y Docker disponibles:

```sh
./scripts/check.sh
docker build -t cauce-collector:test .
python3 scripts/test-collector.py --docker --image cauce-collector:test
python3 scripts/test-collector-storage.py --docker --image cauce-collector:test
python3 scripts/test-collector-compose.py
uv run --no-project --with websockets==15.0.1 python scripts/test-tls-docker.py --keep
uv run --no-project --with jsonschema python scripts/validate-collector.py docs/increment3-compose-export.jsonl
docker build --platform linux/amd64 -t cauce-collector:amd64 .
python3 scripts/test-collector-amd64.py --image cauce-collector:amd64
CAUCE_AMD64_IMAGE=cauce-collector:amd64 CAUCE_AMD64_REPORT=increment3-amd64-http.json python3 scripts/test-amd64.py
```

Los scripts Docker del colector arrancan Colima si el daemon no responde. La prueba
Compose deja proxy y colector offline activos; `--keep` conserva el ejemplo TLS.
Los números de lotes exportados dependen del volumen existente al repetir el test.
Las CA temporales duran dos días para el colector y tres para el ejemplo TLS; se
regeneran al repetir sus pruebas, y `work/` queda fuera del paquete distribuido.

## Escenarios

- Líneas completas/incompletas, offsets de varios eventos, reinicio, exportación
  sin consumo y exclusión de dos procesos sobre el mismo estado.
- Inicio con generaciones rotadas, rename, rotación concurrente, truncado y
  cambios de contenido detectables, pérdida de una generación con bytes pendientes.
- Cuota lógica, techo físico de SQLite, errores de escritura y estado corrupto.
- HTTPS, validación de CA/nombre, credencial rotada, 401/403/429/500, rechazo de
  redirecciones, ACK con ID/cantidad incorrectos, ACK grande, timeout y ACK perdido.
- SIGKILL mientras el receptor retiene el ACK; fallo transaccional después de
  recibir un ACK válido. En ambos casos se conserva y reenvía el lote estable.
- Flujo real proxy → volumen JSONL → colector offline, independiente del servicio
  HTTP. El perfil optativo no dispone de red.

## Alcance de la verificación remota

Se releyó el código de Connectivity en el commit `ee36f1756d0150912e1c8357132adcc2bdd98927`
y se consultaron rutas públicas de lectura. Evidencia y conclusión limitada al
árbol inspeccionado en [CONNECTIVITY](CONNECTIVITY.md). No se publicó código,
no se desplegó un receptor, no se enviaron logs a Connectivity ni se inventó un
login o un namespace autorizado.

## Límites

Un SIGKILL de proceso y un error SQLite inducido no simulan un corte eléctrico de
la VM ni un dispositivo que incumple fsync. La entrega depende de que sobrevivan
spool, checkpoints y logs aún no leídos. Una retención agotada puede perder datos;
no es un sistema sin pérdidas ni exactamente una vez. Las comprobaciones de
identidad/contenido detectan casos concretos de truncado/reescritura, no sustituyen
una fuente append-only con rotación por rename. No se prometen detección de
archivos que nacen y desaparecen entre polls, deduplicación ni aislamiento de
organizaciones en un servidor real que todavía no tiene contrato de ingesta.

Las pruebas anteriores de proxy se conservan en VALIDATION y VALIDATION-2 como
historial de sus respectivas versiones. Solo las regresiones ejecutadas en esta
versión se cuentan aquí. No hay benchmark nuevo ni afirmación de rendimiento
superior; la ejecución amd64 realizada sobre Colima ARM64 es emulación, no hardware amd64 nativo.
