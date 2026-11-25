# Integración local real Cauce → Connectivity

Validada el 17 de noviembre de 2026. **PASS: 8 escenarios**, con el collector
Cauce 0.9.1 real sobre HTTPS y una copia inalterada del adaptador, autenticador
y sink de Connectivity. S3 y DynamoDB están simulados por Moto. Esto no es
un despliegue cloud ni una prueba de durabilidad física de AWS.

## Evidencia

- [Primera corrida completa](connectivity-real-run3/report.json).
- [Confirmación independiente](connectivity-real-confirmation/report.json).
- Cada reporte identifica imagen/binario, hashes de las fuentes copiadas, límites
  del contenedor, solicitudes, recibos y outbox, y limpieza.
- Las corridas 1 y 2 fallaron en el arranque TLS del laboratorio. Se corrigió el
  certificado efímero para cumplir la verificación estricta de Python 3.13
  (KeyUsage/SKI del CA y BasicConstraints/AKI del servidor), sin desactivar
  verificación de certificados. No se cuentan como pruebas aprobadas.

## Casos aprobados

1. Entrega de dos eventos, recibo/outbox únicos y spool drenado.
2. ACK perdido tras commit: reintento con mismo batch y payload, sin nuevos
   recibos, outbox, versiones de objetos ni coste registrado.
3. Token expirado: 401, lote retenido; rotación y recuperación.
4. Token revocado: 401, lote retenido; rotación y recuperación.
5. Namespace deshabilitado: 401, lote retenido; restauración y recuperación.
6. Mapping a namespace no autorizado: 401, lote retenido; recuperación.
7. Revocación del token antes del commit: 403, sin recibo/outbox; recuperación.
8. Deshabilitación del namespace antes del commit: 403, sin recibo/outbox;
   recuperación.

Todas las recuperaciones mantienen ID y bytes del batch. Se verifica el conjunto
exacto de eventos aceptados y la integridad SQLite. Las decisiones de autorización
las toma el código real del servidor, no un receptor que devuelve 401/403 fijos.
El harness sólo inyecta estados en almacenamiento simulado y pérdida del ACK.

## Reproducir

Desde la raíz de Cauce, con Docker y las imágenes locales disponibles:

```sh
# Sólo si falta la imagen de dependencias del laboratorio:
docker build -t cauce-connectivity-real-lab:local tests/connectivity_real
python3 scripts/test-connectivity-real-docker.py \
  --reference /ruta/al/checkout/connectivity-ingest-worktree \
  --report-dir work/connectivity-real-nueva-corrida
```

El directorio de reporte debe ser nuevo. El checkout de referencia se copia con
lista explícita de archivos y se verifica por SHA-256 antes/después. No se cambia
Connectivity. El contenedor ejecuta sin red externa, como UID 10001, raíz de sólo
lectura, sin capabilities y con 1 CPU/512 MiB/96 procesos; tmpfs de 32+16 MiB.
Los tokens y claves TLS sintéticos sólo existen en tmpfs; se verifica que no
aparezcan en logs. Se eliminan contenedor y temporales propios, preservando los
servicios que ya estaban activos.

## Límites

No cubre login/aprovisionamiento de usuarios reales, gateway/IAM, AWS real,
Athena/Iceberg, borrado físico, dashboard integrado ni carga/contención productiva.
No reduce los pendientes del alcance original (mTLS, ACME, health checks activos,
entre otros). El runtime conserva su modo offline por defecto.
