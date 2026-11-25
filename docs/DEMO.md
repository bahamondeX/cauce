# Demo local integrado: proxy y colector offline

Este recorrido enseña el proxy recibiendo solicitudes HTTP, escribiendo access
logs JSONL y al colector guardando esos eventos en un spool local con checkpoint.
No contacta Connectivity ni transmite registros fuera de la máquina. El perfil
del colector usa `network_mode: none` y `delivery = "spool"`.

## Ejecutar el demo aislado

Requisitos: Docker Engine, la imagen runtime `cauce:0.7-final-arm64` y la imagen
fixture `cauce-increment7-backend:local` ya presentes localmente. Este runner no
construye ni descarga imágenes. Desde la raíz de `outputs/cauce/`:

```sh
python3 scripts/demo-offline-docker.py
```

El runner crea una red privada y volúmenes con nombres únicos, no publica puertos,
genera solicitudes al proxy con un cliente efímero basado en el fixture existente,
y corre el colector en un contenedor separado con `network=none`. Comprueba que
las cuatro respuestas 200 aparecen una sola vez en la exportación tras varias
invocaciones/reinicios del colector, que los checkpoints permanecen en el spool y
que el proxy sigue respondiendo entre ejecuciones del colector. Al finalizar
elimina solo sus contenedores, red y volúmenes.

Deja el informe en `docs/increment8-demo.json` y la exportación JSONL en
`docs/increment8-demo-export.jsonl`. Se pueden elegir las imágenes y rutas con
`--proxy-image`, `--backend-image`, `--report` y `--export`. Si Docker no está
disponible, el runner intenta `colima start` y vuelve a comprobar el daemon.

## Recorrido manual con Compose

La alternativa manual usa el proyecto estándar y expone el proxy en
`127.0.0.1:8080`:

```sh
docker compose -p cauce-demo --profile collector up --build -d
curl -i http://localhost:8080/
curl -N http://localhost:8080/sse
```

Ese puerto puede estar ocupado por `cauce-local` u otro servicio. Si `cauce-local`
ya está ejecutándose, reutilízalo y prueba `http://localhost:8080/` sin iniciar
un segundo proyecto Compose; en otro caso libera 8080 antes de arrancar el modo
manual. Usa `localhost` en la URL porque la ruta configurada exige el header
`Host: localhost:8080`. El runner aislado de arriba no tiene ese requisito.

El proxy reparte rutas entre fixtures HTTP locales `python` y `node`. El colector
lee el JSONL del proxy y persiste envelopes en el volumen Docker `collector-state`.

Para el modo manual, detén el colector antes de consultar su SQLite y mantener un
único escritor:

```sh
docker compose -p cauce-demo --profile collector stop collector
docker compose -p cauce-demo --profile collector run --rm --no-deps collector \
  --config /etc/cauce/collector.toml --status
docker compose -p cauce-demo --profile collector run --rm --no-deps collector \
  --config /etc/cauce/collector.toml --export stdout
```

La salida exportada son lotes de spool JSONL; el offset significa que el evento
quedó persistido localmente, no que un servicio remoto lo aceptó. Para revisar
logs operativos del proxy: `docker compose -p cauce-demo logs proxy`.

Al terminar, `docker compose -p cauce-demo --profile collector down` retira contenedores y red, pero
conserva volúmenes. No uses `down -v` si deseas guardar logs y checkpoints.

## Recorridos de protocolo locales

El proyecto también incluye fixtures para TLS de entrada/backend,
HTTP/2/gRPC y lectura de esos eventos por el colector. Son entornos locales
autocontenidos; no cambian el contrato remoto de Connectivity:

- HTTP/2, gRPC, cleartext y TLS: `compose.http2.yaml`, configuraciones en
  `examples/http2/` y el runner `scripts/test-http2-docker.py`. El runner crea
  certificados de prueba bajo `work/http2/tls/` y ejecuta los clientes/backend
  locales.
- Pipeline HTTP/2 a colector offline: `scripts/test-http2-collector.py`, que
  consume un JSONL local y guarda una evidencia bajo `docs/`.
- TLS y WebSocket: `compose.tls.yaml` y `examples/tls/`.
- Transporte HTTPS experimental `proposal-1` contra un receptor de prueba:
  `tests/collector/sink.py` y `python3 scripts/test-collector.py`. El token y
  certificado allí son fixtures efímeros, no credenciales de Connectivity.

Los runners de prueba pueden crear imágenes, contenedores y archivos de trabajo;
ejecútalos solo cuando quieras hacer esas pruebas. Ninguno requiere configurar
una URL real de Connectivity. No habilites `delivery = "proposal-http"` con
datos reales basándote en este demo: antes hace falta el contrato y la
autorización del servidor descritos en [CONNECTIVITY.md](CONNECTIVITY.md).

## Qué demuestra y qué no

El recorrido principal demuestra localmente la captura de metadatos de solicitudes
por el proxy y su persistencia acotada en spool. Los fixtures de protocolo pueden
demostrar TLS/HTTP/2/gRPC y un intercambio HTTPS de prueba. No demuestran una
ingesta desplegada en Connectivity, identidad/autorización multiusuario,
retención remota ni aceptación de registros por un servicio externo.
