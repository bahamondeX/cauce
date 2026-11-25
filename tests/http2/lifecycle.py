"""Hold one live H2 request while the host sends HUP or TERM to the proxy."""
import json, socket, sys, time
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RemoteSettingsChanged, ResponseReceived, StreamEnded, ConnectionTerminated

mode=sys.argv[1]
sock=socket.create_connection(('proxy-clear',8080),timeout=5)
conn=H2Connection(config=H2Configuration(client_side=True,header_encoding='utf-8'))
conn.initiate_connection();sock.sendall(conn.data_to_send())
settings=False
while not settings:
    data=sock.recv(65535)
    if not data:raise RuntimeError('proxy closed before SETTINGS')
    for e in conn.receive_data(data):
        if isinstance(e,RemoteSettingsChanged):settings=True
    sock.sendall(conn.data_to_send())
sid=conn.get_next_available_stream_id()
conn.send_headers(sid,[(':method','GET'),(':scheme','http'),(':authority','proxy-clear:8080'),(':path','/wait'),('x-delay-ms','1200')],end_stream=True)
sock.sendall(conn.data_to_send());print('ACTIVE',flush=True)
response={};ended=False;goaway=False;end=time.time()+5
while time.time()<end and not ended:
    data=sock.recv(65535)
    if not data:break
    for e in conn.receive_data(data):
        if isinstance(e,ResponseReceived) and e.stream_id==sid:response=dict(e.headers)
        elif isinstance(e,DataReceived) and e.stream_id==sid:conn.acknowledge_received_data(e.flow_controlled_length,e.stream_id)
        elif isinstance(e,ConnectionTerminated):goaway=True
        elif isinstance(e,StreamEnded) and e.stream_id==sid:ended=True
    sock.sendall(conn.data_to_send())
if mode=='term' and not goaway:
    sock.settimeout(.4)
    try:
        data=sock.recv(65535)
        if data:goaway=any(isinstance(e,ConnectionTerminated) for e in conn.receive_data(data))
    except socket.timeout:pass
sock.close()
assert ended and response.get(':status')=='200',(response,ended,goaway)
if mode=='term':assert goaway,'SIGTERM did not send GOAWAY'
print(json.dumps({'result':'PASS','status':response.get(':status'),'goaway':goaway}),flush=True)
