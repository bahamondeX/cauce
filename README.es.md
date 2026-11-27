![Cauce: proxy inverso autohospedado en Rust](docs/assets/cauce-cover-es.svg)

[English](README.md) · **Español** · [Ficha del producto (PDF)](docs/CAUCE-brief.pdf)

Cauce es un proxy inverso con licencia Apache-2.0 para equipos que quieren un punto de entrada para sus API HTTP y conexiones de larga duración. Instálalo en tu infraestructura, define rutas en YAML versionado y observa cómo circula el tráfico entre tus servicios.

## Qué puedes hacer

| Necesidad | Lo que ofrece la versión pública |
| --- | --- |
| Dar una entrada común a varios servicios | Enrutamiento por host exacto y por prefijo de ruta más largo, balanceo round-robin o por menor número de solicitudes en curso y conexiones reutilizables por TCP o sockets Unix. |
| Servir más que peticiones HTTP cortas | HTTP/1.1, HTTP/2, gRPC, WebSocket y Server-Sent Events. Los cuerpos pueden transmitirse sin almacenar toda la respuesta en memoria. |
| Cambiar rutas sin reinicios innecesarios | Valida YAML o TOML sin iniciar el servicio. Recarga cambios compatibles y material TLS de forma atómica; si la configuración no es válida, sigue operando la anterior. |
| Operarlo en tu infraestructura | HTTPS de entrada, TLS configurable hacia los servicios con verificación de certificado y nombre, cierre gradual, métricas Prometheus y logs JSONL con recolector opcional. |

## Pruébalo en local

Con Docker y Docker Compose:

```sh
git clone https://github.com/bahamondeX/cauce.git
cd cauce
docker compose up --build -d
curl -fsS http://127.0.0.1:8080/
curl -NsS --max-time 5 http://127.0.0.1:8080/sse
docker compose down
```

El ejemplo expone el proxy solo en la interfaz de loopback e incluye servicios de prueba. La prueba SSE tiene un límite de cinco segundos para no dejar abierta la terminal. Para compilar de forma nativa con Rust 1.88 o posterior:

```sh
cargo build --release --locked
cargo run --locked -- check examples/cauce.yaml
cargo test --locked
```

## Evidencia y límites

El [registro público de ingeniería](STATUS.md) documenta 44 pruebas unitarias Rust y 22 pruebas de integración con sockets reales en macOS ARM64 para el candidato allí descrito, además de validaciones acotadas en contenedores Linux ARM64. No son una garantía de capacidad en producción.

La versión pública es un **proxy inverso HTTP**. Esta versión no incluye HTTP/3 de cara al cliente, TLS mutuo, caché de respuestas, archivos estáticos, proxy genérico TCP/UDP ni FastCGI/uWSGI. No se promete paridad total de configuración, superioridad general de rendimiento ni una versión 1.0.

[Configuración](docs/CONFIGURATION.md) · [Arquitectura](docs/ARCHITECTURE.md) · [HTTP/2 y gRPC](docs/HTTP2.md) · [Observabilidad](docs/LOGGING.md) · [Licencia](LICENSE)

Una [prueba histórica en AMD64](docs/CAUCE-benchmark-note.md) documenta el compromiso entre CPU y latencia en una compilación no publicada.
