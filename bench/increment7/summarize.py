#!/usr/bin/env python3
"""Audit and summarize a COMPLETE benchmark; retain per-run values and medians."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import datetime as dt
import json
import math
from pathlib import Path
import statistics


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def cpu_percent(before, after):
    elapsed = timestamp(after['timestamp']) - timestamp(before['timestamp'])
    delta = after['cpu_total_ns'] - before['cpu_total_ns']
    if elapsed <= 0 or delta < 0:
        raise ValueError('invalid CPU counter interval')
    return 100 * delta / (elapsed * 1e9)


def verify_image_architectures(engine_arch, images):
    aliases = {'aarch64': 'arm64', 'x86_64': 'amd64'}
    native = aliases.get(engine_arch, engine_arch)
    if native not in ('arm64', 'amd64'):
        raise ValueError(f'Unsupported Docker Engine architecture: {engine_arch}')
    for name, image in images.items():
        arch = image['architecture']
        if image['os'] != 'linux' or aliases.get(arch, arch) != native:
            raise ValueError(f'{name} uses {image["os"]}/{arch}; Docker Engine requires linux/{native}')


def summarize(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['status'] != 'COMPLETE' or manifest.get('errors'):
        raise ValueError('only COMPLETE campaigns without errors can be summarized')
    verify_image_architectures(manifest['docker_context'].split()[-1], manifest['images'])
    expected_groups = manifest['workload']['repetitions'] * 2 * 2
    assert len(manifest['capacity_gates']) == expected_groups, 'missing capacity gates'
    assert len(manifest['runs']) == expected_groups * len(manifest['workload']['scenarios']), 'incomplete matrix'
    assert len({r['id'] for r in manifest['runs']}) == len(manifest['runs']), 'duplicate run ids'
    tuples = {(r['repetition'], r['stack'], r['logs'], r['scenario']) for r in manifest['runs']}
    assert tuples == {(rep, stack, logs, scenario) for rep in range(1, manifest['workload']['repetitions'] + 1)
                      for stack in ('cauce', 'nginx') for logs in ('off', 'on')
                      for scenario in manifest['workload']['scenarios']}, 'duplicate/missing matrix cell'
    if 'access_log_checks' in manifest:
        assert len(manifest['access_log_checks']) == expected_groups // 2, 'missing log checks'
        for check in manifest['access_log_checks']:
            expected = 33 + sum(r['generator']['offered'] + r['generator']['warmup_requests_excluded']
                                for r in manifest['runs'] if r['id'].startswith(check['group'] + '-'))
            count = sum(1 for file in check['files'] for line in (directory / file).read_text().splitlines()
                        if line.strip() and isinstance(json.loads(line), dict))
            assert count == check['valid_rows'] == check['expected'] == expected, check['group']
    for gate in manifest['capacity_gates']:
        proof = gate['evidence']
        assert proof['backend_during_hold']['active'] == 32 and proof['backend_during_hold']['peak_active'] == 32
        assert proof['backend_during_hold']['connections'] <= 32 and proof['backend_after_release']['active'] == 0
        assert proof['statuses'].get('200') == 32 and proof['statuses'].get('503') == 1 and not proof['errors']
    rows = []
    for run in manifest['runs']:
        ident = run['id']
        raw = [json.loads(line) for line in (directory / 'raw' / f'{ident}.jsonl').read_text().splitlines()]
        gen = run['generator']
        assert len(raw) == gen['offered'], (ident, 'lost arrivals')
        assert sorted(r['n'] for r in raw) == list(range(len(raw))), (ident, 'duplicate/missing arrival number')
        count = Counter(r['outcome'] for r in raw)
        assert count['success'] == gen['successful_responses'], ident
        assert count['dropped'] == gen['drops'] and count['rejected'] == gen['rejections'], ident
        assert count['timeout'] == gen['timeouts'], ident
        assert count['http_error'] == gen['http_errors'], ident
        assert count['error'] + count['validation_error'] == gen['errors'], ident
        assert not (gen['errors'] or gen['timeouts'] or gen['drops'] or gen['rejections']), ident
        if run['scenario'] != 'saturation':
            assert gen['successful_responses'] == gen['offered'] and not gen['http_errors'], ident
        else:
            assert gen['http_errors'] > 0 and all(r['status'] == 503 for r in raw if r['outcome'] == 'http_error'), ident
        latencies = sorted(r['scheduled_latency_ms'] for r in raw if r['outcome'] == 'success')
        quantiles = gen['percentiles_ms']['scheduled_latency']['success']
        for q in (50, 95, 99):
            expected = latencies[math.ceil(q / 100 * len(latencies)) - 1] if latencies else None
            assert expected == quantiles[f'p{q}'], (ident, 'raw percentile differs from summary')
        respath = directory / run['resource_file']
        if not respath.exists():
            respath = directory / 'raw' / run['resource_file']
        resources = json.loads(respath.read_text())
        proc = resources['proc_samples']
        assert len(proc) >= 2 and all(p['rss_bytes'] > 0 for p in proc), (ident, 'missing process samples')
        for p in proc:
            names = [x['name'] for x in p['processes']]
            assert names.count('nginx') >= 2 if run['stack'] == 'nginx' else 'cauce' in names, (ident, names)
        backend = run['backend']
        assert backend['active'] == 0 and backend['peak_active'] <= 32 and backend['peak_connections'] <= 32, ident
        if run['scenario'] == 'tls-cold':
            assert backend['tls_full_handshakes'] == gen['successful_responses'] == backend['new_connections'], ident
            assert backend['tls_resumed_handshakes'] == 0 and backend['reused_requests'] == 0, ident
        if run['scenario'] == 'ws':
            assert all(r.get('ws_echo_ok') for r in raw if r['outcome'] == 'success'), ident
        if run['scenario'] == 'sse':
            assert all(r['sse_events'] == 5 for r in raw if r['outcome'] == 'success'), ident
        if run['scenario'] == 'slow-reader':
            assert all(r['bytes'] == 1048576 for r in raw if r['outcome'] == 'success'), ident
        for state in run['container_states'].values():
            assert not state['oom_killed'] and state['restart_count'] == 0, ident
        row = {k:run[k] for k in ('id','stack','logs','scenario','repetition')}
        row.update(offered=gen['offered'], offered_rate=gen['offered_rate_per_second'],
                   goodput_rps=gen['total_goodput_per_second'],
                   offer_window_goodput_rps=gen['offer_window_goodput_per_second'],
                   successes_per_offer_duration=gen['offered_goodput_per_second'],
                   success=gen['successful_responses'],
                   http_errors=gen['http_errors'], errors=gen['errors'], timeouts=gen['timeouts'],
                   drops=gen['drops'], queue_rejections=gen['rejections'],
                   p50_ms=quantiles['p50'], p95_ms=quantiles['p95'], p99_ms=quantiles['p99'],
                   p999_ms=latencies[math.ceil(.999 * len(latencies)) - 1] if latencies else None,
                   scheduler_p99_ms=gen['percentiles_ms']['scheduler_lag']['all']['p99'],
                   generator_comparison_limited=resources['generator_comparison_limited'],
                   rss_peak_mib=max(x['rss_bytes'] for x in proc) / 1048576,
                   cgroup_peak_mib=resources['cgroup_memory_peak_bytes']['proxy'] / 1048576,
                   upstream_new=backend['new_connections'], upstream_reused=backend['reused_requests'],
                   frontend_new=gen['connection_new'], frontend_reused=gen['connection_reused'])
        for name in ('proxy','backend','client'):
            row[f'{name}_cpu_pct'] = cpu_percent(resources['baseline'][name], resources['final'][name])
            row[f'{name}_cpu_interval_s'] = timestamp(resources['final'][name]['timestamp']) - timestamp(resources['baseline'][name]['timestamp'])
            b = resources['baseline'][name]['throttling']; a = resources['final'][name]['throttling']
            row[f'{name}_throttled_ns'] = a.get('throttled_time', 0) - b.get('throttled_time', 0)
        cpu_ns = resources['final']['proxy']['cpu_total_ns'] - resources['baseline']['proxy']['cpu_total_ns']
        row['proxy_cpu_us_per_success'] = cpu_ns / (1000 * row['success']) if row['success'] else None
        rows.append(row)
    if not rows:
        raise ValueError('no measured runs')
    with (directory / 'comparison.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    groups = defaultdict(list)
    for row in rows:
        groups[(row['scenario'], row['logs'], row['stack'])].append(row)
    metrics = ('goodput_rps','offer_window_goodput_rps','successes_per_offer_duration',
               'p50_ms','p95_ms','p99_ms','p999_ms','proxy_cpu_pct','proxy_cpu_us_per_success',
               'rss_peak_mib','cgroup_peak_mib','client_cpu_pct','backend_cpu_pct','scheduler_p99_ms')
    aggregate = []
    for (scenario, logs, stack), values in sorted(groups.items()):
        item = dict(scenario=scenario, logs=logs, stack=stack, repetitions=len(values), offered_rate=values[0]['offered_rate'])
        item['generator_limited_runs'] = [v['id'] for v in values if v['generator_comparison_limited']]
        for metric in metrics:
            numbers = [v[metric] for v in values if v[metric] is not None]
            item[metric] = dict(median=statistics.median(numbers), min=min(numbers), max=max(numbers)) if numbers else None
        for metric in ('offered','success','http_errors','errors','timeouts','drops','queue_rejections'):
            item[metric] = sum(v[metric] for v in values)
        aggregate.append(item)
    result = dict(schema='cauce.increment7.comparison.v1', campaign=manifest['run_id'], runs=len(rows),
                  percentile_aggregation='median of per-run nearest-rank percentiles; not a pooled percentile', groups=aggregate)
    (directory / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    lines = ['# Resultados locales', '', 'Mediana entre repeticiones. CPU 100% = un núcleo; RSS suma procesos del proxy.',
             'RSS puede contar páginas compartidas varias veces; memoria cgroup cuenta el contenedor e incluye caché.',
             'CPU usa timestamps del Engine alrededor de carga + drenaje, con margen de polling; intervalos por corrida en el CSV.',
             'Throughput = respuestas útiles / tiempo total incluido drenaje. Latencia desde llegada programada, solo éxito.', '',
             '| Escenario | Logs | Proxy | r/s ofrecidos | r/s útiles | p50 ms | p95 ms | p99 ms | CPU % | RSS máx. observado MiB | Cgroup máx. observado MiB | HTTP errores | Otros errores/drops |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for g in aggregate:
        def med(key):
            v = g[key]
            return f"{v['median']:.2f}" if v else 'n/a'
        nums = [med(x) for x in ('goodput_rps','p50_ms','p95_ms','p99_ms','proxy_cpu_pct','rss_peak_mib','cgroup_peak_mib')]
        other = sum(g[x] for x in ('errors','timeouts','drops','queue_rejections'))
        lines.append('| ' + ' | '.join([g['scenario'], g['logs'], g['stack'], str(g['offered_rate']), *nums, str(g['http_errors']), str(other)]) + ' |')
    lines += ['', 'Los rangos mínimo/máximo por repetición están en `comparison.json`; todas las corridas en `comparison.csv`.',
              'Las corridas con jitter p99 del generador >50 ms se marcan como `generator_comparison_limited`; sus latencias incluyen esa interferencia y no permiten atribuir el retraso exclusivamente al proxy.',
              'También se conserva throughput completado dentro de la ventana ofrecida (`offer_window_goodput_rps`) y éxitos totales / duración ofrecida (`successes_per_offer_duration`, incluye éxitos del drenaje).',
              'Los errores HTTP se contabilizan por separado; los 503 de saturación no se consideran respuestas útiles.', '']
    (directory / 'COMPARISON.md').write_text('\n'.join(lines))
    print(json.dumps({'audited_runs':len(rows), 'offered':sum(r['offered'] for r in rows), 'summary':str(directory / 'COMPARISON.md')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    summarize(parser.parse_args().directory.resolve())
