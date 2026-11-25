# Comparación reproducible con NGINX — incremento 7

Este laboratorio compara el binario de Cauce con [NGINX estable 1.30.6](https://nginx.org/en/download.html), usando
un backend independiente, el mismo generador y límites explícitos. Se ejecuta
localmente en Docker/Colima, sin publicar puertos. Los números describen esta
máquina, esta carga y estas versiones; no estiman capacidad de producción.

## Reproducir

Requiere Docker Engine ARM64 nativo, Compose v2 (se usó 2.32.4), Python 3.10+, OpenSSL y una VM
con al menos cuatro CPU y 2 GiB libres en el filesystem de resultados. La campaña publicada usa Colima con cuatro CPU y
aproximadamente 6 GiB. Mantenga libres los CPU de la VM de otros trabajos de carga.
Desde la raíz del proyecto:

```sh
# macOS/Colima; si el daemon no responde, el runner ejecuta colima start.
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
docker build --platform linux/arm64 -t cauce:0.7-final-arm64 .
python3 bench/increment7/run.py --image cauce:0.7-final-arm64
```

En Linux ARM64 se usa `/var/run/docker.sock` por defecto. El acceso al socket
Unix es necesario para leer contadores del Engine. El runner construye las
fixtures, crea certificados de prueba nuevos, descarga imágenes fijadas por
digest y escribe un directorio `bench/increment7/results/increment7-*` distinto
por campaña. Antes de cada escenario exige al menos 1 GiB libre y prueba escritura en Docker antes de medir. Estos umbrales no reservan espacio frente a otros procesos. No publica puertos. Desmonta contenedores, red y volúmenes de cada
grupo al terminar; conserva los datos y logs exportados. `--skip-build` permite
reutilizar fixtures ya construidas; sus IDs quedan en `images.lock.json`.

```sh
# Sustituir por el directorio que imprime el runner:
python3 bench/increment7/summarize.py bench/increment7/results/increment7-FECHA-ID
uv run --no-project --with matplotlib==3.10.3 python bench/increment7/plot.py \
  bench/increment7/results/increment7-FECHA-ID --output docs/increment7-comparison.png
uv run --no-project --with aiohttp==3.11.18 python -m unittest discover \
  -s bench/increment7/tests -v
```

Por defecto: ocho escenarios, dos proxies, logs on/off, tres repeticiones,
8 s de ofertas medidas y 2 s de warmup por corrida (TLS frío omite warmup).
Cada prueba drena las respuestas antes de avanzar. `--seconds`, `--repetitions`,
`--warmup`, `--seed` y `--scenarios` permiten ampliar o acotar el experimento.
`--rate` sobreescribe todas las tasas y sirve para campañas separadas de carga;
una tasa demasiado alta puede invalidar una corrida por drops/cola del generador.
`--skip-preflight` es solo para diagnóstico y el auditor no acepta esa campaña.
Una interrupción manual puede dejar el cliente de carga hasta el cleanup del
grupo; compruebe los contenedores con prefijo `i7-` antes de otra medición.

## Contrato de comparación

| Recurso o comportamiento | Presupuesto común |
|---|---|
| Protocolo | HTTP/1.1 de entrada y hacia el backend |
| Proxy | 1 CPU, 256 MiB, 128 PID/threads, 1024 FD por proceso |
| Ejecución | Un worker NGINX; un worker Tokio en Cauce |
| Solicitudes simultáneas | 32; exceso HTTP 503 |
| Backend | Uno; hasta 32 conexiones en la fixture |
| Keepalive upstream | Activo; hasta 32 idle, 15 s de inactividad, 60 s de edad |
| Conexión upstream | 1 s |
| Lectura/escritura | 15 s |
| Body máximo | 16 MiB |
| Buffering de proxy | Desactivado para solicitud y respuesta |
| Reintentos | Desactivados |
| TLS | CA local, verificación de nombre y cadena en ambos saltos |

Antes de medir, la prueba de capacidad mantiene 32 solicitudes en el backend,
exige rechazo de la número 33 y comprueba recuperación. El backend cuenta
conexiones y solicitudes de forma independiente al proxy.

La equivalencia tiene un alcance concreto. `max_conns` de NGINX limita
conexiones **activas**, y `keepalive` limita sockets guardados, mientras Cauce
cuenta activas e idle juntas. Un worker, un único destino y la admisión de 32
solicitudes permiten comprobar el límite físico en esta fixture; no equivalen
las políticas generales del pool. Véanse los contratos oficiales de
[upstream](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) y
[limit_conn](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html).
NGINX cuenta solicitudes después de recibir headers completos; Cauce además
limita sockets de cliente. Las solicitudes de la carga tienen headers completos
y no rebasan el presupuesto común de sockets. El deadline absoluto de Cauce y
los timeouts de inactividad NGINX tampoco tienen semántica idéntica.

## Diseño de las mediciones

El generador es open-loop: ofrece llegadas según un reloj independiente de las
respuestas, con workers y cola acotados. Cada llegada deja una fila JSONL,
incluso si se rechaza en la cola o llega demasiado tarde para enviarse. La
latencia principal comienza en la llegada **programada**, e incluye jitter y
espera del generador. Se conservan también latencia de servicio, tiempo hasta
headers, primera entrega SSE, estado HTTP, conexiones de cliente nuevas/reusadas
y errores. Los percentiles de éxito se separan de los de todos los resultados;
un 503 rápido no mejora artificialmente el percentil de respuestas útiles.

Cada repetición caliente calienta el mismo pool que usará durante la medida;
el escenario frío omite warmup para conservar su definición. El escenario
frío crea sesiones y contextos TLS nuevos por solicitud y pide al backend cerrar
el socket después de responder. El modo caliente conserva keepalive. La fixture upstream usa TLS 1.2, sin tickets
ni caché de sesiones, para comparar aperturas frías completas en ambos proxies;
el salto cliente-proxy negocia TLS con las mismas capacidades del cliente.
No se extrapola esta carga fría upstream a TLS 1.3. El
backend ofrece respuestas de tamaño fijo, demora controlada, SSE y cuerpo grande
para lectores lentos. Las variantes de logs aplican la misma carga con registros
activados y desactivados. Ambos escriben archivos JSON en un volumen Docker local,
exportado después de medir y detener el proxy; el filesystem compartido de macOS
no recibe un write por solicitud. Los esquemas y el coste no son idénticos:
Cauce registra más campos y usa una cola asíncrona; NGINX escribe su access log
sin buffering explícito. NGINX no expone una espera de pool equivalente y ese
campo queda `null`. Los archivos exportados incluyen preflight, warmup y todas
las fases del grupo; no son exclusivamente la ventana medida. Se verifican
permisos, presencia, JSON válido y un recuento exacto de registros: ofertas medidas + solicitudes de warmup completadas + las 33 del gate. Se alterna el orden de proxies y se conserva la semilla
para el orden de escenarios.

CPU se obtiene de diferencias del contador de nanosegundos de cgroup dividido
por el tiempo transcurrido: 100% significa un núcleo. Se guardan también
throttling y CPU del generador/backend. Los endpoints del contador rodean la
ventana de carga y drenaje; incorporan el pequeño margen del polling y las
llamadas secuenciales al Engine. Sus timestamps se conservan por contenedor.
Una barrera final detiene el generador antes de serializar los datos, hasta
que se hayan tomado los contadores finales. Memoria cgroup es `memory_stats.usage`
del Engine, sin la resta de caché que aplica el CLI
[`docker stats`](https://docs.docker.com/reference/cli/docker/container/stats/).
Un sidecar separado, con `SYS_PTRACE` y el namespace PID compartido únicamente
para esta medición local, lee `/proc` para sumar RSS, FD y threads de los procesos
Cauce o NGINX, incluidos master y worker. La suma RSS puede contar páginas
compartidas más de una vez; no es PSS. El coste del sondeo queda fuera del
cgroup del proxy. El Engine se consulta aproximadamente cada 0,8 s y `/proc` cada 0,2 s.
Las muestras discretas pueden perder picos breves; los picos reportados son
máximos observados, no el máximo continuo garantizado.

Los contenedores de proxy se comprueban contra su configuración efectiva Docker;
OOM, reinicios, datos incompletos o fallo del gate invalidan una campaña. Los
resultados conservan inputs, versiones, imágenes, datos crudos y errores. Una
campaña fallida no se convierte en aprobada por tener algunas filas útiles.

## Alcance pendiente

Esta matriz no compara HTTP/2/gRPC contra NGINX, ni fallos de backend, reload,
disco lleno, redes WAN, múltiples workers, múltiples orígenes o producción.
Las pruebas funcionales HTTP/2 y de recursos están en
[VALIDATION-6](../../docs/VALIDATION-6.md). Throughput logrado a una tasa ofrecida
fija no equivale al máximo sostenible; para buscarlo hay que repetir a varias
tasas y verificar errores, cola, jitter y CPU del generador/backend.

Los comandos y resultados concretos de la campaña entregada están en
[VALIDATION-7](../../docs/VALIDATION-7.md).
