#!/usr/bin/env python3
"""Bounded HTTP/2 wire stress and deterministic malformed-frame campaign."""
import json, os, random, socket, ssl, struct, threading, time, urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import ResponseReceived, DataReceived, StreamReset, StreamEnded, RemoteSettingsChanged, ConnectionTerminated
from h2.errors import ErrorCodes
from h2.settings import SettingCodes
from hpack import Encoder
from hyperframe.frame import HeadersFrame

MODE = os.getenv("CAUCE_STRESS_MODE", "clear")
HOST = f"proxy-{MODE}"
PORT = 8080
TOKEN = os.getenv("CAUCE_ADMIN_TOKEN", "increment6-http2-stress-probe-token")
SEED = int(os.getenv("CAUCE_STRESS_SEED", "1592639710"))
WAVES = int(os.getenv("CAUCE_STRESS_WAVES", "3"))
LOAD = int(os.getenv("CAUCE_STRESS_REQUESTS", "32"))
FUZZ_CASES = int(os.getenv("CAUCE_STRESS_FUZZ_CASES", "24"))
CA = "/tls/ca.crt"

def conn(timeout=4):
    raw = socket.create_connection((HOST, PORT), timeout)
    if MODE == "tls":
        ctx = ssl.create_default_context(cafile=CA); ctx.set_alpn_protocols(["h2"])
        raw = ctx.wrap_socket(raw, server_hostname="localhost")
        assert raw.selected_alpn_protocol() == "h2", raw.selected_alpn_protocol()
    raw.settimeout(timeout)
    h2 = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
    h2.initiate_connection(); raw.sendall(h2.data_to_send())
    return raw, h2

def metrics():
    req = urllib.request.Request("http://127.0.0.1:9090/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    text = urllib.request.urlopen(req, timeout=1).read().decode()
    values = {}
    for line in text.splitlines():
        if line.startswith("cauce_") and " " in line:
            k, v = line.split(None, 1)
            try: values[k] = int(float(v))
            except ValueError: pass
    required=("cauce_clients_active", "cauce_requests_active", "cauce_upstream_connections_in_use", "cauce_upstream_streams_active", "cauce_http2_tasks_active")
    missing=[key for key in required if key not in values]
    if missing: raise AssertionError(f"required live gauges missing: {missing}; got {sorted(values)}")
    return values

def backend_stats():
    return json.load(urllib.request.urlopen("http://backend:7003/stats", timeout=1))

def call(path="/", method="GET", body=b"", read=True, timeout=3):
    raw, h2 = conn(timeout)
    sid = h2.get_next_available_stream_id()
    headers = [(":method", method), (":scheme", "https" if MODE == "tls" else "http"), (":authority", f"proxy-{MODE}:8080"), (":path", path)]
    if body: headers.extend([("content-length", str(len(body))), ("content-type", "application/octet-stream")])
    h2.send_headers(sid, headers, end_stream=not body)
    if body:
        for offset in range(0,len(body),16384):
            chunk=body[offset:offset+16384]
            h2.send_data(sid,chunk,end_stream=(offset+len(chunk)==len(body)))
    raw.sendall(h2.data_to_send())
    status = None; received = 0; ended = False
    if read:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not ended:
            try: data = raw.recv(65536)
            except socket.timeout: continue
            if not data: break
            for event in h2.receive_data(data):
                if isinstance(event, ResponseReceived) and event.stream_id == sid: status = int(event.headers[0][1])
                elif isinstance(event, DataReceived) and event.stream_id == sid:
                    received += len(event.data); h2.acknowledge_received_data(event.flow_controlled_length, sid)
                elif isinstance(event, (StreamEnded, StreamReset)) and event.stream_id == sid: ended = True
            pending = h2.data_to_send()
            if pending: raw.sendall(pending)
    raw.close()
    return status, received

def wait_gauges_zero(timeout=7):
    end=time.monotonic()+timeout
    last={}
    while time.monotonic()<end:
        try:last=metrics()
        except Exception as e:
            last={"readiness_error":repr(e)};time.sleep(.05);continue
        keys=("cauce_clients_active", "cauce_requests_active", "cauce_upstream_streams_active", "cauce_http2_tasks_active", "cauce_upstream_connections_in_use")
        if all(last.get(k)==0 for k in keys):
            return last
        time.sleep(.05)
    raise AssertionError(f"capacity did not recover (deadline, last metrics={last})")

def process_sample():
    # This container shares the proxy PID namespace. PID 1 is Cauce.
    try:
        fd_count=len(list(Path('/proc/1/fd').iterdir()))
        status=Path('/proc/1/status').read_text()
        get=lambda key: next((int(line.split()[1]) for line in status.splitlines() if line.startswith(key+':')), None)
        rss=get('VmRSS'); threads=get('Threads')
    except Exception as e: return {"sample_error": str(e)}
    return {"fds":fd_count,"rss_kib":rss,"threads":threads}

def snapshot_loop(stop, samples):
    while not stop.is_set():
        try: samples.append({"at":time.monotonic(), "gauges":metrics(), "backend":backend_stats(), "process":process_sample()})
        except Exception as e: samples.append({"at":time.monotonic(),"error":str(e)})
        stop.wait(.05)

def load_wave():
    barrier=threading.Barrier(LOAD+1)
    statuses=[]
    def worker(i):
        barrier.wait()
        try: statuses.append(call(f"/delay?ms=1200&n={i}"))
        except Exception as e: statuses.append(("error",str(e)))
    with ThreadPoolExecutor(max_workers=LOAD) as pool:
        futures=[pool.submit(worker,i) for i in range(LOAD)]
        barrier.wait()
        end=time.monotonic()+5;observed=[metrics()]
        while time.monotonic()<end and not all(f.done() for f in futures):
            observed.append(metrics())
            time.sleep(.04)
        peak={key:max(g[key] for g in observed) for key in observed[0]}
        combined_saturation=any(g['cauce_requests_active']==8 and g['cauce_upstream_streams_active']>=4 and g['cauce_upstream_connections_in_use']==2 for g in observed)
        for f in futures: f.result(timeout=6)
    assert len(statuses)==LOAD
    assert peak["cauce_requests_active"]==8, f"did not saturate request permits: {peak}"
    assert peak["cauce_clients_active"]<=16, peak
    assert 4<=peak["cauce_upstream_streams_active"]<=8, f"upstream stream quota was not exercised within 4..8 active slots: {peak}"
    assert peak["cauce_upstream_connections_in_use"]==2, f"did not saturate upstream physical connection permits: {peak}"
    assert peak["cauce_http2_tasks_active"]>=8, f"did not observe active HTTP/2 handler tasks: {peak}"
    assert combined_saturation,f"request, stream, and physical connection caps were not simultaneously occupied: {observed}"
    assert any(x[0]==200 for x in statuses), statuses[:10]
    backend=backend_stats()
    assert backend["peakConnections"]<=2 and backend["activeConnections"]<=2, backend
    assert 4<=backend["peakStreams"]<=8, f"backend stream quota observations outside 4..8: {backend}"
    return {"peak":peak,"combined_saturation_sample":next(g for g in observed if g['cauce_requests_active']==8 and g['cauce_upstream_streams_active']>=4 and g['cauce_upstream_connections_in_use']==2),"peak_sample_count":len(observed),"configured_upstream_stream_quota":8,"backend_peak":backend,"status_counts":{str(x[0]):sum(1 for y in statuses if y[0]==x[0]) for x in statuses}}

def cancel_case():
    # Negotiate a zero downstream stream window. Response HEADERS remain writable;
    # DATA is forbidden until WINDOW_UPDATE, so the backend must stay flow-controlled.
    raw=socket.create_connection((HOST,PORT),4)
    if MODE=='tls':
        ctx=ssl.create_default_context(cafile=CA);ctx.set_alpn_protocols(['h2']);raw=ctx.wrap_socket(raw,server_hostname='localhost')
    raw.settimeout(.1);h2=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8'))
    h2.initiate_connection();h2.update_settings({SettingCodes.INITIAL_WINDOW_SIZE:0})
    sid=h2.get_next_available_stream_id()
    h2.send_headers(sid,[(':method','GET'),(':scheme','https' if MODE=='tls' else 'http'),(':authority',f'proxy-{MODE}:8080'),(':path','/bulk')],end_stream=True)
    raw.sendall(h2.data_to_send());data=b'';frames=[];until=time.monotonic()+.25
    while time.monotonic()<until:
        try:part=raw.recv(65536)
        except socket.timeout:continue
        if not part:break
        data+=part
        while len(data)>=9:
            size=int.from_bytes(data[:3],'big')
            if len(data)<9+size:break
            typ,flags=data[3],data[4];stream_id=int.from_bytes(data[5:9],'big')&0x7fffffff
            frames.append({"type":typ,"flags":flags,"stream_id":stream_id,"length":size});data=data[9+size:]
    before=metrics();backend_before=backend_stats()
    assert any(f['type']==1 and f['stream_id']==sid for f in frames),frames
    assert not any(f['type']==0 and f['stream_id']==sid for f in frames),frames
    assert before['cauce_requests_active']>=1 and backend_before['activeStreams']>=1,(before,backend_before)
    h2.reset_stream(sid, error_code=ErrorCodes.CANCEL); raw.sendall(h2.data_to_send()); raw.close()
    return {"downstream_initial_stream_window_bytes":0,"downstream_data_frames_before_reset":0,"response_frames":frames,"active_before_reset":before,"backend_before_reset":backend_before,"recovered":wait_gauges_zero()}

def per_connection_cap_case():
    raw,h2=conn(); deadline=time.monotonic()+2; advertised=None
    while time.monotonic()<deadline and advertised is None:
        try:data=raw.recv(65536)
        except socket.timeout:continue
        if not data:break
        for event in h2.receive_data(data):
            if isinstance(event,RemoteSettingsChanged):
                setting=event.changed_settings.get(SettingCodes.MAX_CONCURRENT_STREAMS)
                if setting:advertised=setting.new_value
        pending=h2.data_to_send()
        if pending:raw.sendall(pending)
    assert advertised==4,f"proxy advertised max_concurrent_streams={advertised}, expected 4"
    for _ in range(4):
        sid=h2.get_next_available_stream_id()
        h2.send_headers(sid,[(':method','GET'),(':scheme','https' if MODE=='tls' else 'http'),(':authority',f'proxy-{MODE}:8080'),(':path','/delay?ms=900')],end_stream=True)
    raw.sendall(h2.data_to_send())
    # hyper-h2 correctly honors the peer setting, so inject a fifth legal HEADERS
    # frame directly to exercise the server-side concurrency guard.
    excess=HeadersFrame(9);excess.data=Encoder().encode([(':method','GET'),(':scheme','https' if MODE=='tls' else 'http'),(':authority',f'proxy-{MODE}:8080'),(':path','/delay?ms=900')]);excess.flags.add('END_HEADERS');excess.flags.add('END_STREAM')
    raw.sendall(excess.serialize());time.sleep(.2);peak=metrics()
    assert peak["cauce_requests_active"]<=4,peak
    assert peak["cauce_requests_active"]==4,f"one inbound connection did not hit limit: {peak}"
    terminal=set();statuses={};deadline=time.monotonic()+4;buf=b""
    while time.monotonic()<deadline and 9 not in terminal:
        try:data=raw.recv(65536)
        except socket.timeout:continue
        if not data:break
        buf+=data
        while len(buf)>=9:
            size=int.from_bytes(buf[:3],'big')
            if len(buf)<9+size:break
            typ=buf[3];stream_id=int.from_bytes(buf[5:9],'big')&0x7fffffff;payload=buf[9:9+size];buf=buf[9+size:]
            if typ==1:statuses[stream_id]=payload.hex()
            elif typ==3:terminal.add(stream_id)
            elif typ==7:terminal.add(9)
    raw.close();recovered=wait_gauges_zero();assert 9 in terminal,(statuses,terminal)
    return {"advertised_stream_limit":advertised,"active_at_cap":peak,"stream_statuses":statuses,"terminal_streams":len(terminal),"recovered":recovered}

def early_window_case():
    # Backend advertises initial stream window zero and sends 413 before upload can progress.
    status,_=call('/early',method='POST',body=b'x'*(32*1024),timeout=3)
    assert status==413, f"expected early 413 with blocked upstream upload, got {status}"
    baseline=wait_gauges_zero()
    healthy,_=call('/',timeout=3); assert healthy==200, healthy
    return {"early_status":status,"recovered_status":healthy,"gauges":baseline}

def read_frame_result(sock, timeout=1, ping_ack=False):
    end=time.monotonic()+timeout; sock.settimeout(.1); data=b""; frames=[]
    while len(data)<65536 and time.monotonic()<end:
        try:chunk=sock.recv(65536)
        except socket.timeout:continue
        if not chunk:return frames,"eof"
        data+=chunk
        while len(data)>=9:
            size=int.from_bytes(data[:3],'big')
            if len(data)<9+size:break
            typ,flags=data[3],data[4];sid=int.from_bytes(data[5:9],'big')&0x7fffffff
            frames.append({"type":typ,"flags":flags,"stream_id":sid,"payload_hex":data[9:9+size].hex()});data=data[9+size:]
            if typ==7:return frames,"goaway"
            if typ==3:return frames,"stream_reset"
            if ping_ack and typ==6 and flags&1:return frames,"ping_ack"
    return frames,"timeout"

def raw_socket():
    raw=socket.create_connection((HOST,PORT),3)
    if MODE=='tls':
        ctx=ssl.create_default_context(cafile=CA);ctx.set_alpn_protocols(['h2']);raw=ctx.wrap_socket(raw,server_hostname='localhost')
    raw.settimeout(1);raw.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'+bytes([0,0,0,4,0,0,0,0,0]))
    return raw

def fuzz(seed,count=24):
    """Seeded malformed frames on real TCP/TLS with explicit protocol oracles."""
    rng=random.Random(seed); corpus=[]
    corpus_path=Path(f'/results/http2-fuzz-corpus-{MODE}.json')
    Path(f'/results/http2-fuzz-failure-{MODE}.json').unlink(missing_ok=True)
    corpus_path.unlink(missing_ok=True)
    def persist():
        Path('/results').mkdir(exist_ok=True);corpus_path.write_text(json.dumps({"mode":MODE,"seed":seed,"cases":corpus},indent=2)+"\n")
    def failed(record,error):
        Path('/results').mkdir(exist_ok=True)
        Path(f'/results/http2-fuzz-failure-{MODE}.json').write_text(json.dumps({"mode":MODE,"seed":seed,"error":repr(error),"failing_input":record},indent=2)+"\n")
    # Unknown frames are ignored: PING on this very socket must get an ACK.
    raw=raw_socket();unknown=bytes([0,0,0,0x0b,0,0,0,0,0]);ping=bytes([0,0,8,6,0,0,0,0,0])+b'12345678'
    raw.sendall(unknown+ping);frames,outcome=read_frame_result(raw,ping_ack=True)
    record={"case":"unknown_frame_ignored_reused","seed":seed,"wire_hex":unknown.hex()+ping.hex(),"frames":frames,"outcome":outcome,"oracle":"PING ACK on same connection"}
    if outcome!='ping_ack' or not any(f['type']==6 and f['flags']&1 for f in frames):failed(record,(frames,outcome));raise AssertionError((frames,outcome))
    corpus.append(record);persist();raw.close()
    kinds=("data_stream_zero","orphan_continuation","reserved_stream_bit","bad_hpack","bad_frame_length","bad_flags")
    for i in range(count):
        kind=rng.choice(kinds);sid=rng.choice((0,1,3,5,0x7fffffff));n=rng.randrange(0,8);payload=b"";typ=0;flags=0;declared=None
        if kind=="data_stream_zero":typ,flags,sid,payload=0,0,0,b'x'*n
        elif kind=="orphan_continuation":typ,flags,sid,payload=9,4,sid or 1,bytes(rng.randrange(256) for _ in range(n))
        elif kind=="reserved_stream_bit":typ,flags,sid,payload=0,0,0x80000001,b'x'
        elif kind=="bad_hpack":typ,flags,sid,payload=1,5,1,b'\xff'+bytes(rng.randrange(256) for _ in range(n))
        elif kind=="bad_frame_length":typ,flags,sid,payload=0,0,1,b'x'*n;declared=n+rng.randrange(1,8)
        else:typ,flags,sid,payload=4,0xff,0,bytes(rng.randrange(256) for _ in range(max(1,n)))
        size=declared if declared is not None else len(payload)
        wire=size.to_bytes(3,'big')+bytes([typ,flags])+sid.to_bytes(4,'big')+payload;raw=None
        try:
            raw=raw_socket();raw.sendall(wire);frames,outcome=read_frame_result(raw,.8)
            terminal=outcome in ('eof','goaway','stream_reset')
            record={"case":kind,"seed":seed,"index":i,"mutation":{"stream_id":sid,"declared_length":size,"actual_length":len(payload),"flags":flags,"payload_hex":payload.hex()},"frames":frames,"outcome":outcome,"oracle":"connection error/reset, EOF, GOAWAY, or incomplete-frame timeout"}
            if not terminal and not (kind=='bad_frame_length' and outcome=='timeout'):
                failed(record,(frames,outcome));raise AssertionError((kind,frames,outcome))
            corpus.append(record);persist()
        except Exception as error:
            if not Path(f'/results/http2-fuzz-failure-{MODE}.json').exists():
                failed({"case":kind,"seed":seed,"index":i,"wire_hex":wire.hex()},error)
            raise
        finally:
            if raw:
                try:raw.close()
                except OSError:pass
        time.sleep(.01)
    # Serialize a legal HEADERS block, enlarge its declared frame length, and truncate bytes.
    raw=raw_socket();h=H2Connection(config=H2Configuration(client_side=True));h.initiate_connection();h.data_to_send()
    h.send_headers(1,[(':method','GET'),(':scheme','https' if MODE=='tls' else 'http'),(':authority',f'proxy-{MODE}:8080'),(':path','/')],end_stream=True);encoded=h.data_to_send()
    pos=0
    while pos+9<=len(encoded):
        size=int.from_bytes(encoded[pos:pos+3],'big')
        if encoded[pos+3]==1:break
        pos+=9+size
    frame=encoded[pos:pos+9+size];block=frame[9:];cut=rng.randrange(1,len(block));declared=len(block)+rng.randrange(1,4)
    partial=declared.to_bytes(3,'big')+frame[3:9]+block[:cut]
    before_timeout=metrics()['cauce_http2_header_timeouts_total'];started=time.monotonic()
    raw.sendall(partial);frames,outcome=read_frame_result(raw,.8);elapsed=time.monotonic()-started
    record={"case":"truncated_headers_deadline","seed":seed,"cut":cut,"declared_length":declared,"actual_length":cut,"wire_hex":partial.hex(),"frames":frames,"outcome":outcome,"elapsed_s":elapsed,"oracle":"EOF after 300 ms absolute HEADERS deadline and timeout counter increment"}
    if outcome != 'eof' or not .25<=elapsed<=.8:
        failed(record,(frames,outcome));raise AssertionError((frames,outcome))
    after_timeout=metrics()['cauce_http2_header_timeouts_total']
    assert after_timeout==before_timeout+1,(before_timeout,after_timeout,record)
    record['timeout_counter_delta']=after_timeout-before_timeout
    corpus.append(record);persist();raw.close()
    good,_=call('/',timeout=3);assert good==200,f"fuzzer left proxy unhealthy: {good}"
    return corpus

def main():
    samples=[]; stop=threading.Event(); sampler=threading.Thread(target=snapshot_loop,args=(stop,samples),daemon=True);sampler.start()
    report={"mode":MODE,"seed":SEED,"waves":WAVES,"load_per_wave":LOAD,"checks":[],"wave_results":[]}
    try:
        ready=urllib.request.urlopen('http://127.0.0.1:9090/ready',timeout=3).read().decode().strip()
        assert ready=='ready',ready
        report['readiness']='admin /ready returned ready'
        base=wait_gauges_zero(); report["baseline"]={"gauges":base,"process":process_sample(),"backend":backend_stats()}
        # Make sure baseline is a settled zero before resets and measurements.
        urllib.request.urlopen('http://backend:7003/reset',timeout=1).read()
        for i in range(WAVES):
            urllib.request.urlopen('http://backend:7003/reset',timeout=1).read()
            report["wave_results"].append(load_wave()); report["checks"].append(f"wave {i+1}: observed saturation under resource caps")
            report["wave_results"][-1]["recovered"]=wait_gauges_zero()
        report["per_connection_cap"]=per_connection_cap_case();report["checks"].append("inbound connection reached configured four stream limit and recovered")
        report["cancellation_recovered"]=cancel_case();report["checks"].append("RST_STREAM cancellation released request and upstream permits")
        report["zero_window_early_response"]=early_window_case();report["checks"].append("zero upstream receive window and early response recovered")
        report["fuzz_cases"]=fuzz(SEED,FUZZ_CASES);report["checks"].append("seeded malformed/truncated frame mutations replayed with protocol oracles and proxy remained healthy")
        report["final_gauges"]=wait_gauges_zero();report["final_process"]=process_sample();report["backend_final"]=backend_stats()
        usable=[s["process"] for s in samples if "process" in s and "fds" in s["process"]]
        assert len(usable)>=5, f"insufficient process samples: {samples}"
        if usable:
            p0=report["baseline"]["process"];p1=report["final_process"]
            peaks={key:max(x[key] for x in usable) for key in ('fds','rss_kib','threads')}
            assert peaks['fds']<=p0['fds']+128 and p1['fds']<=p0['fds']+8,(p0,p1,peaks)
            assert peaks['threads']<=p0['threads']+8 and p1['threads']<=p0['threads']+2,(p0,p1,peaks)
            assert peaks['rss_kib']<=p0['rss_kib']+128*1024 and p1['rss_kib']<=p0['rss_kib']+64*1024,(p0,p1,peaks)
            report['process_tolerance_check']={'baseline':p0,'post_quiescence':p1,'observed_peak':peaks,'tolerance':{'fd_peak_delta':128,'fd_recovered_delta':8,'thread_peak_delta':8,'thread_recovered_delta':2,'rss_peak_delta_kib':131072,'rss_recovered_delta_kib':65536}}
        for sample in samples:
            g=sample.get('gauges',{})
            if g:
                assert g['cauce_clients_active']<=16 and g['cauce_requests_active']<=8 and g['cauce_upstream_connections_in_use']<=2 and g['cauce_upstream_streams_active']<=8 and g['cauce_http2_tasks_active']<=16,g
            b=sample.get('backend',{})
            if b: assert b.get('activeConnections',0)<=2 and b.get('activeStreams',0)<=8,b
        report["resource_samples"]=samples
        report["result"]="PASS"
    except Exception as e:
        report["result"]="FAIL";report["error"]=repr(e);report["resource_samples"]=samples
        replay=Path(f'/results/http2-fuzz-failure-{MODE}.json')
        report["replay"]=json.loads(replay.read_text()) if replay.exists() else {"seed":SEED,"mode":MODE,"last_fuzz_case":report.get("fuzz_cases",[]) [-1] if report.get("fuzz_cases") else None}
        raise
    finally:
        stop.set();sampler.join(timeout=1)
        Path('/results').mkdir(exist_ok=True)
        Path(f"/results/http2-stress-{MODE}.json").write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps(report,separators=(',',':')))

if __name__=='__main__': main()
