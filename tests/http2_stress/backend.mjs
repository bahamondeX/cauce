import http2 from 'node:http2';
import http from 'node:http';
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';

// Deliberately observable test backend. Statistics are out-of-band on :7003.
const state = { activeConnections: 0, peakConnections: 0, activeStreams: 0, peakStreams: 0, totalConnections: 0, totalStreams: 0, earlyResponses: 0 };
const servers = [];
function serve(server, label, early = false) {
  server.on('session', session => {
    state.activeConnections++; state.totalConnections++;
    state.peakConnections = Math.max(state.peakConnections, state.activeConnections);
    session.once('close', () => state.activeConnections--);
  });
  server.on('stream', async (stream, headers) => {
    state.activeStreams++; state.totalStreams++;
    state.peakStreams = Math.max(state.peakStreams, state.activeStreams);
    // Expected RST_STREAM from cancellation/stress must not take down Node's fixture.
    stream.on('error', () => {});
    let closed = false;
    stream.once('close', () => { if (!closed) { closed = true; state.activeStreams--; } });
    const path = String(headers[':path'] || '/');
    try {
      if (early || path.startsWith('/early')) {
        state.earlyResponses++;
        stream.respond({ ':status': 413, 'content-type': 'text/plain' });
        stream.end('early');
      } else if (path.startsWith('/delay')) {
        const ms = Math.min(2500, Math.max(1, Number(new URL(path, 'http://x').searchParams.get('ms')) || 900));
        stream.respond({ ':status': 200, 'content-type': 'text/plain' });
        await delay(ms);
        if (!stream.destroyed) stream.end('ok');
      } else if (path.startsWith('/bulk')) {
        stream.respond({ ':status': 200, 'content-type': 'application/octet-stream' });
        const chunk = Buffer.alloc(16384, 0x61);
        const count = 64;
        for (let i = 0; i < count && !stream.destroyed; i++) {
          if (!stream.write(chunk)) await new Promise(resolve => {
            const done=()=>{stream.off('drain',done);stream.off('close',done);resolve();};
            stream.once('drain',done);stream.once('close',done);
          });
        }
        if (!stream.destroyed) stream.end();
      } else {
        stream.respond({ ':status': 200, 'content-type': 'text/plain', 'x-backend': label });
        stream.end('ok');
      }
    } catch { /* client cancellation is expected in stress cases */ }
  });
  servers.push(server);
}

const clear = http2.createServer({ settings: { initialWindowSize: 65535, maxConcurrentStreams: 100 } });
serve(clear, 'h2c'); clear.listen(7001, '0.0.0.0');
const tls = http2.createSecureServer({
  key: fs.readFileSync('/tls/backend.key'), cert: fs.readFileSync('/tls/backend.crt'), allowHTTP1: false,
  settings: { initialWindowSize: 65535, maxConcurrentStreams: 100 },
});
serve(tls, 'h2-tls'); tls.listen(7002, '0.0.0.0');
// Upstream with zero initial stream window which responds before upload completes.
const zero = http2.createServer({ settings: { initialWindowSize: 0, maxConcurrentStreams: 100 } });
serve(zero, 'zero-window-early', true); zero.listen(7009, '0.0.0.0');
const stats = http.createServer((req, res) => {
  if (req.url === '/reset') {
    state.peakConnections = state.activeConnections; state.peakStreams = state.activeStreams;
    state.totalConnections = 0; state.totalStreams = 0; state.earlyResponses = 0;
    res.end(JSON.stringify({ reset: true })); return;
  }
  res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(state));
});
stats.listen(7003, '0.0.0.0');
process.on('SIGTERM', () => Promise.all(servers.map(s => new Promise(r => s.close(r)))).finally(() => process.exit(0)));
