import http2 from 'node:http2';
import http from 'node:http';
import fs from 'node:fs';
import crypto from 'node:crypto';

let next = 0;
const sessions = new WeakMap();
const activity = new WeakMap();
function handle(stream, headers) {
  const session = stream.session;
  if (!sessions.has(session)) sessions.set(session, `up-${++next}`);
  if (!activity.has(session)) activity.set(session,{active:0,peak:0});
  const counters=activity.get(session); counters.active++; counters.peak=Math.max(counters.peak,counters.active);
  stream.once('close',()=>{counters.active=Math.max(0,counters.active-1)});
  const id = sessions.get(session);
  const path = headers[':path'] || '/';
  let requestTrailer='';
  stream.on('trailers',t=>{requestTrailer=t['x-request-trailer']||'';});
  stream.on('error', () => {});
  if (path === '/goaway') {
    stream.respond({ ':status': 200, 'x-backend-session': id });
    stream.end('goaway');
    session.goaway(http2.constants.NGHTTP2_NO_ERROR, stream.id);
    return;
  }
  if (path === '/lifecycle') {
    stream.respond({ ':status': 200, 'x-backend-session': id });
    setTimeout(() => { if (!stream.destroyed) stream.end('lifecycle-body'); }, 1200);
    return;
  }
  if (path.endsWith('/wait')) {
    const ms = Math.min(Number(headers['x-delay-ms'] || 500), 5000);
    setTimeout(() => { if (!stream.destroyed) { stream.respond({ ':status': 200, 'x-backend-session': id, 'x-backend-initial': 'yes', 'x-backend-peak': String(counters.peak) }); stream.end(JSON.stringify({session:id,peak:counters.peak})); } }, ms);
    return;
  }
  let chunks = [];
  stream.on('data', b => chunks.push(b));
  stream.on('end', () => {
    if (stream.destroyed) return;
    const body = Buffer.concat(chunks);
    const responseHeaders={ ':status': 200, 'content-type': 'application/json', 'x-backend-session': id,
      'x-backend-initial': 'yes', 'x-backend-peak': String(counters.peak), 'x-request-copy': headers['x-fixture-request'] || '', 'x-request-trailer-copy': requestTrailer };
    const responseBody=JSON.stringify({ path, body: body.toString('base64'), session: id,
      protocol: session.socket.alpnProtocol || 'h2c', requestId: crypto.randomUUID() });
    // grpcio's HTTP/2 service below supplies real response trailers; keep this
    // ordinary Node endpoint focused on request-trailer forwarding. Node 24's
    // shared stream trailer event path is not reliable for bidirectional tests.
    stream.respond(responseHeaders); stream.end(responseBody);
  });
}
const clear = http2.createServer({ settings: { maxConcurrentStreams: 4 } });
clear.on('stream', handle);
clear.listen(7001, '0.0.0.0');
const clear2 = http2.createServer({ settings: { maxConcurrentStreams: 4 } });
clear2.on('stream', handle); clear2.listen(7004,'0.0.0.0');
const early = http2.createServer({ settings: { maxConcurrentStreams: 4, initialWindowSize: 0 } });
early.on('stream',(stream,headers)=>{stream.on('error',()=>{});if((headers[':path']||'').startsWith('/early/')){stream.respond({':status':413,'content-type':'text/plain'});stream.end('early-response');return;}handle(stream,headers);});early.listen(7006,'0.0.0.0');
const earlyTls = http2.createSecureServer({key:fs.readFileSync('/tls/backend.key'),cert:fs.readFileSync('/tls/backend.crt'),settings:{maxConcurrentStreams:4,initialWindowSize:0}});
earlyTls.on('stream',(stream,headers)=>{stream.on('error',()=>{});if((headers[':path']||'').startsWith('/early/')){stream.respond({':status':413,'content-type':'text/plain'});stream.end('early-response');return;}handle(stream,headers);});earlyTls.listen(7007,'0.0.0.0');
const secure = http2.createSecureServer({ key: fs.readFileSync('/tls/backend.key'), cert: fs.readFileSync('/tls/backend.crt'),
  allowHTTP1: true, settings: { maxConcurrentStreams: 4 } });
secure.on('stream', handle);
secure.on('request', (req, res) => { if (req.httpVersionMajor === 1) { res.setHeader('x-http1-backend', 'yes'); res.end('translated-http1'); } });
secure.listen(7002, '0.0.0.0');
const secureH2 = http2.createSecureServer({ key: fs.readFileSync('/tls/backend.key'), cert: fs.readFileSync('/tls/backend.crt'),
  settings: { maxConcurrentStreams: 4 } });
secureH2.on('stream', handle);
secureH2.listen(7008, '0.0.0.0');
http.createServer((req, res) => {
  if (req.url === '/drain') { clear.close(); clear2.close(); early.close(); earlyTls.close(); secure.close(); res.end('draining'); return; }
  res.end('ok');
}).listen(7003, '0.0.0.0');
http.createServer((req,res)=>{res.setHeader('x-http1-backend','yes');res.end('translated-http1:'+req.url)}).listen(7005,'0.0.0.0');
