# Validación del incremento 4 — 16 noviembre 2026

Este informe conserva la evidencia histórica de 0.4. El timeout HEADERS pendiente
que se describe abajo queda implementado y probado en [0.5](VALIDATION-5.md).

Cauce 0.4 incorpora HTTP/2 de entrada y upstream, gRPC y cuotas de streams. Las nueve agrupaciones HTTP/2 se repiten en ambas
arquitecturas; no representan dieciocho escenarios distintos.
La validación combina Rust nativo en macOS ARM64 y sockets reales entre
contenedores Docker sobre Colima. Los backends usan Node `node:http2` y grpcio;
los clientes usan Python h2, grpcio y Node. Un peer Python h2 adicional controla
la respuesta temprana sin resetear la mitad de subida. Los handlers gRPC son genéricos y
transportan bytes con framing gRPC real, sin necesitar generación de Protobuf.

## Resultados ejecutados

| Comprobación | Resultado | Evidencia |
|---|---|---|
| Rust 1.88, fmt, Clippy y build `--locked` | PASS, 21 pruebas (11 proxy + 10 colector) | [Unitarias](increment4-unit.txt), [Clippy](increment4-clippy.txt) |
| HTTP/2 + gRPC Docker ARM64 | PASS, 9 grupos + HUP/TERM | [Informe](increment4-http2-arm64.json), [logs HTTP](increment4-http2-arm64-access.jsonl) |
| HTTP/2 + gRPC Docker amd64 emulado | PASS, 9 grupos + HUP/TERM | [Informe](increment4-http2-amd64.json), [logs HTTP](increment4-http2-amd64-access.jsonl) |
| Regresión HTTP nativa | PASS, 19 pruebas | [Log](increment4-http-regression.txt) |
| TLS/WebSocket Docker ARM64 | PASS, 7 grupos | [Informe](increment4-tls-regression.json) |
| Colector nativo | PASS, 23 casos | [Informe](increment4-collector-native.json) |
| Almacenamiento nativo | PASS, 13 casos | [Informe](increment4-storage-native.json) |
| Colector Docker ARM64 | PASS, 23 casos | [Informe](increment4-collector-docker.json) |
| Almacenamiento Docker ARM64 | PASS, 13 casos | [Informe](increment4-storage-docker.json) |
| Flujo Compose proxy → colector offline | PASS, 4 comprobaciones | [Informe](increment4-compose.json) |
| Colector con logs reales del nuevo proxy | PASS, 85 eventos (84 HTTP/2), reinicio sin duplicados y exportación offline | [Informe](increment4-http2-collector.json), [fuente](increment4-http2-collector-source.jsonl), [exportación](increment4-http2-export.jsonl) |
| JSON Schema | PASS, logs de ambas arquitecturas, TLS y exportaciones completas | [Log](increment4-schema.txt) |

Los casos de colector/almacenamiento repetidos en dos plataformas representan 36
escenarios distintos, no 72. Su binario Docker coincide por SHA-256 con el de la
versión final; el manifiesto conserva el ID de la imagen usada para esas pruebas.
Los logs TLS y el volumen Compose contienen también eventos históricos: su
validación de schema comprueba compatibilidad 1.0/1.1, no contabiliza todos esos
eventos como solicitudes nuevas del incremento.

## Escenarios HTTP/2 y gRPC

- Entrada cleartext por prior knowledge y TLS con ALPN h2; upstreams HTTP/2 en
  claro y con TLS verificado. Negociación HTTP/1.1 en el mismo listener TLS.
- Traducción HTTP ordinario HTTP/1.1 ↔ HTTP/2; rechazo de una ruta gRPC hacia
  HTTP/1.1, autoridad conflictiva, `grpc-timeout` inválido y exceso de headers.
- SETTINGS de entrada con máximo cuatro streams y cliente que intenta excederlo.
  Máximo local upstream de dos y máximo anunciado por backend de cuatro, bajo
  solicitudes concurrentes; comprobación de reutilización del mismo socket.
- Los cuatro tipos de RPC: unary, client streaming, server streaming y bidi.
  Metadata inicial/final, trailers y status de error conservados.
- Cancelación y deadline con un RPC hermano funcional. Plazo gRPC legible como
  status 4 antes/después de headers; una ventana de recepción bloqueada termina
  dentro del presupuesto de limpieza, sin cancelar otros streams.
- Respuesta final temprana con upload detenido por ventana upstream cero:
  capacidad retenida hasta limpieza y recuperación posterior. Una prueba Rust
  comprueba que un hermano vigente puede terminar durante esa limpieza.
- GOAWAY del backend fuerza reemplazo de conexión. HUP y SIGTERM durante una
  respuesta ya iniciada permiten terminarla; SIGTERM entrega GOAWAY al cliente.

La prueba de ventana bloqueada devuelve crédito de conexión antes de solicitar
el siguiente RPC: aísla el bloqueo por stream sin exigir DATA sobre una ventana
de conexión agotada por el propio cliente. El cliente de ciclo de vida confirma
headers de respuesta antes de enviar la señal al proxy.

## Repetición

Desde la raíz del proyecto:

```sh
./scripts/check.sh
python3 scripts/test-http2-docker.py
uv run --no-project --with websockets==15.0.1 python scripts/test-tls-docker.py --keep
python3 scripts/test-collector-compose.py --report-prefix increment4-compose

docker build --platform linux/arm64 -t cauce:0.4-arm64 .
python3 scripts/test-http2-docker.py --image cauce:0.4-arm64 --platform arm64
docker build --platform linux/amd64 -t cauce:0.4-amd64 .
python3 scripts/test-http2-docker.py --image cauce:0.4-amd64 --platform amd64
python3 scripts/test-http2-collector.py docs/increment4-http2-arm64-access.jsonl

uv run --no-project --with jsonschema python scripts/validate-logs.py docs/increment4-http2-native-access.jsonl
uv run --no-project --with jsonschema python scripts/validate-collector.py docs/increment4-http2-export.jsonl
```

El script HTTP/2 inicia Colima cuando Docker no responde y usa un proyecto Compose
aislado. Genera CA/claves temporales bajo `work/http2/`, prueba los dos listeners
y elimina sus contenedores al terminar salvo `--keep`. Las ejecuciones con
`--platform` cambian la arquitectura del proxy; los fixtures permanecen nativos.
Los informes `increment4-http2-{native,arm64,amd64}.*` se reemplazan al repetir
la misma plataforma; los archivos ARM64 y amd64 incluidos documentan esta entrega. No ejecutar simultáneamente dos pruebas HTTP/2 que usen
el mismo directorio de certificados.

## Alcance y límites

La ejecución amd64 ocurre bajo emulación en Colima ARM64; no es hardware amd64
nativo. Las pruebas muestran casos observados, no certificación exhaustiva de
HTTP/2/gRPC, resistencia a ataques de resets, ausencia de fugas bajo cualquier
carga ni rendimiento superior. No se ejecutó un benchmark nuevo.

`client_headers_ms` limita el preface y headers HTTP/1.1, pero no un bloque HEADERS
HTTP/2 incompleto después del preface. Ese cliente puede retener un slot acotado
hasta desconexión/apagado. El timeout de body inactivo se evalúa al poll; el plazo
absoluto de stream tiene su propio reloj. gRPC permite hasta 100 ms adicionales
para entregar el status terminal, con reset si la ventana bloquea su entrega.
La limpieza upstream tras respuesta temprana puede esperar a hermanos todavía
vigentes, conservando las cuotas físicas durante ese intervalo.

HTTP/2 sobre Unix está implementado, pero esta batería no añade un fixture Unix
HTTP/2; la regresión Unix existente usa HTTP/1.1. Upgrade h2c, RFC8441, gRPC-Web,
HTTP/3, ACME, mTLS y reintentos automáticos permanecen fuera del alcance.

El colector acepta eventos 1.1 con `protocol = HTTP/2` y conserva compatibilidad
1.0/HTTP/1.1. La exportación es offline: no se enviaron logs a Connectivity ni
se modificó el contrato remoto. Se actualizaron 156 entradas de licencias de
dependencias. Opciones, fuentes primarias y semántica en [HTTP2](HTTP2.md);
hashes de fuentes, Cargo.lock, binarios e imágenes en
[manifiesto de la versión](increment4-release.json).
