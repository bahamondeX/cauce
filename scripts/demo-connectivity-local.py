#!/usr/bin/env python3
"""Small presentation wrapper around the existing isolated real-adapter test."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = ROOT.parents[2] / 'task/connectivity-ingest-worktree'
LABELS = {
    'delivery': 'Entrega HTTPS y spool vacío',
    'lost_ack_replay': 'ACK perdido: reintento sin duplicación',
    'expire_token_once': 'Token expirado: rechazo y recuperación',
    'revoke_token_once': 'Token revocado: rechazo y recuperación',
    'disable_namespace_once': 'Namespace deshabilitado: rechazo y recuperación',
    'unauthorized_namespace_once': 'Namespace no autorizado: rechazo y recuperación',
    'revoke_token_before_commit_once': 'Revocación antes del commit: sin recibo indebido',
    'disable_namespace_before_commit_once': 'Deshabilitación antes del commit: sin recibo indebido',
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument('--image', default='cauce:0.9.1-health-frontier-arm64')
    args = parser.parse_args()
    if not (args.reference / 'deploy/proxy_ingest_api.py').is_file():
        parser.error('No encuentro el checkout local. Usa --reference /ruta/connectivity-ingest-worktree')
    checks = [ ['docker', 'info'], ['docker', 'image', 'inspect', args.image],
               ['docker', 'image', 'inspect', 'cauce-connectivity-real-lab:local'] ]
    for command in checks:
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is None or result.returncode:
            print('PRECHECK FALLÓ: Docker o las imágenes locales no están disponibles. No se inició ni descargó nada.', file=sys.stderr)
            return 1
    report_dir = ROOT / 'work' / ('demo-connectivity-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    print('CAUCE + CONNECTIVITY | Demo local reproducible', flush=True)
    print('Eventos sintéticos → collector Rust → HTTPS → API/autorización/sink reales → Moto S3/DynamoDB', flush=True)
    print('Sin AWS real, sin puertos publicados y sin modificar el checkout de Connectivity.', flush=True)
    print('Ejecutando ocho escenarios aislados...', flush=True)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/test-connectivity-real-docker.py'),
                             '--reference', str(args.reference.resolve()), '--image', args.image,
                             '--report-dir', str(report_dir)], capture_output=True, text=True)
    if result.returncode:
        print('DEMO NO APROBADA. Consulta el reporte parcial; no se afirma éxito.', file=sys.stderr)
        print(str(report_dir), file=sys.stderr)
        print(result.stderr[-3000:], file=sys.stderr)
        return result.returncode
    report = json.loads((report_dir / 'report.json').read_text())
    names = {case['name'] for case in report['cases']}
    assert report['result'] == 'PASS' and names == set(LABELS)
    assert report['reference_unchanged'] and report['preexisting_running_containers_preserved']
    assert not report['owned_container_remaining'] and report['temporary_directory_removed']
    storage = report['server']['storage']
    for case in report['cases']:
        print('  PASS  ' + LABELS[case['name']])
    summary = {'result': 'PASS', 'cases': len(names), 'unique_events': len(storage['event_ids']),
               'receipts': storage['receipt_count'], 'outbox': storage['outbox_count'],
               'collector_image_id': report['collector_image_id'],
               'reference_unchanged': True, 'existing_services_preserved': True,
               'lab_cleaned_up': True, 'aws': 'Moto simulation, not AWS deployment'}
    (report_dir / 'demo-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f"\nRESULTADO: {len(names)}/8 PASS | {summary['unique_events']} eventos únicos | {summary['receipts']} recibos | {summary['outbox']} trabajos outbox")
    print('Fuentes intactas, servicios previos preservados y laboratorio eliminado.')
    print('No demuestra login, dashboard, análisis terminado ni durabilidad de AWS real.')
    print('Evidencia: ' + str(report_dir))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
