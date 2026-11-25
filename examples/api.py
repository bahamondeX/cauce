"""ASGI example: uvicorn api:app --app-dir examples --port 8000."""
import asyncio
import json

async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
    if scope['type'] == 'websocket':
        await send({'type': 'websocket.close', 'code': 1000})
        return  # proxy upgrades remain pending in increment 1
    if scope['path'] == '/sse':
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
        for n in range(5):
            await send({'type': 'http.response.body', 'body': f'data: {n}\n\n'.encode(), 'more_body': True})
            await asyncio.sleep(.25)
        await send({'type': 'http.response.body', 'body': b''})
        return
    payload = json.dumps({'server': 'uvicorn', 'path': scope['path']}).encode()
    await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(payload)).encode())]})
    await send({'type': 'http.response.body', 'body': payload})
