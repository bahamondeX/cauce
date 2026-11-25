# Incremento 5: timeout de HEADERS HTTP/2

Cauce 0.5 aplica `limits.client_headers_ms` a bloques HEADERS/CONTINUATION
incompletos de entrada, en claro y después de TLS. El plazo es absoluto desde el
primer byte del frame header hasta el último byte del payload con END_HEADERS.
Incluye frame headers parciales, esperas entre CONTINUATION y trailers. Recibir
bytes no renueva el presupuesto. El observador retiene nueve bytes y contadores;
Hyper conserva la validación de protocolo y HPACK.

El watchdog se ejecuta independientemente de las lecturas/escrituras de Hyper y
permanece activo durante el drenaje. Al vencer cierra el transporte completo y
cancela sus tareas de streams, incluidos hermanos. No promete GOAWAY, HTTP 408 ni
`grpc-status` para un bloque incompleto. No impone un timeout idle entre frames
sin bloque abierto ni al payload DATA. Detalles y contador administrativo en
[HTTP2](HTTP2.md#headers-incompletos-plazo-de-entrada).

## Resultados observados

Pruebas locales del 16 de noviembre de 2026: macOS ARM64, Rust 1.88 y Docker
27.4 sobre Colima. El proxy amd64 se ejecutó bajo emulación; los backends Node
HTTP/2 y grpcio se ejecutaron nativamente. No se contactó Connectivity.

| Comprobación | Resultado | Evidencia |
|---|---|---|
| Rust, fmt, Clippy y build `--locked` | PASS: 29 pruebas, 19 proxy + 10 colector | [Unitarias](increment5-unit.txt), [Clippy](increment5-clippy.txt), [build nativo](increment5-native-build.txt) |
| HEADERS incompletos Docker ARM64 | PASS: 20 casos, claro y TLS | [Informe](increment5-http2-headers-arm64.json), [logs de acceso](increment5-http2-headers-arm64-access.jsonl) |
| HEADERS incompletos Docker amd64 | PASS: 20 casos, claro y TLS | [Informe](increment5-http2-headers-amd64.json), [logs de acceso](increment5-http2-headers-amd64-access.jsonl) |
| Control negativo Cauce 0.4 ARM64 | FAIL esperado: HEADERS incompletos no cierran dentro de 1,25 s | [Informe](increment5-http2-headers-negative-control.json) |
| Regresión HTTP/2 y gRPC ARM64 | PASS: 9 grupos + HUP/TERM | [Informe](increment5-http2-arm64.json) |
| Regresión HTTP/2 y gRPC amd64 | PASS: 9 grupos + HUP/TERM | [Informe](increment5-http2-amd64.json) |
| Regresión HTTP/1.1 nativa | PASS: 19 pruebas | [Log](increment5-http-regression.txt) |
| Regresión TLS/WebSocket Docker ARM64 | PASS: 7 grupos | [Informe](increment5-tls-regression.json) |
| JSON Schema | PASS: 204 eventos de estas ejecuciones | [Log](increment5-schema.txt) |

Los diez casos por transporte son: payload HEADERS truncado con END_HEADERS;
un solo byte de frame header; frame header por goteo; espera sin CONTINUATION;
CONTINUATION con frame header por goteo; CONTINUATION con payload por goteo;
trailers incompletos; bloque fragmentado válido seguido de idle y reutilización;
un único payload DATA recibido durante 900 ms; y cierre con stream hermano
activo seguido de una conexión nueva que responde 200.

La configuración de estas pruebas usa `client_headers_ms = 800`. Cada cierre
adversarial exige EOF/reset entre 700 y 1250 ms: ni un rechazo inmediato por
HPACK inválido ni un GOAWAY sin cierre cuentan como aprobación. El último goteo
ocurre a los 640 ms; renovar el reloj con ese byte excedería la cota superior.
Los 32 cierres observados en ambas arquitecturas estuvieron entre 804 y 857 ms.
Los resultados incluyen los tiempos individuales medidos por el cliente.

Para probar cancelación, primero se reciben los headers 200 de `/lifecycle`,
cuyo backend mantiene pendiente el body durante 1200 ms. Se inicia el bloque
incompleto en otro stream. El informe correlaciona el `x-request-id` recibido
con exactamente un registro `outcome=cancelled`, fase `transfer`, cero bytes de
respuesta y duración inferior a 1200 ms. La prueba unitaria del executor también
verifica que al soltar la conexión se abortan sus tareas y no se admiten tareas
nuevas después del cierre.

Los logs HTTP/2/gRPC contienen 85 eventos por arquitectura. Los logs TLS contienen
solo los 14 eventos de la instancia de esta ejecución, filtrando la historia del
volumen persistente. Los logs adversariales se correlacionan con los IDs de la
corrida y se filtran por instancia. No se crea un evento de acceso para HEADERS
que nunca pudieron parsearse como solicitud.

## Reproducción

Desde la raíz del producto, con Docker disponible (los arneses ejecutan
`colima start` si no responde):

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --release --locked
CAUCE_BIN="$PWD/target/release/cauce" uv run --no-project --with jsonschema python tests/integration.py

docker build --platform linux/arm64 -t cauce:0.5-arm64 .
python3 scripts/test-http2-headers-docker.py --image cauce:0.5-arm64 --platform arm64
python3 scripts/test-http2-docker.py --image cauce:0.5-arm64 --platform arm64 --report-prefix increment5-http2

docker build --platform linux/amd64 -t cauce:0.5-amd64 .
python3 scripts/test-http2-headers-docker.py --image cauce:0.5-amd64 --platform amd64
python3 scripts/test-http2-docker.py --image cauce:0.5-amd64 --platform amd64 --report-prefix increment5-http2

uv run --no-project --with websockets==15.0.1 python scripts/test-tls-docker.py
```

Ejecutar secuencialmente las arquitecturas: cada arnés reutiliza su directorio
de certificados temporales. Los scripts crean proyectos Compose aislados y
retiran sus contenedores al acabar, salvo `--keep`. Sus informes se reemplazan
al repetir las pruebas; use `--report-path` para conservar otra ejecución del
arnés HEADERS. El arnés general admite `--report-prefix`.

El control negativo requiere conservar o reconstruir la imagen **0.4 desde su
paquete histórico**, no desde el árbol 0.5. Su salida no cero es deliberada:

```sh
python3 scripts/test-http2-headers-docker.py --image cauce:0.4-arm64 --platform arm64 \
  --report-path docs/increment5-http2-headers-negative-control.json
```

## Procedencia y límites

[Manifiesto](increment5-release.json) con hashes de fuentes, entradas de prueba,
evidencias y binarios. Las imágenes locales probadas son:

- ARM64: `sha256:185e43cc44c030022e4fcf2d8d7d5e4a59078269dde728b3f94d3270b9a735bd`.
- amd64: `sha256:8065775f156ffd9c7a45e22577eb670b1c3bb1acefc476995b6714f692a8b7a1`.

Los binarios de la regresión TLS coinciden con los de ARM64. No se añadieron
ni actualizaron dependencias; se conserva Cargo.lock y los avisos de terceros.
El colector no cambió: se repitieron sus diez pruebas unitarias, mientras sus
integraciones completas de [0.4](VALIDATION-4.md) permanecen como evidencia histórica.

El plazo empieza cuando el proxy lee los bytes, no en su llegada física al NIC.
El tiempo observado incluye scheduling y transporte local. Estas pruebas no
certifican conformidad RFC exhaustiva, resistencia DDoS, rendimiento productivo
ni carga sostenida; quedan pendientes fuzzing diferencial y stress prolongado.
Los plazos de solicitud/body y el watchdog upstream siguen aplicándose según
[HTTP2](HTTP2.md); no se promete liberación instantánea de uploads upstream
bloqueados que Hyper hubiera separado de respuestas ya terminadas.
