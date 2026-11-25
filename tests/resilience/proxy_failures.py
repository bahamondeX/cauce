#!/usr/bin/env python3
"""Bounded real-Docker backend failure and live-reload integration checks."""
import argparse, hashlib, json, re, shutil, subprocess, tempfile, time, uuid
from pathlib import Path

NODE = "node:22.14.0-alpine3.21@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944"
TOKEN = "increment9-proxy-test-token-0123456789abcdef"
HERE = Path(__file__).resolve().parent

class Docker:
    def __init__(self): self.owned=[]; self.network=None; self.commands=[]
    def run(self,*args,check=True,timeout=30):
        cmd=["docker",*map(str,args)]; self.commands.append(cmd)
        p=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
        if check and p.returncode: raise RuntimeError(f"docker {cmd}: {p.stderr[-1200:]}")
        return p
    def backend(self,net,name,label,work):
        cid=self.run("run","-d","--name",name,"--network",net,"--network-alias",name,
          "--label","cauce.increment9.proxy=true","--memory=128m","--cpus=.35","--pids-limit=32",
          "--log-opt","max-size=1m","--log-opt","max-file=2","-e",f"LABEL={label}",
          "-v",f"{HERE}/proxy_backend.mjs:/proxy_backend.mjs:ro","-v",f"{work}:/control:ro",NODE,"node","/proxy_backend.mjs").stdout.strip()
        self.owned.append(cid); return cid
    def node(self,net,js,timeout=8,pidns=None):
        name="i9-probe-"+uuid.uuid4().hex[:10]
        self.owned.append(name)
        args=["run","--rm","--name",name,"--network",net,"--memory=96m","--cpus=.25","--pids-limit=24",
              "--label","cauce.increment9.proxy=true","--log-opt","max-size=1m","--log-opt","max-file=2"]
        if pidns: args += ["--pid",f"container:{pidns}","--user","10001:10001"]
        return self.run(*args,NODE,"node","-e",js,timeout=timeout).stdout.strip()

def write_config(path, main_ids, main_addresses, post=True, invalid=False, post_address="broken:8000"):
    if invalid: path.write_text('name = [ invalid\n'); return
    s='''name="cauce"
listen="0.0.0.0:8080"
admin="127.0.0.1:9090"
[http2]
enabled=true
max_concurrent_streams=16
[limits]
clients=64
requests=32
connections=16
idle_connections=8
connecting=4
acquire_ms=250
connect_ms=500
headers_ms=10000
stream_idle_ms=3000
request_ms=15000
client_headers_ms=1500
idle_connection_ms=3000
max_connection_age_ms=10000
dns_cache_ms=1000
tcp_keepalive_secs=10
max_header_bytes=16384
max_headers=64
max_body_bytes=1048576
shutdown_ms=1000
[logs]
enabled=true
path="/tmp/cauce-access.jsonl"
queue=128
rotate_bytes=1048576
retained_files=2
'''
    pairs=list(zip(main_ids,main_addresses))
    if post: pairs.append(("postbroken",post_address))
    for ident,address in pairs:
        s+=f'''\n[[upstreams]]
id="{ident}"
address="{address}"
host="backend.test:8000"
isolation="{ident}"
connections=4
idle_connections=2
failure_backoff_ms=5000
'''
    if post:
        s+='''\n[[routes]]
id="post"
host="proxy:8080"
path_prefix="/post/"
balance="round_robin"
upstreams=["postbroken"]
'''
    s+=f'''\n[[routes]]
id="main"
host="proxy:8080"
path_prefix="/"
balance="round_robin"
upstreams={json.dumps(main_ids)}
'''
    path.write_text(s)

def metrics(d,proxy):
    js=f"fetch('http://127.0.0.1:9090/metrics',{{headers:{{authorization:'Bearer {TOKEN}'}}}}).then(r=>r.text()).then(console.log)"
    raw=d.node("container:"+proxy,js)
    out={}
    for line in raw.splitlines():
        m=re.match(r'cauce_(\w+)\s+([\d.eE+-]+)$',line)
        if m:
            try: out[m[1]]=int(float(m[2]))
            except ValueError: pass
    return out

def request(d,net,path="/",method="GET",body=None):
    js=f"""(async()=>{{try{{const r=await fetch('http://proxy:8080'+{json.dumps(path)},{{method:{json.dumps(method)},body:{json.dumps(body) if body is not None else 'undefined'},signal:AbortSignal.timeout(16000)}});let t='';try{{t=await r.text()}}catch(e){{t='BODY_ERROR:'+e}};console.log(JSON.stringify({{status:r.status,body:t,backend:r.headers.get('x-backend')}}))}}catch(e){{console.log(JSON.stringify({{error:String(e)}}))}}}})()"""
    return json.loads(d.node(net,js,timeout=20))

def backend_stats(d,net,name):
    js=f"fetch('http://{name}:8000/stats').then(r=>r.json()).then(x=>console.log(JSON.stringify(x)))"
    return json.loads(d.node(net,js))

def proc(d,proxy):
    js="const fs=require('fs');const s=fs.readFileSync('/proc/1/status','utf8');const n=k=>{const l=s.split('\\n').find(x=>x.startsWith(k+':'));return l?parseInt(l.split(/\\s+/)[1],10):null};console.log(JSON.stringify({fds:fs.readdirSync('/proc/1/fd').length,rss_kib:n('VmRSS'),threads:n('Threads')}))"
    return json.loads(d.node("none",js,pidns=proxy))

def wait_zero(d,proxy):
    end=time.monotonic()+7; last={}
    while time.monotonic()<end:
        last=metrics(d,proxy)
        if all(last.get(k)==0 for k in ("clients_active","requests_active","upstream_connections_in_use")): return last
        time.sleep(.1)
    raise AssertionError(f"gauges did not recover: {last}")

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--image",default="cauce:0.9-final-arm64");ap.add_argument("--report",required=True);ap.add_argument("--work-dir");a=ap.parse_args()
    report_path=Path(a.report).resolve();report_path.parent.mkdir(parents=True,exist_ok=True)
    work=Path(a.work_dir).resolve() if a.work_dir else Path(tempfile.mkdtemp(prefix=".proxy-run-",dir=HERE));work.mkdir(parents=True,exist_ok=True)
    d=Docker(); start=time.time(); report={"case":"proxy_failures_reload","image":a.image,"node_image":NODE,"evidence":{},"limits":{"proxy_memory":"256m","proxy_cpus":0.75,"proxy_pids":64,"backend_memory":"128m","max_duration_seconds":120}}
    try:
        runtime_image=d.run("image","inspect","--format","{{.Id}}|{{.Os}}|{{.Architecture}}",a.image).stdout.strip()
        node_image=d.run("image","inspect","--format","{{.Id}}|{{.Os}}|{{.Architecture}}",NODE).stdout.strip()
        report["images"]={"runtime_tag":a.image,"runtime_id_os_arch":runtime_image,"node_pinned_ref":NODE,"node_id_os_arch":node_image}
        report["evidence"]["source_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__),HERE/"proxy_backend.mjs")}
        suffix=uuid.uuid4().hex[:8]
        net="cauce-i9-proxy-"+suffix;d.run("network","create","--internal",net);d.network=net
        (work/"release-slow").unlink(missing_ok=True)
        healthy_name="i9-healthy-"+suffix;broken_name="i9-broken-"+suffix;new_name="i9-new-"+suffix
        healthy=d.backend(net,healthy_name,"healthy",work);broken=d.backend(net,broken_name,"broken",work)
        work.chmod(0o755)
        cfg=work/"cauce.toml";write_config(cfg,["healthy","broken"],[f"{healthy_name}:8000",f"{broken_name}:8000"],post_address=f"{broken_name}:8000");cfg.chmod(0o644);time.sleep(.25)
        cfg.chmod(0o644)
        proxy=d.run("run","-d","--name","i9-proxy-"+suffix,"--network",net,"--network-alias","proxy","--label","cauce.increment9.proxy=true","--memory=256m","--cpus=.75","--pids-limit=64","--ulimit","nofile=1024:1024","--log-opt","max-size=1m","--log-opt","max-file=2","-e",f"CAUCE_ADMIN_TOKEN={TOKEN}","-v",f"{work}:/config:ro",a.image,"/config/cauce.toml").stdout.strip();d.owned.append(proxy)
        host=d.run("inspect","--format","{{json .HostConfig}}",proxy)
        report["limits"]["proxy_effective"]={k:json.loads(host.stdout).get(k) for k in ("Memory","NanoCpus","PidsLimit","Ulimits")}
        report["evidence"].update({"network":net,"container_ids":{"proxy":proxy,"healthy":healthy,"broken":broken}})
        end=time.monotonic()+15; readiness=[]
        while time.monotonic()<end:
            try:
                first=request(d,net)
                readiness.append(first)
                if first.get("status")==200: break
            except Exception as e: readiness.append(repr(e))
            time.sleep(.2)
        else: raise AssertionError(f"Cauce readiness timeout: {readiness[-2:]}")
        baseline=proc(d,proxy)
        post=request(d,net,"/post/drop","POST","one-shot-side-effect-payload")
        observed=backend_stats(d,net,broken_name)
        assert observed.get("paths",{}).get("/post/drop")==1, (post,observed)
        assert post.get("status") in (502,504) or "error" in post or "BODY_ERROR" in post.get("body",""),post
        assert len(json.dumps(post))<1200,post
        d.run("kill","--signal","KILL",broken);time.sleep(.2)
        failover=[request(d,net) for _ in range(8)]
        good=[x for x in failover if x.get("status")==200]
        bounded=[x for x in failover if x.get("status") in (502,504) or "error" in x]
        assert len(good)>=6 and all(json.loads(x["body"])["backend"]=="healthy" for x in good),failover
        assert all(x.get("status") in (200,502,504) or "error" in x for x in failover),failover
        assert bounded,"killed backend was never observed to fail on the main route"
        recovered=wait_zero(d,proxy);after=proc(d,proxy)
        assert after["fds"]<=baseline["fds"]+8,(baseline,after)
        report["checks"]={"backend_failure_failover":{"passed":True,"responses":len(failover),"raw_responses":failover,"backend":"healthy"},"post_midbody_failure_bounded_no_retry":{"passed":True,"result":post,"backend_counter":observed,"attempts_observed":observed["paths"]["/post/drop"]},"resources_recovered":{"passed":True,"gauges":recovered,"before":baseline,"after":after}}
        d.run("start",broken)
        end=time.monotonic()+5;broken_ready=False
        while time.monotonic()<end:
            try:
                if backend_stats(d,net,broken_name).get("label")=="broken": broken_ready=True;break
            except Exception: pass
            time.sleep(.1)
        assert broken_ready,"restarted backend never became ready"
        restore_js="""const http=require('http');let n=0,seen={};const end=Date.now()+9000;function go(){const r=http.get('http://proxy:8080/',x=>{x.resume();x.on('end',()=>{n++;const b=x.headers['x-backend']||String(x.statusCode);seen[b]=(seen[b]||0)+1;if(b==='broken'){console.log(JSON.stringify({restored:true,n,seen}));return}if(Date.now()>end){console.log(JSON.stringify({restored:false,n,seen}));return}setTimeout(go,50)})});r.on('error',()=>{n++;if(Date.now()>end){console.log(JSON.stringify({restored:false,n,seen}));return}setTimeout(go,50)})}go()"""
        restored=json.loads(d.node(net,restore_js,timeout=12))
        assert restored.get("restored") and restored.get("seen",{}).get("broken",0)>=1,restored
        report["checks"]["backend_recovered_after_backoff"]={"passed":True,"failure_backoff_ms":5000,"probe":restored}
        write_config(cfg,["healthy"],[f"{healthy_name}:8000"],post_address=f"{broken_name}:8000")
        cfg.chmod(0o644);d.run("kill","--signal","HUP",proxy)
        end=time.monotonic()+5;version=0
        while time.monotonic()<end:
            version=metrics(d,proxy).get("config_version",0)
            if version>=2: break
            time.sleep(.1)
        assert version==2,f"could not establish generation A: {version}"
        driver_js="""const http=require('http');let n=0,bad=0,by={},first={},last={};const started=Date.now(),stop=started+8500;function go(){if(Date.now()>stop){console.log(JSON.stringify({n,bad,started,finished:Date.now(),by,first,last}));return}const r=http.get('http://proxy:8080/',x=>{const b=x.headers['x-backend']||String(x.statusCode),now=Date.now();x.resume();x.on('end',()=>{n++;if(n===1)console.log('LOAD_STARTED');by[b]=(by[b]||0)+1;first[b]??=now;last[b]=now;if(x.statusCode!==200)bad++;setTimeout(go,15)})});r.on('error',()=>{bad++;setTimeout(go,25)})}go()"""
        from concurrent.futures import ThreadPoolExecutor
        held_pool=ThreadPoolExecutor(max_workers=2)
        new=d.backend(net,new_name,"new",work)
        end=time.monotonic()+8;new_ready=False
        while time.monotonic()<end:
            try:
                if backend_stats(d,net,new_name).get("label")=="new": new_ready=True;break
            except Exception: pass
            time.sleep(.1)
        assert new_ready,"new backend did not become ready before route switch"
        held=held_pool.submit(request,d,net,"/slow")
        end=time.monotonic()+4;slow_seen=False
        while time.monotonic()<end:
            sample=backend_stats(d,net,healthy_name)
            if sample.get("paths",{}).get("/slow",0)>=1 and sample.get("active",0)>=1: slow_seen=True;break
            time.sleep(.05)
        assert slow_seen,"slow generation-A request did not remain active at backend"
        driver=d.run("run","-d","--name","i9-load-"+suffix,"--network",net,"--label","cauce.increment9.proxy=true","--memory=96m","--cpus=.25","--pids-limit=24",NODE,"node","-e",driver_js).stdout.strip();d.owned.append(driver)
        end=time.monotonic()+5
        while 'LOAD_STARTED' not in d.run("logs",driver).stdout:
            assert time.monotonic()<end, "load did not start before HUP"
            time.sleep(.05)
        assert not held.done(), "held request ended before HUP"
        cfg.chmod(0o644);write_config(cfg,["healthy"],[f"{new_name}:8000"],post_address=f"{broken_name}:8000")
        cfg.chmod(0o644)
        d.run("kill","--signal","HUP",proxy)
        end=time.monotonic()+6;version=0
        while time.monotonic()<end:
            version=metrics(d,proxy).get("config_version",0)
            if version>=3: break
            time.sleep(.1)
        assert version==3,f"reload did not advance version: {version}"
        new_resp=request(d,net)
        assert new_resp.get("status")==200 and json.loads(new_resp["body"])["backend"]=="new",new_resp
        write_config(cfg,[],[],invalid=True);d.run("kill","--signal","HUP",proxy);time.sleep(.3)
        invalid_version=metrics(d,proxy).get("config_version")
        still=request(d,net)
        assert invalid_version==3 and json.loads(still["body"])["backend"]=="new",(invalid_version,still)
        assert not held.done(), "old generation did not remain in flight through both HUPs"
        (work/"release-slow").touch()
        held_resp=held.result(timeout=20)
        assert held_resp.get("status")==200 and json.loads(held_resp["body"])["backend"]=="healthy",held_resp
        end=time.monotonic()+12;load={"n":0,"bad":0}
        while time.monotonic()<end:
            p=d.run("logs",driver,check=False)
            m=re.search(r'\{"n":\d+,"bad":\d+,[^\n]+\}',p.stdout)
            if m: load=json.loads(m.group());break
            time.sleep(.25)
        assert load["n"]>=10 and load["bad"]==0,load
        assert load.get("by",{}).get("healthy",0)>0 and load.get("by",{}).get("new",0)>0,load
        d.run("stop","-t","1",driver,check=False)
        gauges=wait_zero(d,proxy);after_reload=proc(d,proxy)
        assert after_reload["fds"]<=baseline["fds"]+12,(baseline,after_reload)
        access_path=report_path.with_name(report_path.stem+"-access.jsonl")
        cp=d.run("cp",f"{proxy}:/tmp/cauce-access.jsonl",access_path,check=False,timeout=12)
        assert cp.returncode==0 and access_path.exists(),(cp.stderr,cp.stdout)
        access=[json.loads(line) for line in access_path.read_text().splitlines() if line.strip()]
        old_held=[e for e in access if e.get("config_version")==2 and e.get("upstream_id")=="healthy" and e.get("duration_ms",0)>500]
        new_logs=[e for e in access if e.get("config_version")==3 and e.get("upstream_id")=="healthy"]
        assert old_held and new_logs,(len(access),old_held,new_logs)
        report["evidence"]["access_log"]={"records":len(access),"held_generation_event":old_held[-1],"new_generation_sample":new_logs[-1],"raw_file":access_path.name}
        report["checks"]["reload_while_http1_load"]={"passed":True,"generation_a_version":2,"version":version,"inflight_generation_backend":"healthy","new_generation_backend":"new","requests":load,"gauges":gauges,"fds_before":baseline["fds"],"fds_after":after_reload["fds"],"invalid_reload_kept_version":invalid_version,"held_was_active_before_hup":slow_seen}
        h2js="""const h=require('http2'),c=h.connect('http://proxy:8080'),r=c.request({':path':'/'});let b='';r.on('data',x=>b+=x);r.on('end',()=>{console.log(JSON.stringify({body:b}));c.close()});r.end()"""
        h2=json.loads(d.node(net,h2js));assert json.loads(h2["body"])["backend"]=="new",h2
        report["checks"]["http2_new_generation"]={"passed":True,"response":h2}
        operational=d.run("logs",proxy,check=False)
        report["evidence"]["proxy_logs"]=(operational.stdout+operational.stderr)[-3000:]
        report["status"]="passed";report["result"]="PASS"
    except Exception as e:
        report.update({"status":"failed","error":repr(e)})
        if "proxy" in locals():
            lp=d.run("logs",proxy,check=False);report.setdefault("evidence",{})["proxy_logs_on_failure"]=(lp.stdout+lp.stderr)[-5000:]
        raise
    finally:
        if "held_pool" in locals():
            (work/"release-slow").touch()
            held_pool.shutdown(wait=True,cancel_futures=True)
        report["duration_seconds"]=round(time.time()-start,3)
        report["commands"]=[" ".join(map(str,x)) for x in d.commands]
        for cid in reversed(d.owned): d.run("rm","-f",cid,check=False,timeout=15)
        if d.network: d.run("network","rm",d.network,check=False)
        report_path.write_text(json.dumps(report,indent=2)+"\n")
        if a.work_dir: (work/"proxy-failures-raw.json").write_text(json.dumps(report.get("evidence",{}),indent=2)+"\n")
        else: shutil.rmtree(work)
    print(json.dumps({"status":report["status"],"report":str(report_path),"duration_seconds":report["duration_seconds"]}))

if __name__=="__main__": main()
