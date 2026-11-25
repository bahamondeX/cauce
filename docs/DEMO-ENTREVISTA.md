# Demo de entrevista: Cauce + Connectivity

## Ejecutar ahora

En Terminal del Mac:

```sh
cd "$HOME/Documents/Codex/2026-11-16/files-pasted-by-the-user-act/outputs/cauce"
python3 scripts/demo-connectivity-local.py
```

La demo dura aproximadamente 15–20 segundos con Docker listo y las imágenes
locales ya disponibles. No construye ni descarga imágenes, no inicia servicios
existentes, no publica puertos y no llama a AWS. Puede repetirse: cada ejecución
usa un laboratorio temporal nuevo y lo elimina al terminar.

**Ensayada el 17/11/2026: 8/8 PASS, 16 eventos únicos, 8 recibos y 8 trabajos outbox.**
[Salida del ensayo](interview-demo-output.log).

Si se movió el checkout de Connectivity:

```sh
python3 scripts/demo-connectivity-local.py --reference /ruta/connectivity-ingest-worktree
```

Si falla el precheck, comprueba que Docker esté disponible y que existan las
imágenes `cauce:0.9.1-health-frontier-arm64` y
`cauce-connectivity-real-lab:local`. No improvises un despliegue ni cambies AWS
antes de la entrevista. Puedes mostrar la salida del ensayo y su evidencia,
indicando honestamente que es una corrida guardada.

## Guion de tres minutos

**0:00–0:30 | Qué construí**

«Cauce es un reverse proxy con colector Rust opcional. El colector conserva sus
lotes y checkpoints localmente. Connectivity recibe eventos por HTTPS, verifica
identidad y namespace y registra aceptación más un trabajo pendiente de análisis».

**0:30–1:00 | Qué está ejecutándose aquí**

«Esta es una prueba local reproducible. El binario Rust y el código de API,
autorización y sink de Connectivity son reales. Los eventos son sintéticos y
S3/DynamoDB están simulados por Moto. No la presento como un despliegue AWS».

Ejecuta el comando. La demo reutiliza las pruebas de integración, no un backend
nuevo ni respuestas prefabricadas de autorización.

**1:00–2:00 | Tres propiedades que mostrar**

1. **Entrega normal:** dos eventos producen aceptación y se vacía la cola local.
2. **ACK perdido:** el servidor registra el lote, pero la respuesta se pierde.
   Cauce reintenta el mismo ID y contenido; no aparecen recibos, eventos ni
   trabajos outbox duplicados.
3. **Revocación y aislamiento:** token vencido/revocado o namespace deshabilitado
   bloquean la ingesta; se conserva el lote y se recupera al restaurar acceso.
   También se revoca entre la recepción y el commit para comprobar la revalidación.

**2:00–3:00 | Límites y aprendizaje técnico**

«El ACK prueba aceptación del sink local y creación del outbox, no que el análisis
haya terminado. Encontré y corregí un fallo: al truncarse un log con el mismo
inode, el tamaño podía retroceder sobre bytes ya observados. Ahora falla de forma
segura y conserva el checkpoint. Las dos regresiones fallaban antes y pasan
después. El árbol promovido pasó 39 tests Rust y los ocho casos de integración».

«Los health checks HTTP/1 opt-in ya excluyen y recuperan upstreams con umbrales.
No afirmo completos mTLS/ACME, health HTTP/2/gRPC, login real, dashboard end-to-end
ni despliegue/durabilidad AWS».

## Qué mirar en la evidencia

El comando imprime el directorio de cada ejecución, con:

- `demo-summary.json`: ocho casos, eventos únicos, recibos, outbox e imagen exacta.
- `report.json`: solicitudes, rechazo/recuperación, integridad de cola, hashes de
  fuentes e imagen, límites y limpieza del laboratorio.

El ensayo guardado está en
`work/demo-connectivity-20261117-171653-429201`.
El resumen puede mostrarse con `cat` sin enseñar tokens o claves; éstos sólo
existieron en el tmpfs del laboratorio. La copia del código Connectivity se
verifica por hashes antes y después.

**No es una demo visual del dashboard, ni un flujo continuo de tráfico vivo del
proxy al dashboard.** Esta demo comienza en logs sintéticos y termina en el
recibo/outbox local. Para el proxy y colector offline existe el script separado
`scripts/demo-offline-docker.py`; no confundir ambos recorridos.
