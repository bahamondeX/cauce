# Incremento 2 — Cauce 0.2

TLS de entrada y upstream con rustls, y WebSocket ws/wss sobre HTTP/1.1, implementados y
probados con contenedores reales. El listener de datos puede servir HTTP o HTTPS según
configuración. Administración permanece separada en loopback. [Validación](VALIDATION-2.md).

## Ejemplo con un comando

Desde la raíz del código, con Docker/Colima, OpenSSL y uv instalados:

```sh
uv run --no-project --with websockets==15.0.1 python scripts/test-tls-docker.py --keep
```

El comando ejecuta `colima start` si el daemon Docker no responde, construye las imágenes, genera una CA local y certificados de prueba con SAN,
verifica el perfil y lo deja activo. `compose.tls.yaml` publica 127.0.0.1:8443. El ejemplo
HTTPS es `https://localhost:8443/http`; el eco WebSocket es `wss://localhost:8443/ws`.

```sh
curl --cacert work/tls/ca.crt https://localhost:8443/http
# Un cliente Python debe usar ssl.create_default_context(cafile='work/tls/ca.crt').
docker compose -p cauce-tls -f compose.tls.yaml kill -s HUP proxy
docker compose -p cauce-tls -f compose.tls.yaml down
```

No instale la CA de prueba en el almacén global del equipo. Las claves se generan dentro
de `work/tls/`, excluido del paquete/Git; valen para evaluación local. El certificado tiene
validez de tres días. El directorio se monta de solo lectura en los contenedores. Las claves
leaf de esta fixture tienen permiso de lectura para UID 10001; en una instalación propia
use secretos y propietario/grupo adecuados. La clave de CA queda con modo 0600.

El ejemplo integra tres backends del mismo contenedor de pruebas: WSS en 8765, WS en 8764
y HTTPS persistente en 8766. Rutas `/ws`, `/plainws`, `/http`. El parser HTTP pequeño de
la fixture sirve únicamente para pruebas GET locales; no es un servidor de aplicación
para producción. Uvicorn/Node originales siguen disponibles en el Compose base.

## Configurar TLS

```toml
# Tabla de raíz: listener HTTPS. Omitirla conserva el listener HTTP.
[tls]
cert_file = "/etc/cauce/tls/fullchain.pem"
key_file = "/etc/cauce/tls/private-key.pem"

# Dentro de cada [[upstreams]]:
# address = "api.internal:8443"
# host = "api.example:8443"
# tls = { server_name = "api.example", ca_file = "/etc/cauce/tls/trust.pem" }
```

`server_name` define identidad verificada y SNI; Host se configura aparte. Cada upstream
confía únicamente en su bundle CA explícito. TLS 1.2/1.3, ALPN HTTP/1.1, sin bypass de
validación. No se negocia HTTP/2. TLS sobre un socket Unix se rechaza para impedir que una
opción declarada se ignore. HUP prepara certificados y CA antes del cambio atómico:
PEM inválido conserva generación previa. Cambiar contenido/camino PEM está permitido;
activar/desactivar TLS del listener requiere reinicio. Los pools anteriores drenan.

Montar el directorio de configuración evita el problema de mantener un inode antiguo
cuando se reemplaza atómicamente un fichero montado individualmente. El harness verifica
por SHA256 la visibilidad del fichero dentro de Colima antes de enviar HUP.

`limits.tls_handshake_ms` vale 5000 por defecto. `limits.websocket_max_ms` vale 3600000;
`stream_idle_ms` limita la inactividad combinada del túnel. Un WebSocket ocupa permisos
de cliente, petición y conexión upstream hasta terminar. No vuelve al pool HTTP.
Extensiones/compresión WebSocket desactivadas; ping/pong/fragmentos/Close se transmiten
entre endpoints. El backend controla Origin, autorización y tamaños de mensajes.

## Repetir la verificación

Después del comando inicial con `--keep`:

```sh
uv run --no-project --with websockets==15.0.1 python scripts/test-websocket-edges.py
python3 scripts/test-tls-lifecycle.py
# Esta última prueba usa OpenSSL 4 para emitir un certificado ya caducado.
uv run --no-project --with jsonschema python scripts/validate-logs.py \
  docs/docker-tls-websocket.jsonl docs/increment2-tls-lifecycle.jsonl \
  docs/increment2-websocket-edges.jsonl
```

Las pruebas de ciclo de vida crean contenedores efímeros y usan 18443/18444; los eliminan
al terminar. No ejecutar dos instancias del mismo script simultáneamente. El perfil básico
usa certificados compartidos, por lo que se ejecuta primero y no se regenera durante los
otros scripts. `--keep` deja servicios activos; omitirlo los detiene al terminar.

Para amd64, construir `docker build --platform linux/amd64 -t cauce-tls-proxy:amd64 .` y
luego ejecutar `uv run --no-project --with websockets==15.0.1 python scripts/test-amd64-tls.py`.
En este equipo eso utiliza emulación sobre ARM64. El Dockerfile conserva compilación cruzada.

Pendientes: mTLS, ACME, HTTP/2/gRPC, health checks activos, colector Connectivity,
compresión WebSocket y pruebas de carga prolongadas. No se presenta el benchmark histórico
HTTP 0.1 como medición de TLS/WebSocket 0.2.
