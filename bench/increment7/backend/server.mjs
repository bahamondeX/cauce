import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import crypto from 'node:crypto';

const state = { requests: 0, active: 0, peak_active: 0, new_connections: 0, reused_requests: 0,
  connections: 0, peak_connections: 0, identities: {}, run_id: 'startup', warm_connections: [],
  tls_handshakes: 0, tls_full_handshakes: 0, tls_resumed_handshakes: 0 };
let nextConnection = 1;
const openConnections = new Set();
function record(req, res) {
  state.requests++;
  state.active++;
  state.peak_active = Math.max(state.peak_active, state.active);
  const id = req.socket.__identity;
  const seen=state.identities[id]||0;
  if (seen>0 || state.warm_connections.includes(id)) state.reused_requests++;
  state.identities[id]=seen+1;
  let ended = false;
  const done = () => { if (!ended) { ended = true; state.active--; } };
  res.once('finish', done); res.once('close', done);
}
function handler(req, res) {
  if (req.url.startsWith('/__stats')) { res.writeHead(200, {'content-type':'application/json'}); res.end(JSON.stringify(state)); return; }
  if (req.url.startsWith('/__reset')) {
    state.requests=0; state.active=0; state.peak_active=0; state.new_connections=0; state.reused_requests=0;
    state.identities={}; state.run_id=new URL(req.url,'http://backend').searchParams.get('run') || 'reset';
    res.writeHead(200); res.end('ok'); return;
  }
  record(req,res);
  const u = new URL(req.url, 'http://backend');
  if (u.searchParams.get('cold') === '1') res.shouldKeepAlive = false;
  const delay = Math.min(10000, Math.max(0, Number(u.searchParams.get('ms') || 0)));
  const send = () => {
    if (req.url.startsWith('/sse')) {
      res.writeHead(200, {'content-type':'text/event-stream','cache-control':'no-cache'});
      let n=0; const timer=setInterval(()=>{res.write(`data: ${n++}\n\n`); if(n>=5){clearInterval(timer);res.end();}},100);
      res.once('close',()=>clearInterval(timer)); return;
    }
    if (req.url.startsWith('/blob')) {
      const bytes = Math.min(16*1024*1024, Math.max(0, Number(u.searchParams.get('bytes') || 1024)));
      res.writeHead(200, {'content-type':'application/octet-stream','content-length':String(bytes)});
      const chunk=Buffer.alloc(64*1024, 0x5a); let left=bytes;
      const write=()=>{ while(left>0){const part=chunk.subarray(0,Math.min(left,chunk.length));left-=part.length;if(!res.write(part)) return res.once('drain',write);} res.end();}; write(); return;
    }
    const body=Buffer.alloc(1024,0x78);
    res.writeHead(200, {'content-type':'application/octet-stream','content-length':String(body.length)}); res.end(body);
  };
  if (delay) setTimeout(send,delay); else send();
}
function websocket(req, socket) {
  state.requests++; state.active++; state.peak_active=Math.max(state.peak_active,state.active);
  const id=req.socket.__identity; const seen=state.identities[id]||0;
  if(seen>0 || state.warm_connections.includes(id)) state.reused_requests++;
  state.identities[id]=seen+1;
  const key=req.headers['sec-websocket-key'];
  if(!key || req.headers.upgrade?.toLowerCase()!=='websocket') { socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');state.active--;return; }
  const accept=crypto.createHash('sha1').update(key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let pending=Buffer.alloc(0);
  socket.on('data',chunk=>{
    pending=Buffer.concat([pending,chunk]);
    while(pending.length>=2){const opcode=pending[0]&0x0f;let len=pending[1]&0x7f;let offset=2;
      if(len===126){if(pending.length<4)return;len=pending.readUInt16BE(2);offset=4;}
      else if(len===127){if(pending.length<10)return;const wide=pending.readBigUInt64BE(2);if(wide>1048576n){socket.destroy();return;}len=Number(wide);offset=10;}
      const masked=(pending[1]&0x80)!==0;if(masked)offset+=4;if(pending.length<offset+len)return;
      let payload=pending.subarray(offset,offset+len);if(masked){const mask=pending.subarray(offset-4,offset);payload=Buffer.from(payload);for(let i=0;i<payload.length;i++)payload[i]^=mask[i%4];}
      pending=pending.subarray(offset+len);
      if(opcode===8){socket.end(Buffer.from([0x88,0]));return;}
      if(opcode===9){socket.write(Buffer.concat([Buffer.from([0x8a,payload.length]),payload]));continue;}
      if(opcode===1||opcode===2){if(payload.length<126)socket.write(Buffer.concat([Buffer.from([0x80|opcode,payload.length]),payload]));else if(payload.length<=65535){const h=Buffer.alloc(4);h[0]=0x80|opcode;h[1]=126;h.writeUInt16BE(payload.length,2);socket.write(Buffer.concat([h,payload]));}else socket.destroy();}
    }
  });
  socket.once('close',()=>{state.active=Math.max(0,state.active-1);});
}
const server=http.createServer(handler);
server.keepAliveTimeout=15000; server.headersTimeout=16000;
function opened(s) { s.__identity=nextConnection++;openConnections.add(s.__identity);state.new_connections++;state.connections++;state.peak_connections=Math.max(state.peak_connections,state.connections);s.once('close',()=>{openConnections.delete(s.__identity);state.connections--;}); }
server.on('connection',opened);
server.on('upgrade',(req,socket)=>websocket(req,socket));
const port=Number(process.env.PORT||8000);
const bind=process.env.BENCH_BIND || '0.0.0.0';
if (process.env.TLS==='1') {
  server.listen(port,bind);
  const tlsServer=https.createServer({key:fs.readFileSync('/certs/backend.key'),cert:fs.readFileSync('/certs/backend.crt'),
    minVersion:'TLSv1.2',maxVersion:'TLSv1.2',secureOptions:crypto.constants.SSL_OP_NO_TICKET},handler);
  tlsServer.keepAliveTimeout=15000; tlsServer.headersTimeout=16000;
  tlsServer.on('resumeSession',(_id,cb)=>cb(null,null));
  tlsServer.on('secureConnection',s=>{state.tls_handshakes++;if(s.isSessionReused())state.tls_resumed_handshakes++;else state.tls_full_handshakes++;opened(s);});
  tlsServer.on('upgrade',(req,socket)=>websocket(req,socket));
  tlsServer.listen(8443,bind);
} else server.listen(port,bind);
const statsServer=http.createServer((req,res)=>{
  if(req.url.startsWith('/reset')) {
    if(state.active!==0){res.writeHead(409);res.end('cannot reset counters while requests are active');return;}
    state.requests=0; state.peak_active=0; state.new_connections=0; state.reused_requests=0;
    state.tls_handshakes=0;state.tls_full_handshakes=0;state.tls_resumed_handshakes=0;
    state.identities={}; state.warm_connections=[...openConnections]; state.peak_connections=state.connections;
    state.run_id=new URL(req.url,'http://stats').searchParams.get('run')||'reset'; res.writeHead(200);res.end('ok');return;
  }
  res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(state));
});
statsServer.listen(Number(process.env.BENCH_STATS_PORT || 9000),bind);
process.on('SIGTERM',()=>{server.close(()=>process.exit(0));});
