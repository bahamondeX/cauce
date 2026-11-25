# Validación local — incremento 6 (0.6.0)

Ejecutada en Docker/Colima Linux ARM64; el proxy amd64 se ejecutó mediante
emulación, con clientes y backends nativos. Son campañas acotadas, no una
certificación RFC, prueba de ausencia universal de fugas o resistencia DDoS.
El incremento añade instrumentos de observación; no cambia cuotas ni timeouts
predeterminados. El colector sigue siendo opcional e independiente.

## Código y regresiones

- 29 pruebas Rust (19 proxy, 10 colector), incluidas cancelación de tareas no
  ejecutadas y liberación de holds upstream tras uploads bloqueados.
- `cargo fmt --check` y `cargo clippy --all-targets --all-features -- -D warnings`.
- 19 pruebas HTTP/1.1 con sockets nativos, sin errores.
- 190 eventos de acceso H2/HEADERS validados contra JSON Schema ([salida](increment6-schema.txt)).
- 9 grupos HTTP/2/gRPC por arquitectura, además de HUP/SIGTERM: multiplexación,
  SETTINGS, rechazo del quinto stream ante cuota 4, traducción HTTP, las cuatro
  modalidades gRPC, trailers, GOAWAY y deadlines con ventanas cero.
- 20 casos HEADERS por arquitectura (TCP y TLS), incluidos payload truncado,
  CONTINUATION, trailers, goteo de bytes, cancelación de un hermano activo,
  reutilización válida y DATA lento que no debe activar el timeout de headers.

Registros: [unit](increment6-unit.txt), [clippy](increment6-clippy.txt),
[HTTP/1](increment6-http-regression.txt),
[H2 ARM](increment6-http2-arm64.json), [H2 amd64](increment6-http2-amd64.json),
[HEADERS ARM](increment6-http2-headers-arm64.json),
[HEADERS amd64](increment6-http2-headers-amd64.json).
Las integraciones completas del colector/TLS-WebSocket anteriores siguen siendo
históricas; aquí se repiten las unitarias del colector y TLS de HTTP/2.

## Fuzzing instrumentado

El target de [fuzz/](../fuzz/README.md) incluye el mismo `src/transport.rs` de
producción. LibFuzzer/AddressSanitizer compara entrega completa y fragmentada,
estados esperados de secuencias estructuradas y reloj sintético en los bordes
0/999/1000/1001 ms. Incluye HEADERS, CONTINUATION correcto/incorrecto, DATA,
payload truncado, frame header parcial, preface y passthrough HTTP/1.

La campaña fija seed 60616, 100.000 ejecuciones como máximo, 60 s, entrada de
4 KiB y timeout de 5 s por entrada. Terminó las 100.000 ejecuciones sin artifact
de crash. LibFuzzer reportó cov=201 y ft=343; son contadores de instrumentación,
no porcentaje de conformidad. El informe de función cubre 62/146 edges del
observador y 8/45 del helper temporal. No se fuzzearon directamente HPACK,
rustls ni todo el estado de Hyper. La campaña no duró 60 s: alcanzó antes el
límite de ejecuciones (1 s según su salida; el build es independiente).

```sh
./fuzz/run-docker.sh
```

La imagen, Rust/nightly, cargo-fuzz, lockfile, corpus inicial y presupuestos
están fijados en el harness. Cada ejecución escribe corpus/artifacts en un
subdirectorio nuevo de `work/fuzz6/runs/`; consulte el README de fuzz para replay.
Fuentes del mecanismo: [Rust Fuzz Book](https://rust-fuzz.github.io/book/cargo-fuzz/guide.html)
y [libfuzzer-sys](https://github.com/rust-fuzz/libfuzzer).

## Estrés real y recursos

Cada arquitectura ejecutó tres olas de 32 clientes contra TCP y TLS (384
intentos de carga entre ambas arquitecturas), más pruebas de cancelación,
cuota por conexión y 26 casos de wire por modalidad (104 en total).
Seed de mutación: 1592639710. Un frame desconocido debe permitir PING/ACK en
el mismo socket; las mutaciones inválidas deben producir error terminal según
su caso. Un payload DATA truncado puede esperar porque no tiene deadline de
HEADERS. El HEADERS truncado exige EOF a los 300 ms configurados y aumento de
exactamente uno del contador de timeout; recibir SETTINGS no cuenta como cierre.

Cuotas de la fixture: 16 clientes, 8 solicitudes, 2 conexiones físicas,
4 streams por socket upstream y 4 por socket entrante. Cada ola alcanzó 8
solicitudes y 2 conexiones físicas; se comprobaron todos los samples contra
las cuotas. El backend registra su propia actividad y confirma hasta 8 streams
sin superar 2 conexiones. La prueba de una conexión entrante mantiene cuatro
streams y envía un quinto frame HEADERS por fuera del gate cooperativo del
cliente, exigiendo reset/GOAWAY sin admisión de una quinta solicitud.

También se ejercita una ventana downstream cero seguida de RST_STREAM, y
respuesta temprana 413 con ventana upstream cero. Después de cada fase se
espera de forma acotada hasta que clientes, solicitudes, streams, tareas y
conexiones físicas vuelvan a cero. Luego una petición nueva debe funcionar.
La salida JSON conserva gauges, muestras del backend, `/proc/1` y estado Docker.

Se miden FD, threads y RSS desde el namespace PID del proxy. La memoria de
cgroup se lee **dentro del contenedor proxy**, separada del RSS y de la memoria
del cliente de pruebas. Se comprueban OOM/reinicios y que el proceso siga vivo.
Los márgenes del harness permiten cachés/allocator: FD final ≤ baseline+8 y
pico ≤ baseline+128; threads final ≤ baseline+2 y pico ≤ baseline+8; RSS final
≤ baseline+64 MiB y pico ≤ baseline+128 MiB. Cgroup ≤256 MiB y valor tras la
quiescencia ≤ baseline+64 MiB. Estos márgenes son detectores de regresión,
no pruebas matemáticas de fuga ni estimaciones de capacidad de producción.

Resultados completos: [ARM64](increment6-http2-stress-arm64.json) y
[amd64 emulado](increment6-http2-stress-amd64.json). Los samples son discretos:
los picos entre lecturas pueden no observarse; los semáforos estructurales
implementan las cuotas con independencia del muestreo.

```sh
docker build --platform linux/arm64 -t cauce:0.6-arm64 .
python3 scripts/test-http2-stress-docker.py --image cauce:0.6-arm64 --platform arm64
# Repetir con imagen amd64 construida mediante el Dockerfile cruzado:
docker build --platform linux/amd64 -t cauce:0.6-amd64 .
python3 scripts/test-http2-stress-docker.py --image cauce:0.6-amd64 --platform amd64
```

El runner inicia Colima si Docker no responde y elimina su proyecto Compose al
terminar. `--waves`, `--requests`, `--fuzz-cases` y `--seed` amplían la campaña
con límites explícitos. `--report` permite preservar cada corrida. Ejecute
arquitecturas secuencialmente: certificados y resultados temporales comparten
`work/http2-stress`. El manifiesto de release conserva hashes de código, pruebas,
imágenes y evidencia; claves temporales, builds y corpus generado no se empaquetan.

## Máximos observados en la corrida final

| Plataforma / transporte | FD pico / final | Threads pico / final | RSS MiB pico / final | Cgroup MiB pico |
|---|---:|---:|---:|---:|
| arm64 / clear | 26 / 12 | 8 / 8 | 6.00 / 6.00 | 3.36 |
| arm64 / tls | 23 / 12 | 7 / 7 | 7.12 / 7.12 | 3.62 |
| amd64 / clear | 25 / 12 | 8 / 8 | 26.45 / 26.45 | 24.63 |
| amd64 / tls | 28 / 12 | 8 / 8 | 34.26 / 34.26 | 31.68 |

Todas las cuotas activas terminaron en cero. Valores amd64 incluyen el coste de
ejecución emulada y no son una comparación de rendimiento entre arquitecturas.
Fuzz: [resumen](increment6-fuzz.json), [log completo](increment6-fuzz.log),
[toolchain](increment6-fuzz-toolchain.txt), [imagen](increment6-fuzz-image.txt).
