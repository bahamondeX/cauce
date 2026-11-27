# Cauce: a historical CPU/latency tradeoff

This experiment concerns an **unreleased historical Cauce build**. It is not a benchmark of the downloadable version, the latest development tree, HTTPS, or a general claim that Cauce outperforms another proxy.

## What was measured

The test ran Cauce and NGINX OSS 1.30.8 in a virtualized AMD64 Linux environment. Both used one worker, cleartext HTTP/1.1 and disabled access logs. At 1,000 offered requests/s, seven matched pairs qualified. Each target had direct-backend controls. The independent audit covered 350,000 measured requests and 140,000 warm-up requests for the 14 target cells.

| Metric at matched load | Cauce | Comparison proxy |
| --- | ---: | ---: |
| Process CPU per successful request | 40.0 µs | 46.0 µs |
| p99 latency | 152.90 µs | 147.03 µs |

Cauce used **13.0% less CPU per successful request** in this specific workload, with a higher p99. The median within-pair p99 difference was +5.52 µs, with a 95% interval of +3.31 to +10.26 µs. The extreme tail at p99.9 was inconclusive. These numbers are meaningful only as a matched comparison under the stated conditions.

## Capacity was a separate test

At one worker, the 60-second confirmation established **7,500 successful requests/s** for Cauce and **at least 8,000/s** for the comparison proxy. Cauce's 8,000/s attempt failed its warm-up gate. These are confirmed points, not saturation ceilings. The test did not establish multicore scaling, broad feature parity, or a current-release performance advantage.

## Versión breve en español

En una compilación histórica no publicada, con HTTP/1.1 sin TLS, un worker, logs desactivados, Linux AMD64 virtualizado y siete pares a 1.000 solicitudes ofrecidas/s, Cauce usó **13,0% menos CPU por solicitud exitosa** (40,0 frente a 46,0 µs). Su p99 fue mayor (152,90 frente a 147,03 µs). En la prueba de capacidad por separado se confirmaron 7.500 éxitos/s para Cauce y al menos 8.000 para el proxy de referencia. No es una medición de la versión pública ni una conclusión sobre TLS, multicore o superioridad general.

[Back to Cauce](../README.md)
