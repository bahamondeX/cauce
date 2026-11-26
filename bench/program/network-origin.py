#!/usr/bin/env python3
"""Loopback TCP half-close and UDP datagram echo origins for the network runner."""
import argparse
import asyncio
import socket


async def echo(reader, writer):
    try:
        while data := await reader.read(16 * 1024):
            writer.write(data)
            await writer.drain()
        writer.write_eof()
        await writer.drain()
    except (OSError, ConnectionError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def run(protocol, port):
    if protocol == "tcp":
        async with await asyncio.start_server(echo, "127.0.0.1", port) as server:
            print("ready", flush=True)
            await server.serve_forever()
    else:
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", port))
            sock.setblocking(False)
            print("ready", flush=True)
            while True:
                data, peer = await loop.sock_recvfrom(sock, 65_535)
                await loop.sock_sendto(sock, data, peer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", choices=["tcp", "udp"])
    parser.add_argument("port", type=int)
    args = parser.parse_args()
    asyncio.run(run(args.protocol, args.port))
