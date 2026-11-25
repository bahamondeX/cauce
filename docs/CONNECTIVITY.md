# Connectivity: contrato local y estado de integración

Revisión del 17 de noviembre de 2026. **Existe un adaptador local compatible con
`proposal-1`; no se acredita un endpoint de ingesta desplegado.** La conclusión
anterior de Cauce sobre ausencia de contrato correspondía al árbol remoto
inspeccionado, no al trabajo local posterior. Esa evidencia se conserva en
[CONNECTIVITY-HISTORICAL.md](CONNECTIVITY-HISTORICAL.md).

## Fuentes y alcance

Se leyeron únicamente `PROXY_INGEST_CONTRACT.md`, `PHASE2B_DURABILITY.md` y
`PHASE2B_CONSUMER.md` del worktree local del equipo Connectivity. Sus rutas y hashes
están en [connectivity-local-contract-review.json](connectivity-local-contract-review.json).
Las afirmaciones de implementación del servidor que siguen proceden de esos
documentos; no equivalen a una inspección de código ni a repetir sus pruebas desde
Cauce. No se consultó ni modificó AWS, el proyecto o los servicios de Connectivity.

Los documentos de durabilidad/consumidor registran pruebas de un colector Rust real
copiado a un entorno aislado: entrega por HTTPS, pérdida del ACK tras persistir,
reenvío con el mismo UUID y dos eventos únicos. La validación más reciente informa
298 pruebas Python y tres de frontend, con integraciones locales opcionales
habilitadas. Es evidencia declarada por el equipo servidor. Moto, DynamoDB Local y
DuckDB no acreditan las garantías de S3, IAM, Athena o un despliegue de producción.

## Contrato para el colector

| Elemento | Contrato local documentado |
|---|---|
| Petición | `POST /v1/ingest/cauce-proposal`, `Content-Type: application/json` |
| Envelope | `contract_version: "proposal-1"`, UUID `batch_id`, array `events` |
| Autenticación | Bearer de máquina expirable; el servidor conserva su digest y resuelve identidad, namespace y mapping |
| ACK | HTTP **200**, exactamente `batch_id` y `accepted_count`; ID idéntico y cantidad total |
| Límites | Máximo 128 eventos y 524288 bytes de cuerpo descomprimido; Cauce envía JSON sin comprimir |
| Tiempo de origen | Timestamp UTC, hasta 30 días de antigüedad y cinco minutos de adelanto |
| Autorización | Usuario, host, ownership, namespace y token activos, alcance `proxy:ingest`, sin pausa global; se revalida al persistir |
| Idempotencia | Recibo por usuario + namespace + batch; eventos canónicos deduplicados dentro del namespace; mismo ID con contenido distinto se rechaza |
| Significado del ACK | Persistencia durable y outbox, **no** finalización de análisis ni éxito de la aplicación |

La otra ruta documentada, `/v1/ingest/proxy-events`, admite NDJSON y aceptación
parcial. **No es el destino del colector de Cauce.** No se añade un namespace al
evento ni a la URL para conceder autorización: el token y mapping del servidor
deciden el destino. Las rutas y upstreams enviados deben figurar en ese mapping.
Cambiar la configuración/versionado del host requiere un nuevo namespace según
el contrato local. La rotación de tokens debe preservar la misma identidad y
namespace para reutilizar recibos y el spool.

## Configuración explícita y secretos

El arranque normal y Compose continúan offline, con `delivery = "spool"` y sin
red para el colector. [collector-connectivity.toml](../examples/collector-connectivity.toml)
es una plantilla offline con los límites acordados. Antes de habilitar envío,
el operador necesita URL HTTPS real del entorno autorizado, CA si es privada,
token emitido por ese servidor y mapping de las rutas de los eventos sintéticos.
Un hostname `.invalid` en un ejemplo es un marcador, no un endpoint de Connectivity.

La credencial se monta como archivo regular de solo lectura, legible únicamente
por la identidad del colector; no se coloca en TOML, variables de Compose, argumentos,
logs o repositorio. Cauce vuelve a leer el archivo en cada intento, valida cadena
y hostname TLS, rechaza redirects y no usa proxies del entorno. No existe modo
TLS inseguro. Una CA adicional amplía las raíces públicas del cliente; no es un
pin exclusivo. Consulte [COLLECTOR.md](COLLECTOR.md).

Cauce no emite tokens, implementa login interactivo ni verifica por sí solo que un
token nuevo corresponde al namespace anterior. Ese control pertenece al servidor
y al aprovisionamiento del operador. La base fija el path de origen y endpoint;
no cambie la identidad de entrega de una cola existente. Migre con exportación y
estado separado cuando cambie de namespace.

## Fallos y recuperación

El colector conserva el lote y sus IDs hasta recibir el ACK completo. HTTP 401/403,
429, fallo de red/TLS, ACK perdido o inválido no confirman la cola. En modo continuo
se reintenta con backoff; en `--once` el intento fallido termina con error. Resolver
la autorización y rotar el archivo de token permite reintentar el mismo lote.

El spool pausado por cuota conserva lo ya persistido y deja de avanzar el origen.
El drenaje tras restablecer el destino necesita que los logs pendientes sigan
retenidos. Una política remota puede rechazar un lote completo por una ruta no
mapeada, contenido inválido o timestamp demasiado antiguo: Cauce no elimina ese
lote automáticamente. El contrato documenta como pendiente el lookup autenticado
del recibo antes de revalidar la ventana temporal; no se promete que un reenvío
después de 30 días se acepte. Los límites de tasa y cuota del servidor también
pueden mantener una cola retenida: backoff no constituye admisión garantizada.

## Prueba independiente con el adaptador real

Completada posteriormente con copia autorizada e inalterada del checkout del
servidor: ocho escenarios de entrega, replay, autorización y recuperación.
[Validación, evidencia y comando reproducible](VALIDATION-CONNECTIVITY-REAL.md).
El adaptador y autenticador son reales; AWS se simula con Moto. Esta evidencia
local no acredita el despliegue ni las propiedades de AWS real.

## Producción pendiente

Según los documentos suministrados, aún faltan las APIs del propietario para
namespaces/tokens y las rutas/throttling del gateway; despliegue y pruebas reales
del consumidor Iceberg/Athena, dispatcher, DLQ e IAM; integración de borrado,
retención y reconciliación; y exposición en marts/dashboard/herramientas. El flujo
de borrado local termina esperando reescritura y retención de Iceberg: no acredita
borrado físico. No se modifican esos servicios desde Cauce ni se sustituye su
autenticación por una simulación presentada como real.
