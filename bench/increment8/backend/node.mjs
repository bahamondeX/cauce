import http2 from 'node:http2';
import fs from 'node:fs';
import http from 'node:http';

const state = { active: 0, peak_active: 0, requests: 0, completed: 0, cancelled: 0,
  connections: 0, peak_connections: 0, new_connections: 0, connection_rows: {} };
let nextConnection = 0;
const ids = new WeakMap();

function addConnection(session) {
  if (ids.has(session)) return ids.get(session);
  const id = `h2-${++nextConnection}`;
  ids.set(session, id);
  state.connections++;
  state.new_connections++;
  state.peak_connections = Math.max(state.peak_connections, state.connections);
  state.connection_rows[id] = { active: 0, peak_streams: 0, requests: 0,
    tls: Boolean(session.socket.encrypted), alpn: session.socket.alpnProtocol || 'h2c' };
  session.once('close', () => { state.connections = Math.max(0, state.connections - 1); });
  return id;
}

function handle(stream, headers) {
  const id = addConnection(stream.session);
  const row = state.connection_rows[id];
  state.requests++; row.requests++;
  state.active++; row.active++;
  state.peak_active = Math.max(state.peak_active, state.active);
  row.peak_streams = Math.max(row.peak_streams, row.active);
  let settled = false;
  const finish = (ok) => {
    if (settled) return;
    settled = true;
    state.active = Math.max(0, state.active - 1);
    row.active = Math.max(0, row.active - 1);
    if (ok) state.completed++;
  };
  stream.once('close', () => { if (!stream.closed || !stream.rstCode) { finish(true); } else { state.cancelled++; finish(false); } });
  stream.on('error', () => { if (!settled) { state.cancelled++; finish(false); } });
  const url = new URL(headers[':path'] || '/', 'http://backend.test');
  const size = Math.max(0, Math.min(16 * 1024 * 1024, Number(url.searchParams.get('size') || 1024)));
  const delay = Math.max(0, Math.min(30000, Number(url.searchParams.get('delay_ms') || headers['x-delay-ms'] || 0)));
  const body = Buffer.alloc(size, 0x78);
  const respond = () => {
    if (stream.destroyed) return;
    stream.respond({ ':status': 200, 'content-type': 'application/octet-stream', 'content-length': String(body.length),
      'x-backend-connection': id, 'x-backend-connection-active': String(row.active), 'x-backend-protocol': 'h2' });
    stream.end(body, () => finish(true));
  };
  if (delay) setTimeout(respond, delay); else respond();
}

function snapshot() {
  return { ...state, connection_rows: Object.fromEntries(Object.entries(state.connection_rows).map(([k,v]) => [k, {...v}])) };
}
function reset(res) {
  if (state.active !== 0) { res.writeHead(409); res.end(JSON.stringify({ error: 'active requests' })); return; }
  state.requests = 0; state.completed = 0; state.cancelled = 0;
  state.peak_active = 0; state.peak_connections = state.connections; state.new_connections = 0;
  state.connection_rows = Object.fromEntries(Object.entries(state.connection_rows).map(([k,v]) => [k, {...v, active:0, peak_streams:0, requests:0}]));
  res.writeHead(200, {'content-type':'application/json'}); res.end(JSON.stringify(snapshot()));
}
const bind = process.env.BENCH_BIND || '0.0.0.0';
http.createServer((req,res) => {
  if (req.url === '/stats') { res.writeHead(200, {'content-type':'application/json'}); res.end(JSON.stringify(snapshot())); }
  else if (req.url === '/reset') reset(res);
  else { res.writeHead(404); res.end(); }
}).listen(Number(process.env.BENCH_STATS_PORT || 9000), bind);

http2.createServer({ settings: { maxConcurrentStreams: 16 } })
  .on('session', addConnection).on('stream', handle).listen(Number(process.env.PORT || 8000), bind);
if (process.env.BENCH_H2_TLS !== '0') {
  http2.createSecureServer({ key: fs.readFileSync('/certs/backend.key'), cert: fs.readFileSync('/certs/backend.crt'),
    allowHTTP1: false, settings: { maxConcurrentStreams: 16 } })
    .on('session', addConnection).on('stream', handle).listen(8443, bind);
}
