"""Small real WSS backend used by the Docker integration profile."""
import asyncio
import itertools
import json
import os
import ssl
import websockets


async def echo(ws):
    async for message in ws:
        await ws.send(message)


async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.environ['TLS_CERT'], os.environ['TLS_KEY'])
    ws_server = await websockets.serve(
        echo, '0.0.0.0', 8765, ssl=ctx,
        max_size=8 * 1024 * 1024, max_queue=16,
        ping_interval=5, ping_timeout=5,
    )
    plain_ws_server = await websockets.serve(
        echo, '0.0.0.0', 8764, max_size=8 * 1024 * 1024,
        max_queue=16, ping_interval=5, ping_timeout=5,
    )
    ids = itertools.count(1)

    async def http_client(reader, writer):
        connection_id = next(ids)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                method, path, _ = line.decode('ascii').strip().split(' ', 2)
                while await reader.readline() not in (b'\r\n', b'\n', b''):
                    pass
                body = json.dumps({'method': method, 'path': path,
                                   'connection_id': connection_id}).encode()
                writer.write(b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n'
                             + f'content-length: {len(body)}\r\nconnection: keep-alive\r\n\r\n'.encode()
                             + body)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    http_server = await asyncio.start_server(http_client, '0.0.0.0', 8766, ssl=ctx)
    try:
        await asyncio.gather(ws_server.wait_closed(), plain_ws_server.wait_closed(), http_server.serve_forever())
    finally:
        http_server.close()
        await http_server.wait_closed()
        plain_ws_server.close()
        await plain_ws_server.wait_closed()


asyncio.run(main())
