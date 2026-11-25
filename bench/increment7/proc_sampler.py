import json, os, pathlib, time

root=pathlib.Path('/results')
marker=root/'.sample'
path=root/'proc-samples.jsonl'
clock_ticks=os.sysconf('SC_CLK_TCK')
page_size=os.sysconf('SC_PAGE_SIZE')
def sample():
    rows=[]
    for p in pathlib.Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            status=(p/'status').read_text()
            values={}
            for line in status.splitlines():
                if line.startswith(('Name:','VmRSS:','Threads:')):
                    k,v=line.split(':',1); values[k]=v.strip()
            cmd=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace').strip()
            comm=values.get('Name','')
            if comm=='cauce' and '/usr/local/bin/cauce' not in cmd: continue
            if comm=='nginx' and not ('nginx: master process' in cmd or 'nginx: worker process' in cmd): continue
            if comm not in ('cauce','nginx'): continue
            try: fd_count=len(list((p/'fd').iterdir()))
            except PermissionError: fd_count=None
            statline=(p/'stat').read_text()
            fields=statline.rsplit(')',1)[-1].split()
            rows.append({'pid':int(p.name),'name':comm,'cmdline':cmd,'rss_bytes':int(values.get('VmRSS','0 kB').split()[0])*1024,
                         'threads':int(values.get('Threads','0')),'fds':fd_count,'cpu_ticks':int(fields[11])+int(fields[12])})
        except (FileNotFoundError,ProcessLookupError,ValueError): pass
    return rows
with path.open('a', buffering=1) as out:
    while True:
        if marker.exists():
            try: run=marker.read_text().strip()
            except FileNotFoundError: run=''
            rows=sample()
            out.write(json.dumps({'t_mono':time.monotonic(),'run':run,'processes':rows,
                                  'rss_bytes':sum(x['rss_bytes'] for x in rows),'threads':sum(x['threads'] for x in rows),
                                  'fds':sum(x['fds'] for x in rows)})+'\n')
        time.sleep(.2)
