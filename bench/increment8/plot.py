#!/usr/bin/env python3
"""Render the audited increment8 summary as paired median/range plots."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


SCENARIOS = (
    ('h2-clear', 'HTTP/2\nclaro'),
    ('h2-tls', 'HTTP/2\nTLS'),
    ('h2-delayed', 'HTTP/2\nbackend lento'),
    ('h2-saturation', 'HTTP/2\nsaturación'),
    ('grpc-unary-clear', 'gRPC unary\nclaro'),
    ('grpc-unary-tls', 'gRPC unary\nTLS'),
    ('grpc-client-stream', 'gRPC cliente\nstream'),
    ('grpc-server-stream', 'gRPC servidor\nstream'),
    ('grpc-bidi', 'gRPC\nbidireccional'),
)
STACKS = ('cauce', 'nginx')
COLORS = {'cauce': '#1769AA', 'nginx': '#E87924'}
NAMES = {'cauce': 'Cauce 0.7', 'nginx': 'NGINX 1.30.6'}
PANELS = (
    ('p95_ms', 'Latencia p95 de éxitos', 'ms · desde llegada programada', True),
    ('goodput_rps', 'Throughput útil', 'respuestas/RPC completados por segundo', False),
    ('cpu_percent', 'CPU del proxy', '% de un núcleo (100 % = 1 núcleo)', False),
    ('cgroup_peak_mib', 'Memoria del contenedor', 'MiB · máximo de uso cgroup muestreado', False),
)


def read_groups(result_dir: Path) -> dict[tuple[str, str], dict]:
    summary_path = result_dir / 'summary.json'
    doc = json.loads(summary_path.read_text(encoding='utf-8'))
    groups = {}
    for group in doc.get('groups', []):
        key = (group.get('scenario'), group.get('stack'))
        if key in groups:
            raise ValueError(f'duplicate summary group: {key}')
        groups[key] = group
    expected = {(scenario, stack) for scenario, _ in SCENARIOS for stack in STACKS}
    missing = sorted(expected - groups.keys())
    extra = sorted(groups.keys() - expected)
    if missing or extra:
        raise ValueError(f'incomplete increment8 matrix; missing={missing}, extra={extra}')
    for key, group in groups.items():
        if group.get('runs') != 3:
            raise ValueError(f'{key}: expected 3 repetitions, got {group.get("runs")}')
    return groups


def make_plot(groups: dict, output: Path) -> None:
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9, 'axes.titleweight': 'bold'})
    fig, axes = plt.subplots(2, 2, figsize=(16, 10), layout='constrained')
    x = np.arange(len(SCENARIOS), dtype=float)
    bar_width = 0.34
    offsets = {'cauce': -bar_width / 2, 'nginx': bar_width / 2}

    for ax, (metric, title, ylabel, logarithmic) in zip(axes.flat, PANELS):
        for stack in STACKS:
            stats = [groups[(scenario, stack)]['metrics'][metric] for scenario, _ in SCENARIOS]
            med = np.asarray([float(v['median']) for v in stats])
            low = np.asarray([float(v['min']) for v in stats])
            high = np.asarray([float(v['max']) for v in stats])
            if not np.all(np.isfinite(np.r_[med, low, high])) or np.any(low > med) or np.any(med > high):
                raise ValueError(f'invalid median/range values for {metric}/{stack}')
            if logarithmic and np.any(low <= 0):
                raise ValueError(f'{metric}/{stack}: log-scale p95 range must be positive')
            ax.bar(x + offsets[stack], med, width=bar_width, color=COLORS[stack], label=NAMES[stack],
                   yerr=np.vstack((med - low, high - med)), capsize=2.5,
                   error_kw={'elinewidth': 1.0, 'ecolor': '#30343B'}, linewidth=0, zorder=3)
        ax.set_title(title, loc='left', fontsize=12, pad=9)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x, [label for _, label in SCENARIOS], fontsize=8)
        ax.set_axisbelow(True)
        ax.grid(axis='y', color='#B8C1CC', alpha=.32, linewidth=.7)
        ax.spines[['top', 'right']].set_visible(False)
        if logarithmic:
            ax.set_yscale('log')
        else:
            ax.set_ylim(bottom=0)

    axes[0, 0].legend(frameon=False, loc='upper left', ncols=2, fontsize=9)
    fig.suptitle('Cauce y NGINX · Benchmarks HTTP/2 y gRPC en Docker', fontsize=16, fontweight='bold')
    fig.text(.5, -.012,
             'Mediana de 3 repeticiones; barras = mínimo y máximo. Latencia p95 de solicitudes exitosas desde la llegada programada. '
             'CPU: 100 % equivale a un núcleo. Memoria: máximo muestreado del cgroup, incluida caché.',
             ha='center', va='top', fontsize=9, color='#384454', wrap=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches='tight', facecolor='white')
    fig.savefig(output.with_suffix('.svg'), bbox_inches='tight', facecolor='white')
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result_dir', type=Path, help='increment8 campaign directory containing summary.json')
    parser.add_argument('--output', type=Path, required=True, help='PNG destination; an SVG with the same stem is also saved')
    args = parser.parse_args()
    make_plot(read_groups(args.result_dir), args.output)
    print(args.output)
    print(args.output.with_suffix('.svg'))


if __name__ == '__main__':
    main()
