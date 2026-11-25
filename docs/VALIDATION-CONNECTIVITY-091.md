# Parche 0.9.1: contrato local de Connectivity y cliente HTTPS

17 de noviembre de 2026, Docker/Colima ARM64. **Cliente validado contra un
receptor sintético; montaje independiente del adaptador real todavía pendiente.**
No es una campaña de benchmarks ni un despliegue de Connectivity.

## Cambios

El contrato local del equipo Connectivity ya define `POST
/v1/ingest/cauce-proposal`, envelope `proposal-1`, autenticación de máquina y un
ACK HTTP 200 con exactamente `batch_id` y `accepted_count`. Cauce antes aceptaba
cualquier 2xx con un ACK válido. 0.9.1 exige 200, mantiene el lote ante otros
estados y elimina los detalles de serde del error de parseo: un campo inesperado
del JSON de respuesta podría contener una credencial reflejada y acabar en stderr.

Comparación de fuentes contra `cauce-increment9.tar.gz`: únicamente cambian
`Cargo.toml`, `Cargo.lock` y `src/collector.rs` en el runtime. El motor del proxy
no cambia. Los informes y archivo 0.9.0 siguen siendo históricos; sus hashes no
se atribuyen a este árbol 0.9.1. No se repitieron los benchmarks ni toda la campaña
de fallos en este parche.

La plantilla [collector-connectivity.toml](../examples/collector-connectivity.toml)
sigue offline, sin credencial ni endpoint activo. El operador debe configurar
URL/CA/token reales del entorno autorizado. Contiene límites de 128 eventos y
524288 bytes, polling de 10 segundos y backoff de 10–300 segundos para el perfil
de baja frecuencia documentado por el servidor. No garantiza cuotas globales,
por usuario o compartidas con otros colectores. Bajar los límites no reserializa
los lotes ya persistidos: revise un export de una cola preexistente antes de
habilitar el destino. Nunca borre la base para eludir un lote rechazado.

## Verificación ejecutada

- Build release Docker, Rust 1.88, `--locked`, ARM64 nativo.
- `cargo fmt --check`, `cargo clippy --release --locked --all-targets ... -- -D warnings`.
- **33 tests Rust**: 22 del proxy y 11 del colector. La prueba nueva verifica que
  toda la cadena de error omite una cadena sintética situada como clave y valor
  de un ACK inválido.
- **Nueve casos Docker** del binario real con receptor Python HTTPS, certificados
  temporales y red interna sin puertos publicados. Sink y clientes con 1 CPU,
  256 MiB y 128 PIDs; el colector usa filesystem raíz de solo lectura y sin capabilities.

| Caso | Observación y criterio |
|---|---|
| HTTP 201 y 202 | Salida de error, un evento retenido; mismo batch aceptado al volver a 200 |
| HTTP 401 y 403 | Conserva un evento y el UUID; reintento 200 drena a cero |
| ACK con campo extra | Se rechaza; nombre y valor no aparecen en stderr; recuperación con ACK válido |
| ACK perdido después de aceptar | Reenvía envelope idéntico; un solo ID añadido al conjunto del fixture; cola final cero |
| Token rotado en archivo | Relee credencial; conserva batch/event ID y drena tras aceptación del fixture |
| CA no confiable | Falla antes del request HTTP; conserva el lote |
| Hostname incorrecto | Falla antes del request HTTP; conserva el lote |

Estos son nueve casos contando por separado cada estado HTTP y fallo TLS. Los
casos usan un evento por lote. Se configuraron los máximos 128/524288 para los
casos HTTP; **no se midieron aquí sus fronteras exactas ni la capacidad máxima**.
401/403 son respuestas inyectadas: no acreditan expiración/revocación real de
un token ni una decisión de autorización de namespace en Connectivity. El conjunto
de IDs del fixture prueba el reenvío estable del cliente, no la durabilidad del
sink real.

Imagen local: `cauce:0.9.1-connectivity-arm64`.
ID: `sha256:a7950e7c0d8120b4db0dd01d21a06e18f9b1f2564de93ad2cf1adde8d7561042`.

Evidencia: [reporte Docker](connectivity-091-client-docker.json),
[checks Rust](connectivity-091-rust-checks.log), [build](connectivity-091-build.log),
[fuentes, binario y limpieza](connectivity-091-verification.json).
No quedan contenedores ni redes de estos tests; los servicios preexistentes siguen
activos. Los archivos temporales de la corrida aprobada se eliminaron. No se
incluyen claves privadas ni tokens del laboratorio en esta entrega.

## Reproducción local

Desde la raíz del producto, con Docker accesible:

```sh
docker build --platform linux/arm64 -t cauce:0.9.1-connectivity-arm64 .
docker build --platform linux/arm64 --target checks \
  -f docs/increment9-check.Dockerfile .
python3 scripts/test-collector-connectivity-contract.py \
  --image cauce:0.9.1-connectivity-arm64 \
  --temp-dir /tmp/colima \
  --report work/connectivity-contract-current.json
```

El script ejecuta `colima start` si Docker no responde. En Colima se usa
`/tmp/colima`, compartido por defecto con la VM; `/tmp` arbitrario del host no
está necesariamente compartido. En Linux se puede usar `--temp-dir /tmp`.
El test reutiliza el helper de `scripts/test-collector.py`, pinnea la imagen de
Python y limpia sus propios recursos. No toca los servicios del equipo Connectivity.

## Integración del servidor: actualización

La limitación de acceso de la revisión inicial se resolvió con la copia autorizada
posterior. Ya se ejecutaron desde Cauce ocho casos contra el adaptador,
autenticador y sink reales en laboratorio aislado con Moto. Véanse la
[validación independiente y sus límites](VALIDATION-CONNECTIVITY-REAL.md).
Esto no modifica ni reemplaza los nueve casos del cliente descritos arriba,
ni acredita despliegue productivo de Connectivity.
