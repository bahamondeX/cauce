// Protocol and resource admission gate. Never used for latency comparison.
import http2 from 'node:http2';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const [url, mode, statsURL] = process.argv.slice(2);
const secure = url.startsWith('https:');
const sessions=[];
const nap=ms=>new Promise(r=>setTimeout(r,ms));
async function connection() {
  const s=http2.connect(url, secure?{ca:fs.readFileSync('/certs/ca.crt')}:{});
  s.on('error',()=>{});
  await new Promise((resolve,reject)=>{s.once('remoteSettings',resolve);s.once('error',reject)});
  assert.equal(s.remoteSettings.maxConcurrentStreams,16);
  if(secure) assert.equal(s.socket.alpnProtocol,'h2');
  sessions.push(s);return s;
}
function request(s, delay=0) {
  return new Promise((resolve,reject)=>{
    const headers={':method':mode==='grpc'?'POST':'GET',':path':mode==='grpc'?'/fixture.Echo/Unary':`/bytes?size=1024&delay_ms=${delay}`};
    if(mode==='grpc') Object.assign(headers,{'content-type':'application/grpc','te':'trailers','x-delay-ms':String(delay)});
    const req=s.request(headers);let status,trailers={},chunks=[];
    req.on('response',h=>{status=h[':status'];if(h['grpc-status']!==undefined)trailers=h});
    req.on('trailers',h=>trailers=h);req.on('data',c=>chunks.push(c));req.on('error',reject);
    req.setTimeout(6000,()=>{req.close();reject(Error('gate timeout'))});
    req.on('end',()=>resolve({status,grpc_status:trailers['grpc-status'],grpc_message:trailers['grpc-message'],bytes:Buffer.concat(chunks).length}));
    if(mode==='grpc'){const b=Buffer.from('bench'),h=Buffer.alloc(5);h.writeUInt32BE(b.length,1);req.end(Buffer.concat([h,b]))}else req.end();
  });
}
const read=async path=>{const r=await fetch(statsURL+path);assert(r.ok);return r.json()};
async function waitActive(n) {let v;for(let i=0;i<100;i++){v=await read('/stats');if(v.active===n)return v;await nap(10)}throw Error(`active expected ${n}: ${JSON.stringify(v)}`)}
try {
  const a=await connection();const smoke=await request(a);assert.equal(smoke.status,200,JSON.stringify(smoke));if(mode==='grpc')assert.equal(smoke.grpc_status,'0',JSON.stringify(smoke));await read('/reset');
  const first=Array.from({length:17},()=>request(a,650));
  const oneDuring=await waitActive(16);assert.equal(oneDuring.requests,16);
  const oneResults=await Promise.all(first);assert(oneResults.every(x=>x.status===200));
  const oneAfter=await waitActive(0);assert.equal(oneAfter.peak_active,16);assert.equal(oneAfter.requests,17);
  await read('/reset');const b=await connection(),c=await connection();
  const held=[...Array.from({length:16},()=>request(a,2200)),...Array.from({length:16},()=>request(b,2200))];
  const during=await waitActive(32);const extra=await request(c);
  assert.equal(extra.status,503,JSON.stringify(extra));assert.equal(during.peak_active,32);
  const results=await Promise.all(held);assert(results.every(x=>x.status===200));
  if(mode==='grpc')assert(results.every(x=>x.grpc_status==='0'),JSON.stringify(results));
  const after=await waitActive(0);assert.equal(after.requests,32);assert.equal(after.peak_active,32);
  if(after.peak_connections!==undefined)assert(after.peak_connections<=32);
  const recovered=await request(a);assert.equal(recovered.status,200);await waitActive(0);
  console.log(JSON.stringify({status:'PASS',url,mode,settings_max_streams:16,one_connection:{during:oneDuring,after:oneAfter,completed:oneResults.length},global:{during,after,excess:extra,completed:results.length},recovered}));
}finally{for(const s of sessions)s.destroy()}
