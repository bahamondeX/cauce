"""Docker-network client: HTTP/2 frame checks and generic grpcio calls (no protoc)."""
import json, os, socket, ssl, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import grpc
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import ResponseReceived, DataReceived, TrailersReceived, StreamEnded, RemoteSettingsChanged, StreamReset, ConnectionTerminated
from h2.settings import SettingCodes

CA = os.environ.get('CA_FILE', '/tls/ca.crt')
checks=[]

def h2_request(host, tls=False, path='/', headers=None, body=b'', request_trailers=None):
    sock=socket.create_connection((host,8080),timeout=5)
    if tls:
        ctx=ssl.create_default_context(cafile=CA); ctx.set_alpn_protocols(['h2'])
        sock=ctx.wrap_socket(sock,server_hostname='localhost')
        assert sock.selected_alpn_protocol()=='h2', sock.selected_alpn_protocol()
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8',validate_outbound_headers=False))
    conn.initiate_connection(); sock.sendall(conn.data_to_send())
    deadline=time.time()+5; peer_settings_seen=False
    while not peer_settings_seen and time.time()<deadline:
        data=sock.recv(65535)
        if not data: raise AssertionError('peer closed during settings')
        for event in conn.receive_data(data):
            if isinstance(event,RemoteSettingsChanged): peer_settings_seen=True
        sock.sendall(conn.data_to_send())
    assert peer_settings_seen, 'server did not send SETTINGS'
    cap=conn.remote_settings.max_concurrent_streams
    stream=conn.get_next_available_stream_id()
    hs=[(':method','POST' if body or request_trailers else 'GET'),(':scheme','https' if tls else 'http'),(':authority',host+':8080'),(':path',path),('content-length',str(len(body)))]
    hs += list((headers or {}).items())
    conn.send_headers(stream,hs,end_stream=not body and request_trailers is None)
    if body:
        conn.send_data(stream,body,end_stream=request_trailers is None)
    if request_trailers is not None:
        conn.send_headers(stream,list(request_trailers.items()),end_stream=True)
    sock.sendall(conn.data_to_send())
    response=[]; trailers=[]; chunks=[]; ended=False
    while not ended and time.time()<deadline:
        data=sock.recv(65535)
        if not data: break
        for event in conn.receive_data(data):
            if isinstance(event,ResponseReceived) and event.stream_id==stream: response=event.headers
            elif isinstance(event,DataReceived) and event.stream_id==stream:
                chunks.append(event.data); conn.acknowledge_received_data(event.flow_controlled_length,event.stream_id)
            elif isinstance(event,TrailersReceived) and event.stream_id==stream: trailers=event.headers
            elif isinstance(event,StreamEnded) and event.stream_id==stream: ended=True
        sock.sendall(conn.data_to_send())
    sock.close()
    if not ended: raise AssertionError(f'no response completion path={path} headers={response}')
    return int(dict(response).get(':status','0')),dict(response),dict(trailers),b''.join(chunks),cap


def overlimit_frontend_test(host,tls=False):
    sock=socket.create_connection((host,8080),timeout=4)
    if tls:
        ctx=ssl.create_default_context(cafile=CA);ctx.set_alpn_protocols(['h2']);sock=ctx.wrap_socket(sock,server_hostname='localhost')
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8',validate_outbound_headers=False));conn.initiate_connection();sock.sendall(conn.data_to_send())
    setting=False;end=time.time()+3
    while not setting and time.time()<end:
        for e in conn.receive_data(sock.recv(65535)):
            if isinstance(e,RemoteSettingsChanged):setting=True
        sock.sendall(conn.data_to_send())
    assert setting and conn.remote_settings.max_concurrent_streams==4
    # Deliberately override the client's local peer-cap guard to test server-side enforcement.
    conn.remote_settings._settings[SettingCodes.MAX_CONCURRENT_STREAMS]=deque([100])
    ids=[]
    for _ in range(5):
        sid=conn.get_next_available_stream_id();ids.append(sid)
        conn.send_headers(sid,[(':method','GET'),(':scheme','https' if tls else 'http'),(':authority',host+':8080'),(':path','/wait'),('x-delay-ms','1200')],end_stream=True)
    sock.sendall(conn.data_to_send());refused=False;limit=time.time()+2
    sock.settimeout(.2)
    while time.time()<limit and not refused:
        try:data=sock.recv(65535)
        except socket.timeout:continue
        if not data:break
        for e in conn.receive_data(data):
            if isinstance(e,StreamReset) and e.stream_id==ids[4]:refused=True
        sock.sendall(conn.data_to_send())
    sock.close();assert refused,f'fifth concurrent stream was not rejected; stream_ids={ids}'
    return 'fifth concurrent downstream stream rejected at advertised cap 4'

def cap_route_test(host,tls,path,limit,count):
    def request(_):return h2_request(host,tls=tls,path=path,headers={'x-delay-ms':'450'})
    with ThreadPoolExecutor(max_workers=count) as pool: results=list(pool.map(request,range(count)))
    assert all(r[0]==200 for r in results),(path,[r[:3] for r in results])
    peaks={}
    for r in results:
        session=r[1]['x-backend-session'];peak=int(r[1]['x-backend-peak']);peaks[session]=max(peaks.get(session,0),peak)
    assert peaks and max(peaks.values())<=limit,(path,peaks)
    return peaks

def active_stream_client(require_goaway=False):
    sock=socket.create_connection(('proxy-clear',8080),timeout=5)
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8'));conn.initiate_connection();sock.sendall(conn.data_to_send())
    done=False
    while not done:
        for e in conn.receive_data(sock.recv(65535)):
            if isinstance(e,RemoteSettingsChanged):done=True
        sock.sendall(conn.data_to_send())
    sid=conn.get_next_available_stream_id();conn.send_headers(sid,[(':method','GET'),(':scheme','http'),(':authority','proxy-clear:8080'),(':path','/wait'),('x-delay-ms','1200')],end_stream=True);sock.sendall(conn.data_to_send())
    print('ACTIVE',flush=True);response={};chunks=[];ended=False;goaway=False;end=time.time()+5
    while time.time()<end and not ended:
        data=sock.recv(65535)
        if not data:break
        for e in conn.receive_data(data):
            if isinstance(e,ResponseReceived) and e.stream_id==sid:response=dict(e.headers)
            elif isinstance(e,DataReceived) and e.stream_id==sid:chunks.append(e.data);conn.acknowledge_received_data(e.flow_controlled_length,e.stream_id)
            elif isinstance(e,ConnectionTerminated):goaway=True
            elif isinstance(e,StreamEnded) and e.stream_id==sid:ended=True
        sock.sendall(conn.data_to_send())
    if require_goaway and not goaway:
        sock.settimeout(.4)
        try:
            data=sock.recv(65535)
            if data:
                goaway=any(isinstance(e,ConnectionTerminated) for e in conn.receive_data(data))
        except socket.timeout:pass
    sock.close();assert ended and response.get(':status')=='200',(response,ended,goaway)
    if require_goaway:assert goaway,'SIGTERM did not send HTTP/2 GOAWAY'
    print(json.dumps({'result':'PASS','status':response.get(':status'),'goaway':goaway}),flush=True)


def open_early_upload():
    host='proxy-clear';sock=socket.create_connection((host,8080),timeout=5)
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8'));conn.initiate_connection();sock.sendall(conn.data_to_send())
    settings=False;limit=time.time()+3
    while not settings and time.time()<limit:
        for e in conn.receive_data(sock.recv(65535)):
            if isinstance(e,RemoteSettingsChanged):settings=True
        sock.sendall(conn.data_to_send())
    sid=conn.get_next_available_stream_id()
    conn.send_headers(sid,[(':method','POST'),(':scheme','http'),(':authority','proxy-clear:8080'),(':path','/early/upload')],end_stream=False)
    # Fill most of the downstream receive window while the backend's initial
    # stream window is zero, then leave END_STREAM pending.
    for _ in range(4):conn.send_data(sid,b'u'*15000,end_stream=False)
    sock.sendall(conn.data_to_send())
    status=None;ended=False;limit=time.time()+3
    while time.time()<limit and not ended:
        data=sock.recv(65535)
        if not data:break
        for e in conn.receive_data(data):
            if isinstance(e,ResponseReceived) and e.stream_id==sid:status=int(dict(e.headers)[':status'])
            elif isinstance(e,DataReceived) and e.stream_id==sid:conn.acknowledge_received_data(e.flow_controlled_length,e.stream_id)
            elif isinstance(e,StreamEnded) and e.stream_id==sid:ended=True
        sock.sendall(conn.data_to_send())
    assert status==413 and ended,(status,ended)
    return sock,conn,sid

def stalled_upload_deadline_test():
    with ThreadPoolExecutor(max_workers=8) as pool: held=list(pool.map(lambda _:open_early_upload(),range(8)))
    rejected=h2_request('proxy-clear',path='/early/check')
    assert rejected[0]==503,('upstream HTTP/2 capacity not held by blocked upload',rejected[:3])
    time.sleep(5.5) # request_ms=5000; upstream initial stream window remains zero.
    recovered=h2_request('proxy-clear',path='/early/check')
    assert recovered[0]==413,('upstream HTTP/2 capacity did not recover after per-stream deadline',recovered[:3])
    for sock,conn,sid in held:
        try:conn.reset_stream(sid);sock.sendall(conn.data_to_send())
        except Exception:pass
        sock.close()
    return 'early response with zero upstream window retained request capacity until deadline; same-route request recovered'

def grpc_channel(target, tls=False):
    if not tls: return grpc.insecure_channel(target)
    creds=grpc.ssl_channel_credentials(root_certificates=open(CA,'rb').read())
    return grpc.secure_channel(target,creds)

def rpc_calls(target,tls=False):
    ch=grpc_channel(target,tls)
    unary=ch.unary_unary('/fixture.Echo/Unary',request_serializer=lambda x:x,response_deserializer=lambda x:x)
    client_stream=ch.stream_unary('/fixture.Echo/ClientStream',request_serializer=lambda x:x,response_deserializer=lambda x:x)
    server_stream=ch.unary_stream('/fixture.Echo/ServerStream',request_serializer=lambda x:x,response_deserializer=lambda x:x)
    bidi=ch.stream_stream('/fixture.Echo/Bidi',request_serializer=lambda x:x,response_deserializer=lambda x:x)
    call=unary.with_call(b'hello',metadata=(('x-probe','metadata'),),timeout=5)
    assert call[0]==b'echo:hello'; assert ('backend-initial','grpc') in call[1].initial_metadata(); assert ('backend-trailer','complete') in call[1].trailing_metadata()
    assert client_stream(iter([b'a',b'b',b'c']),timeout=5)==b'a|b|c'
    assert list(server_stream(b's',timeout=5))==[b's:0',b's:1',b's:2']
    assert list(bidi(iter([b'x',b'y']),timeout=5))==[b'bidi:x',b'bidi:y']
    try: unary(b'fail',timeout=5); raise AssertionError('gRPC error was swallowed')
    except grpc.RpcError as e:
        assert e.code()==grpc.StatusCode.RESOURCE_EXHAUSTED,e.code()
        assert e.details()=='fixture failure'
        assert ('backend-trailer','complete') in e.trailing_metadata()
    # A cancelled/deadline stream must not make a sibling RPC unusable.
    slow=unary.future(b'slow',timeout=5); time.sleep(.08); slow.cancel()
    assert unary(b'sibling',timeout=3)==b'echo:sibling'
    try: unary(b'slow',timeout=.08); raise AssertionError('deadline ignored')
    except grpc.RpcError as e: assert e.code()==grpc.StatusCode.DEADLINE_EXCEEDED,e.code()
    ch.close()


def paused_grpc_window_test():
    host='proxy-clear'; sock=socket.create_connection((host,8080),timeout=4)
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8',validate_outbound_headers=False))
    conn.initiate_connection();sock.sendall(conn.data_to_send())
    deadline=time.time()+3; settings=False
    while not settings and time.time()<deadline:
        data=sock.recv(65535)
        for e in conn.receive_data(data):
            if isinstance(e,RemoteSettingsChanged): settings=True
        sock.sendall(conn.data_to_send())
    assert settings and conn.remote_settings.max_concurrent_streams==4
    def grpc_headers(path):
        return [(':method','POST'),(':scheme','http'),(':authority','proxy-clear:8080'),(':path',path),('content-type','application/grpc'),('te','trailers'),('grpc-timeout','700m')]
    large=conn.get_next_available_stream_id()
    conn.send_headers(large,grpc_headers('/fixture.Echo/Unary'),end_stream=False)
    payload=b'large';conn.send_data(large,b'\0'+len(payload).to_bytes(4,'big')+payload,end_stream=True);sock.sendall(conn.data_to_send())
    seen=0; conn_flow=0; reset=False; ended=False; large_trailers={}; limit=time.time()+3
    sock.settimeout(.2)
    while time.time()<limit and not reset and not ended:
        try: data=sock.recv(65535)
        except socket.timeout: continue
        if not data: break
        for e in conn.receive_data(data):
            if isinstance(e,DataReceived) and e.stream_id==large:
                seen+=len(e.data);conn_flow+=e.flow_controlled_length  # withhold stream credit to stall only this stream
            elif isinstance(e,TrailersReceived) and e.stream_id==large: large_trailers=dict(e.headers)
            elif isinstance(e,StreamReset) and e.stream_id==large: reset=True
            elif isinstance(e,StreamEnded) and e.stream_id==large: ended=True
        sock.sendall(conn.data_to_send())
    assert seen>0 and seen < 4*1024*1024, f'large stream did not stall at the downstream window: {seen}'
    assert reset or (ended and large_trailers.get('grpc-status')=='4'),(f'flow-control-blocked stream did not terminate at its deadline; ended={ended} reset={reset} trailers={large_trailers}')
    conn.increment_flow_control_window(conn_flow);sock.sendall(conn.data_to_send())
    sibling=conn.get_next_available_stream_id(); payload=b'sibling'
    conn.send_headers(sibling,grpc_headers('/fixture.Echo/Unary'),end_stream=False)
    conn.send_data(sibling,b'\0'+len(payload).to_bytes(4,'big')+payload,end_stream=True);sock.sendall(conn.data_to_send())
    response=[];trailers={};chunks=[];done=False;limit=time.time()+3
    while time.time()<limit and not done:
        try:data=sock.recv(65535)
        except socket.timeout:continue
        if not data: break
        for e in conn.receive_data(data):
            if isinstance(e,ResponseReceived) and e.stream_id==sibling: response=e.headers
            elif isinstance(e,DataReceived) and e.stream_id==sibling:
                chunks.append(e.data);conn.acknowledge_received_data(e.flow_controlled_length,e.stream_id)
            elif isinstance(e,TrailersReceived) and e.stream_id==sibling: trailers=dict(e.headers)
            elif isinstance(e,StreamEnded) and e.stream_id==sibling: done=True
        sock.sendall(conn.data_to_send())
    assert done and dict(response).get(':status')=='200',(response,trailers)
    framed=b''.join(chunks);assert framed[0]==0 and int.from_bytes(framed[1:5],'big')==len(framed)-5 and framed[5:]==b'echo:sibling',framed[:40]
    assert trailers.get('grpc-status')=='0',trailers
    sock.close()
    return f'blocked gRPC stream stopped at {seen} response bytes; terminal status/reset isolated sibling RPC'


def grpc_deadline_status_test():
    sock=socket.create_connection(('proxy-clear',8080),timeout=4)
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8',validate_outbound_headers=False))
    conn.initiate_connection();sock.sendall(conn.data_to_send());settings=False;until=time.time()+3
    while not settings and time.time()<until:
        for e in conn.receive_data(sock.recv(65535)):
            if isinstance(e,RemoteSettingsChanged):settings=True
        sock.sendall(conn.data_to_send())
    assert settings
    sid=conn.get_next_available_stream_id()
    conn.send_headers(sid,[(':method','POST'),(':scheme','http'),(':authority','proxy-clear:8080'),(':path','/fixture.Echo/Unary'),('content-type','application/grpc'),('te','trailers'),('grpc-timeout','80m')],end_stream=False)
    payload=b'slow';conn.send_data(sid,b'\0'+len(payload).to_bytes(4,'big')+payload,end_stream=True);sock.sendall(conn.data_to_send())
    trailers={};response={};ended=False;reset=False;until=time.time()+2;sock.settimeout(.2)
    while time.time()<until and not ended and not reset:
        try:data=sock.recv(65535)
        except socket.timeout:continue
        if not data:break
        for e in conn.receive_data(data):
            if isinstance(e,ResponseReceived) and e.stream_id==sid:response=dict(e.headers)
            elif isinstance(e,TrailersReceived) and e.stream_id==sid:trailers=dict(e.headers)
            elif isinstance(e,StreamEnded) and e.stream_id==sid:ended=True
            elif isinstance(e,StreamReset) and e.stream_id==sid:reset=True
        sock.sendall(conn.data_to_send())
    sock.close()
    assert ended and not reset and (response.get('grpc-status')=='4' or trailers.get('grpc-status')=='4'), (ended,reset,response,trailers)
    sock=socket.create_connection(('proxy-clear',8080),timeout=4)
    conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8',validate_outbound_headers=False));conn.initiate_connection();sock.sendall(conn.data_to_send())
    while True:
        events=conn.receive_data(sock.recv(65535));sock.sendall(conn.data_to_send())
        if any(isinstance(e,RemoteSettingsChanged) for e in events):break
    sid=conn.get_next_available_stream_id()
    conn.send_headers(sid,[(':method','POST'),(':scheme','http'),(':authority','proxy-clear:8080'),(':path','/fixture.Echo/NoHeaders'),('content-type','application/grpc'),('te','trailers'),('grpc-timeout','80m')],end_stream=False)
    payload=b'x';conn.send_data(sid,b'\0'+len(payload).to_bytes(4,'big')+payload,end_stream=True);sock.sendall(conn.data_to_send())
    response={};trailers={};ended=False;until=time.time()+2;sock.settimeout(.2)
    while time.time()<until and not ended:
        try:data=sock.recv(65535)
        except socket.timeout:continue
        if not data:break
        for e in conn.receive_data(data):
            if isinstance(e,ResponseReceived) and e.stream_id==sid:response.update(e.headers)
            elif isinstance(e,TrailersReceived) and e.stream_id==sid:trailers.update(e.headers)
            elif isinstance(e,StreamEnded) and e.stream_id==sid:ended=True
        sock.sendall(conn.data_to_send())
    sock.close();assert ended and response.get('grpc-status',trailers.get('grpc-status'))=='4',(ended,response,trailers)
    return 'readable and trailers-only gRPC deadlines return grpc-status 4'

# Cleartext HTTP/2 prior knowledge; peer SETTINGS and trailer forwarding.
for label,tls,target in [('clear',False,'proxy-clear:8080'),('tls',True,'proxy-tls:8080')]:
    status,headers,trailers,body,cap=h2_request('proxy-'+label,tls=tls,headers={'x-fixture-request':'copied'})
    assert status==200,(label,status,headers)
    doc=json.loads(body); assert doc['session'].startswith('up-'),doc
    assert headers.get('x-backend-initial')=='yes',headers
    assert headers.get('x-request-copy')=='copied',headers
    assert cap==4, (label,cap)
    assert headers.get('x-request-ended-in-headers')=='true',(label,headers)
    tail_only=h2_request('proxy-'+label,tls=tls,path='/request-trailer',
                         request_trailers={'x-request-trailer':'only-trailers'})
    assert tail_only[0]==200 and tail_only[1].get('x-request-trailer-copy')=='only-trailers',(label,tail_only[:3])
    assert tail_only[1].get('x-request-ended-in-headers')=='false',(label,tail_only[:3])
    checks.append(f'{label}: empty uploads end in initial HEADERS; zero-data request trailers forwarded')
    reqtrail=h2_request('proxy-'+label,tls=tls,path='/request-trailer',body=b'payload',request_trailers={'x-request-trailer':'client-finished'})
    assert reqtrail[0]==200 and reqtrail[1].get('x-request-trailer-copy')=='client-finished',(label,reqtrail[:3])
    h1=h2_request('proxy-'+label,tls=tls,path='/http1/check')
    assert h1[0]==200 and h1[1].get('x-http1-backend')=='yes', (label,h1[:3])
    no_grpc_downgrade=h2_request('proxy-'+label,tls=tls,path='/http1/fixture.Echo/Unary',headers={'content-type':'application/grpc','te':'trailers'})
    assert no_grpc_downgrade[0]==502,(label,no_grpc_downgrade[:3])
    local_peaks=cap_route_test('proxy-'+label,tls,'/localcap/wait',2,6)
    peer_peaks=cap_route_test('proxy-'+label,tls,'/peercap/wait',4,8)
    checks.append(f'{label}: HTTP/2-to-HTTP/1 translation; local upstream stream cap 2 {local_peaks}; peer SETTINGS cap 4 {peer_peaks}')
    # Conflicting :authority and Host, invalid gRPC timeout and an oversized header list are rejected.
    bad_authority=h2_request('proxy-'+label,tls=tls,headers={'host':'other.invalid'})
    assert bad_authority[0]==400, (label,bad_authority[:3])
    invalid_timeout=h2_request('proxy-'+label,tls=tls,path='/fixture.Echo/Unary',headers={'content-type':'application/grpc','te':'trailers','grpc-timeout':'invalid'})
    assert invalid_timeout[0]==400, (label,invalid_timeout[:3])
    many={f'x-extra-{i}':'v' for i in range(140)}
    header_limit=h2_request('proxy-'+label,tls=tls,headers=many)
    assert header_limit[0]==431, (label,header_limit[:3])
    # Requests use separate downstream sockets and share one upstream HTTP/2 session.
    def once(i):
        return h2_request('proxy-'+label,tls=tls,path='/wait',headers={'x-delay-ms':'250'})
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(once,range(4)))
    sessions=[r[1]['x-backend-session'] for r in results]
    assert len(set(sessions))==1,(label,sessions)
    rpc_calls(target,tls)
    # Backend GOAWAY retires the upstream session; the next request must connect afresh.
    goaway=h2_request('proxy-'+label,tls=tls,path='/goaway')
    assert goaway[0]==200, (label,goaway[:3])
    time.sleep(.1)
    replacement=h2_request('proxy-'+label,tls=tls)
    old=goaway[1]['x-backend-session']; new=json.loads(replacement[3])['session']
    assert old!=new, (label,old,new)
    checks.append(f'{label}: ALPN/H2C, stream cap, header limits, authority validation, multiplex reuse, trailers, GOAWAY replacement, all four gRPC shapes/status/cancel/deadline')

checks.append(overlimit_frontend_test('proxy-clear'))

# HTTP/1 inbound remains supported over the TLS listener; H2-upstream integration is exercised above.
ctx=ssl.create_default_context(cafile=CA); ctx.set_alpn_protocols(['http/1.1'])
s=ctx.wrap_socket(socket.create_connection(('proxy-tls',8080)),server_hostname='localhost')
s.sendall(b'GET / HTTP/1.1\r\nHost: proxy-tls:8080\r\nConnection: close\r\n\r\n')
raw=b''
while True:
    data=s.recv(65535)
    if not data: break
    raw+=data
s.close(); assert b'200' in raw.split(b'\r\n',1)[0],raw[:200]
checks.append('TLS listener negotiates HTTP/1.1 when ALPN selects it')
checks.append(grpc_deadline_status_test())
checks.append(paused_grpc_window_test())
checks.append(stalled_upload_deadline_test())


print(json.dumps({'result':'PASS','checks':checks,'count':len(checks)},indent=2))
