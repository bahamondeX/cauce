"""Independent aioquic request driver shared with the diagnostic runner."""
import asyncio

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.events import ConnectionTerminated, StreamReset

class Client(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http = H3Connection(self._quic)
        self.inbox = {}
        self.closed_event = None

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self.closed_event = event
            for queue in self.inbox.values():
                queue.put_nowait(event)
        if isinstance(event, StreamReset) and event.stream_id in self.inbox:
            self.inbox[event.stream_id].put_nowait(event)
        for message in self.http.handle_event(event):
            if isinstance(message, (HeadersReceived, DataReceived)) and message.stream_id in self.inbox:
                self.inbox[message.stream_id].put_nowait(message)

    def stream(self):
        stream = self._quic.get_next_available_stream_id()
        self.inbox[stream] = asyncio.Queue()
        return stream

    def request(self, path, method=b"GET", end=True, headers=(), authority=b"route.test"):
        stream = self.stream()
        self.http.send_headers(stream, [
            (b":method", method), (b":scheme", b"https"), (b":authority", authority),
            (b":path", path.encode()), *headers,
        ], end_stream=end)
        self.transmit()
        return stream

    def data(self, stream, data, end=False):
        self.http.send_data(stream, data, end_stream=end)
        self.transmit()

    def trailers(self, stream, headers):
        self.http.send_headers(stream, headers, end_stream=True)
        self.transmit()

    def cancel(self, stream):
        self._quic.stop_stream(stream, 0x10c)
        self.transmit()

    async def event(self, stream):
        return await asyncio.wait_for(self.inbox[stream].get(), 3)

    async def response(self, stream):
        headers, data = [], bytearray()
        while True:
            event = await self.event(stream)
            if isinstance(event, (StreamReset, ConnectionTerminated)):
                raise ConnectionError(str(event))
            if isinstance(event, HeadersReceived):
                headers += event.headers
            elif isinstance(event, DataReceived):
                data += event.data
            if event.stream_ended:
                self.inbox.pop(stream)
                return dict(headers), bytes(data)
