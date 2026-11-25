#!/usr/bin/env python3
"""Independently audit raw offers and export per-run and median/range summaries."""
import argparse,csv,json,math,pathlib,statistics

def percentile(values,p):
    ordered=sorted(values)
    return ordered[max(0,math.ceil(len(ordered)*p)-1)] if ordered else None

def audit_run(root,run,saturation=False):
    initial=run.get('initial_backend')
    if not isinstance(initial,dict):raise ValueError('missing clean initial backend snapshot')
    for field in ('active','requests','completed','connections','peak_connections'):
        if field not in initial:raise ValueError(f'initial backend snapshot missing {field}')
        if initial[field]!=0:raise ValueError(f'initial backend is contaminated: {field}={initial[field]}')
    rows=[json.loads(x) for x in (root/run['raw']).read_text().splitlines() if x.strip()]
    s=run['generator'];n=s['offered']
    if len(rows)!=n or sorted(x['seq'] for x in rows)!=list(range(n)):raise ValueError('lost, duplicate or extra offered arrivals')
    for row in rows:
        seq=row.get('seq')
        scheduled=row.get('scheduled_at_ms');ended=row.get('ended_at_ms');arrival=row.get('arrival_latency_ms')
        if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (scheduled,ended,arrival)):
            raise ValueError(f'nonfinite/missing arrival timing for seq {seq}')
        if arrival<0 or ended<scheduled:raise ValueError(f'negative arrival duration for seq {seq}')
        if not math.isclose(arrival,ended-scheduled,rel_tol=0,abs_tol=1e-4):
            raise ValueError(f'arrival latency does not match timestamps for seq {seq}')
        started=row.get('started_at_ms');service=row.get('service_latency_ms')
        if started is None:
            if service is not None:raise ValueError(f'service latency without start for seq {seq}')
            continue
        if not isinstance(started,(int,float)) or not math.isfinite(started) or started<scheduled:
            raise ValueError(f'invalid start timestamp for seq {seq}')
        if not isinstance(service,(int,float)) or not math.isfinite(service) or service<0:
            raise ValueError(f'invalid service duration for seq {seq}')
        if ended<started or not math.isclose(service,ended-started,rel_tol=0,abs_tol=1e-4):
            raise ValueError(f'service latency does not match timestamps for seq {seq}')
    outcomes={k:sum(r['outcome']==k for r in rows) for k in {r['outcome'] for r in rows}}
    good=[r for r in rows if r['outcome']=='ok'];bad=[r for r in rows if r['outcome']!='ok']
    if len(good)!=s['ok'] or not good:raise ValueError('successful count mismatch or zero success')
    if s['errors'] or s['rejected'] or s['dropped']:raise ValueError('generator/transport errors, drops or queue rejection')
    if saturation:
        if not bad or any(r['status']!=503 or r['outcome']!='overload_expected' for r in bad):raise ValueError('saturation must produce only expected 503 failures')
        if run['backend']['peak_active']!=32:raise ValueError('saturation did not reach active request limit')
    elif bad:raise ValueError('unexpected non-success in nonsaturation load')
    if any(v!=16 for v in s['remote_max_concurrent_streams']):raise ValueError('server SETTINGS limit was not 16')
    if any(v>16 for v in [x['peak_inflight'] for x in s['sessions']]):raise ValueError('generator per-connection stream budget exceeded')
    if s['protocol']=='TLS+HTTP/2' and any(v!='h2' for v in s['negotiated_alpn']):raise ValueError('TLS did not negotiate HTTP/2')
    if any(r.get('stream_id') is None or r['stream_id']%2!=1 for r in good):raise ValueError('missing or invalid HTTP/2 stream identity')
    if len({(r['session_id'],r['stream_id']) for r in good})!=len(good):raise ValueError('reused HTTP/2 stream identity')
    for row in good:
        if not math.isfinite(row['arrival_latency_ms']) or row['arrival_latency_ms']<0:raise ValueError('invalid latency')
        if s['mode']=='grpc' and (row['grpc_status']!='0' or row['grpc_message_count']!=(3 if s['method'] in ('ServerStream','Bidi') else 1)):raise ValueError('invalid gRPC trailer/message count')
        if s['mode']=='http2' and row['bytes']!=1024:raise ValueError('HTTP payload length mismatch')
    if any(k not in ('ok','overload_expected') or (k=='overload_expected' and not saturation) for k in s['warmup']['outcomes']):raise ValueError('warmup failed')
    if run['backend']['requests']!=len(good):raise ValueError('independent backend request count does not match successful client offers')
    p={q:percentile([r['arrival_latency_ms'] for r in good],f) for q,f in [('p50',.5),('p95',.95),('p99',.99)]}
    lag=percentile([r['scheduler_lag_ms'] for r in rows],.99)
    # Recompute from raw data rather than trusting the client's summary percentiles.
    throughput=len(good)/s['measurement_elapsed_s']
    res=json.loads((root/run['resources']).read_text());cpu=res['cpu']['proxy']['cpu_percent_of_one_core']
    if cpu is None or cpu<0:raise ValueError('missing/negative CPU usage')
    if len(res['samples'])<2:raise ValueError('insufficient resource samples')
    flags=[]
    if lag>50:flags.append('scheduler_lag_p99_over_50ms')
    for component in ('proxy','client','backend'):
        if res['cpu'][component]['throttling_delta'].get('throttled_time',0)>0:flags.append(component+'_cpu_throttled')
    procs=[x for x in res['processes'] if x['run']==run['id']]
    rss=max(sum(p['rss_bytes'] for p in x['processes']) for x in procs)
    return {'raw_offers':len(rows),'outcomes':outcomes,'latency_ms':p,'throughput_ok_per_s_including_drain':throughput,
      'scheduler_lag_p99_ms':lag,'proxy_cpu_percent':cpu,'proxy_cgroup_peak_bytes':res['cgroup_peak_bytes']['proxy'],
      'proxy_rss_sum_peak_bytes':rss,'upstream_peak_connections':run['backend'].get('peak_connections'),
      'comparison_flags':flags}

def summarize(root):
    manifest=json.loads((root/'manifest.json').read_text())
    if manifest['status']!='COMPLETE':raise ValueError('refusing incomplete campaign')
    expected=len(manifest['workload']['scenarios'])*manifest['workload']['repetitions']*2
    if len(manifest['runs'])!=expected:raise ValueError('matrix incomplete')
    keys={(r['scenario'],r['stack'],r['repetition']) for r in manifest['runs']}
    if len(keys)!=expected:raise ValueError('duplicate matrix rows')
    allrows=[];groups=[]
    for r in manifest['runs']:
        a=audit_run(root,r,r['scenario']=='h2-saturation')
        allrows.append({'id':r['id'],'scenario':r['scenario'],'stack':r['stack'],'repetition':r['repetition'],
          'offered':a['raw_offers'],'ok':a['outcomes']['ok'],'expected_503':a['outcomes'].get('overload_expected',0),
          'p50_ms':a['latency_ms']['p50'],'p95_ms':a['latency_ms']['p95'],'p99_ms':a['latency_ms']['p99'],
          'goodput_rps':a['throughput_ok_per_s_including_drain'],'cpu_percent':a['proxy_cpu_percent'],
          'cgroup_peak_mib':a['proxy_cgroup_peak_bytes']/2**20,'rss_sum_peak_mib':a['proxy_rss_sum_peak_bytes']/2**20,
          'upstream_peak_connections':a['upstream_peak_connections'],'scheduler_lag_p99_ms':a['scheduler_lag_p99_ms'],
          'flags':';'.join(a['comparison_flags'])})
    for scenario in manifest['workload']['scenarios']:
        for stack in ('cauce','nginx'):
            rs=[r for r in allrows if r['scenario']==scenario and r['stack']==stack]
            metrics={k:{'median':statistics.median(r[k] for r in rs),'min':min(r[k] for r in rs),'max':max(r[k] for r in rs)} for k in ('p50_ms','p95_ms','p99_ms','goodput_rps','cpu_percent','cgroup_peak_mib','rss_sum_peak_mib','upstream_peak_connections')}
            groups.append({'scenario':scenario,'stack':stack,'runs':len(rs),'metrics':metrics,'flags':[r['flags'] for r in rs if r['flags']]})
    with (root/'runs.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=allrows[0].keys());writer.writeheader();writer.writerows(allrows)
    doc={'campaign':root.name,'runs':len(allrows),'offers':sum(r['offered'] for r in allrows),'successes':sum(r['ok'] for r in allrows),'expected_503':sum(r['expected_503'] for r in allrows),'groups':groups,'flagged_runs':[r['id'] for r in allrows if r['flags']]}
    (root/'summary.json').write_text(json.dumps(doc,indent=2)+'\n');print(json.dumps({k:v for k,v in doc.items() if k!='groups'}));return doc
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('results',type=pathlib.Path);a=p.parse_args();summarize(a.results)
