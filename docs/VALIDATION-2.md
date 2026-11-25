# Validación del incremento 2 — 16 noviembre 2026

Código Cauce 0.2.0, Rust 1.88; Hyper 1.12, rustls 0.23.46, tokio-rustls 0.26.6, ring 0.17.14.
Docker sobre Colima ARM64. Cliente Python, backend Python websockets 15.0.1 y servidor HTTPS
de prueba; conexiones y certificados reales. No mocks del transporte o del pool.

| Prueba ejecutada | Resultado | Evidencia |
|---|---|---|
| Formato, Clippy con warnings como errores | Aprobados | increment2-checks.txt; corrección final del handshake también pasó Clippy |
| Unitarios Rust | 5/5 | increment2-unit-final.txt |
| Regresión HTTP nativa macOS | 19/19, sin skips | increment2-integration.txt |
| Regresión HTTP Linux ARM64 release | 19/19, sin skips | increment2-linux-integration.txt |
| TLS/WSS principal Docker | 7 casos aprobados | increment2-tls-regression.json, increment2-docker-tests.txt |
| Ciclo de vida TLS | 11 casos aprobados | increment2-tls-lifecycle.json y JSONL |
| Ciclo de vida WebSocket | 9 casos aprobados | increment2-websocket-edges.json y JSONL |
| Esquema JSONL 1.0 | 170 eventos válidos | increment2-schema-results.txt; el volumen incluye iteraciones previas |
| Imagen ARM64 | Construida y probada | increment2-docker-build.txt |
| Imagen amd64 | Construida; HTTP y WSS probados bajo emulación ARM64 | increment2-amd64-build.txt, increment2-amd64-smoke.json, increment2-amd64-tls.json |

TLS: se verificaron TLS1.2/TLS1.3 de entrada, ALPN HTTP/1.1, rechazo de h2-only,
certificado de entrada con nombre incorrecto, CA upstream no confiable, nombre upstream
incorrecto, certificado backend caducado, timeout de handshake en ambos sentidos,
recarga con PEM inválido conservando servicio, cambio de certificado observado por
fingerprint y cambio de CA que drena sockets antes confiables. El timeout upstream de
300 ms devolvió 504 en aproximadamente 305 ms en esta prueba local.

HTTPS persistente: dos peticiones reutilizaron el mismo socket del backend; los eventos
asociados por x-request-id mostraron tls_ms medido en el primer intento y null en el
reutilizado. Los JSONL registran rechazos TLS con tls_handshake_error/timeout.

WebSocket: eco binario fragmentado de 4 MiB, ping/pong, ocho sesiones concurrentes,
cierre normal 1000, desconexión abrupta y recuperación de capacidad, segunda conexión
rechazada 503 mientras la primera retiene la cuota upstream, idle, actividad que renueva
idle y vida máxima independiente, SIGTERM con túnel abierto. Con shutdown_ms=500 el
proceso/túnel cerró en unos 649 ms desde el cliente, incluyendo docker CLI/red/logger.
Se ejercitaron ws→ws, ws→wss, wss→ws y wss→wss. No se afirma inspección de frames ni
un Close sintetizado al abortar por timeout. `complete` conserva semántica de EOF.

La primera interoperabilidad con Python detectó Content-Length:0 añadido por el body
HTTP vacío al handshake. Se corrigió para enviar un cuerpo explícitamente terminado,
y se mantuvo ese backend en la prueba hasta pasar. El harness también se corrigió para
certificados de prueba compatibles con validación estricta (AKI, SKI y KeyUsage) y para
sincronizar cambios de archivos compartidos con Colima. Vistas truncadas de TOML observadas
al recrear contenedores motivaron nombres únicos en fixtures efímeras y verificación de
hash antes de HUP; la configuración inválida siempre falló de forma cerrada.

Estas son pruebas funcionales locales, no una auditoría externa, un test de carga prolongado
ni una comparación de rendimiento 0.2. La suite completa TLS/WS se ejecutó en ARM64;
amd64 recibió smoke por emulación, no hardware x86. mTLS/ACME, HTTP2/gRPC, compresión WS,
colector e integración Connectivity siguen pendientes. No se publicaron repositorios,
registros ni GitHub Actions. El histórico 0.1 permanece en VALIDATION.md y bench/RESULTS.md.
