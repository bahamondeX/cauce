"""Minimal H2 peer that ends only its response half and never consumes uploads."""
import socket
import ssl
import threading

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import RequestReceived, StreamReset
from h2.settings import SettingCodes, Settings


def serve_connection(sock):
    conn = H2Connection(config=H2Configuration(client_side=False, header_encoding="utf-8"))
    conn.local_settings = Settings(client=False, initial_values={
        SettingCodes.MAX_CONCURRENT_STREAMS: 4,
        SettingCodes.INITIAL_WINDOW_SIZE: 0,
    })
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    try:
        while True:
            data = sock.recv(65535)
            if not data:
                return
            for event in conn.receive_data(data):
                if isinstance(event, RequestReceived):
                    conn.send_headers(event.stream_id, [
                        (":status", "413"), ("content-type", "text/plain"),
                        ("content-length", "14"),
                    ])
                    conn.send_data(event.stream_id, b"early-response", end_stream=True)
                elif isinstance(event, StreamReset):
                    pass
            pending = conn.data_to_send()
            if pending:
                sock.sendall(pending)
    except (OSError, ssl.SSLError):
        return
    finally:
        sock.close()


def serve(port, tls=False):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen(128)
    context = None
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain("/tls/backend.crt", "/tls/backend.key")
        context.set_alpn_protocols(["h2"])
    while True:
        client, _ = listener.accept()
        if context:
            try:
                client = context.wrap_socket(client, server_side=True)
            except ssl.SSLError:
                client.close()
                continue
        threading.Thread(target=serve_connection, args=(client,), daemon=True).start()


threading.Thread(target=serve, args=(7009,), daemon=True).start()
serve(7010, tls=True)
