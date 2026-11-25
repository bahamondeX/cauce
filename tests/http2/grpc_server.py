import grpc
import time
from concurrent import futures
from pathlib import Path


def setup(context, name):
    probe = next((value for key, value in context.invocation_metadata() if key == 'x-probe'), '')
    context.send_initial_metadata((('backend-initial', 'grpc'), ('x-rpc-name', name), ('x-probe-copy', probe)))
    context.set_trailing_metadata((('backend-trailer', 'complete'), ('x-trailer-source', 'fixture')))


def unary(request, context):
    setup(context, 'unary')
    if request == b'large':
        return b'x' * (4 * 1024 * 1024)
    if request == b'fail':
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, 'fixture failure')
    if request == b'slow':
        for _ in range(100):
            if not context.is_active(): return b''
            time.sleep(.02)
    return b'echo:' + request


def client_stream(request_iterator, context):
    setup(context, 'client-stream')
    return b'|'.join(request_iterator)


def server_stream(request, context):
    setup(context, 'server-stream')
    for i in range(3):
        if not context.is_active(): return
        yield request + b':' + str(i).encode()


def bidi(request_iterator, context):
    setup(context, 'bidi')
    for request in request_iterator:
        if not context.is_active(): return
        yield b'bidi:' + request


def no_headers(request, context):
    # Hold an RPC open without initial metadata to exercise trailers-only timeout handling.
    for _ in range(100):
        if not context.is_active(): return b''
        time.sleep(.02)
    return b'late'


def server(secure=False):
    s = grpc.server(futures.ThreadPoolExecutor(max_workers=64), options=[('grpc.max_concurrent_streams', 4), ('grpc.max_send_message_length', 8*1024*1024), ('grpc.max_receive_message_length', 8*1024*1024)])
    handlers = {
      'Unary': grpc.unary_unary_rpc_method_handler(unary, request_deserializer=lambda x:x, response_serializer=lambda x:x),
      'ClientStream': grpc.stream_unary_rpc_method_handler(client_stream, request_deserializer=lambda x:x, response_serializer=lambda x:x),
      'ServerStream': grpc.unary_stream_rpc_method_handler(server_stream, request_deserializer=lambda x:x, response_serializer=lambda x:x),
      'Bidi': grpc.stream_stream_rpc_method_handler(bidi, request_deserializer=lambda x:x, response_serializer=lambda x:x),
      'NoHeaders': grpc.unary_unary_rpc_method_handler(no_headers, request_deserializer=lambda x:x, response_serializer=lambda x:x),
    }
    s.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('fixture.Echo', handlers),))
    if secure:
        p=Path('/tls'); creds=grpc.ssl_server_credentials(((p.joinpath('grpc.key').read_bytes(), p.joinpath('grpc.crt').read_bytes()),))
        s.add_secure_port('0.0.0.0:50052', creds)
    else: s.add_insecure_port('0.0.0.0:50051')
    s.start(); return s

servers=[server(), server(True)]
for s in servers: s.wait_for_termination()
