#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import http2 from 'node:http2';
import { performance } from 'node:perf_hooks';
import { setTimeout as sleep } from 'node:timers/promises';

const METHODS = ['Unary', 'ClientStream', 'ServerStream', 'Bidi'];
const OPEN_SESSIONS=[];
function parseArgs(argv) {
  const a = { mode: 'http2', method: 'Unary', connections: 2, streams: 16, rate: 100, seconds: 10,
    warmup: 2, timeout: 5000, path: '/bytes?size=1024&delay_ms=0', size: undefined, queue: undefined, allow503:false };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (!k.startsWith('--')) throw new Error(`unexpected argument ${k}`);
    const key = k.slice(2);
    const v = argv[++i]; if (v === undefined) throw new Error(`missing value for --${key}`);
    const names = { url:'url', mode:'mode', method:'method', connections:'connections', streams:'streams', rate:'rate',
      seconds:'seconds', warmup:'warmup', timeout:'timeout', path:'path', size:'size', ca:'ca', out:'out', queue:'queue', barrier:'barrierDir', 'allow-503':'allow503' };
    if (!names[key]) throw new Error(`unknown option --${key}`);
    const n = names[key]; a[n] = ['connections','streams','size','queue'].includes(key) ? Number(v) :
      ['rate','seconds','warmup','timeout'].includes(key) ? Number(v) : v;
    if(key==='allow-503') { if(!['true','false'].includes(v)) throw new Error('--allow-503 must be true or false'); a.allow503=v==='true'; }
  }
  if (!a.url || !a.out) throw new Error('--url and --out are required');
  const u = new URL(a.url); if (!['http:','https:'].includes(u.protocol)) throw new Error('URL must use http or https');
  if (!['http2','grpc'].includes(a.mode)) throw new Error('--mode must be http2 or grpc');
  if (a.mode === 'grpc' && !METHODS.includes(a.method)) throw new Error(`--method must be one of ${METHODS.join(', ')}`);
  if (!(a.connections >= 1 && a.connections <= 64 && a.streams >= 1 && a.streams <= 1024)) throw new Error('connections/streams out of range');
  if (!(a.rate > 0 && a.rate <= 100000 && a.seconds > 0 && a.seconds <= 3600 && a.warmup >= 0 && a.warmup <= 3600 && a.timeout > 0)) throw new Error('invalid rate, duration, warmup, or timeout');
  const total = Math.floor(a.rate * a.seconds); if (total < 1 || total > 100000) throw new Error('offered requests must be in [1,100000]');
  if (a.queue === undefined) a.queue = a.connections * a.streams * 4;
  if (!(a.queue >= 1 && a.queue <= 65536)) throw new Error('queue must be in [1,65536]');
  if (a.size !== undefined && (!(a.size >= 0) || a.size > 16 * 1024 * 1024)) throw new Error('--size must be 0..16777216');
  return a;
}

function frame(payload) { const h = Buffer.alloc(5); h.writeUInt32BE(payload.length, 1); return Buffer.concat([h, payload]); }
function parseFrames(buf) {
  const messages=[]; let off=0;
  while (off < buf.length) {
    if (buf.length-off < 5) throw new Error('truncated_grpc_frame_header');
    const compressed=buf[off]; if (compressed !== 0) throw new Error('compressed_grpc_frame_unsupported');
    const len=buf.readUInt32BE(off+1); if (buf.length-off-5 < len) throw new Error('truncated_grpc_frame_payload');
    messages.push(buf.subarray(off+5,off+5+len)); off += 5+len;
  }
  return messages;
}
function parseGrpcResponse(httpStatus, headers, trailers, body) {
  if (Number(httpStatus) !== 200) return { kind:'http_error', error:`http_status_${httpStatus}` };
  if (!String(headers['content-type']||'').startsWith('application/grpc')) return { kind:'grpc_error', error:'invalid_grpc_content_type' };
  const status=String(trailers['grpc-status']??headers['grpc-status']??'');
  if (status!=='0') return { kind:'grpc_error', error:`grpc_status_${status||'missing'}:${trailers['grpc-message']||headers['grpc-message']||''}` };
  return { kind:'ok', messages:parseFrames(body) };
}
function expectedGrpc(method) {
  if (method==='Unary') return [Buffer.from('echo:bench')];
  if (method==='ClientStream') return [Buffer.from('bench0|bench1|bench2')];
  if (method==='ServerStream') return [0,1,2].map(i=>Buffer.from(`bench:${i}`));
  return [0,1,2].map(i=>Buffer.from(`bidi:bench${i}`));
}
function percentile(nums,p) { if (!nums.length) return null; const s=[...nums].sort((x,y)=>x-y); return s[Math.max(0,Math.ceil(p*s.length)-1)]; }
function quantiles(nums) { return {p50:percentile(nums,.5),p95:percentile(nums,.95),p99:percentile(nums,.99),max:nums.length?Math.max(...nums):null}; }
const now=()=>performance.now();

function waitForFile(file, timeout=120000) {
  return new Promise((resolve,reject)=>{ const start=Date.now(); const tick=()=>fs.existsSync(file)?resolve():Date.now()-start>timeout?reject(new Error(`barrier_timeout:${file}`)):setTimeout(tick,25); tick(); });
}
function touch(file) { fs.mkdirSync(path.dirname(file),{recursive:true}); fs.writeFileSync(file,`${new Date().toISOString()}\n`); }

async function main() {
  const a=parseArgs(process.argv.slice(2)); const url=new URL(a.url); const ca=a.ca?fs.readFileSync(a.ca):undefined;
  const sessions=[]; let next=0;
  for(let i=0;i<a.connections;i++) {
    const s=http2.connect(url.origin,{ ...(ca?{ca}:{}), ...(url.protocol==='https:'?{rejectUnauthorized:true,ALPNProtocols:['h2']}:{}), maxSessionMemory:32 });
    const rec={id:i,session:s,inflight:0,peak:0,settings:null,alpn:null,closed:false};
    s.on('remoteSettings',x=>{rec.settings=x;}); s.on('connect',()=>{rec.alpn=s.socket?.alpnProtocol||null;});
    s.on('error',e=>{rec.lastError=e.message;}); s.on('close',()=>{rec.closed=true;}); sessions.push(rec); OPEN_SESSIONS.push(s);
  }
  await Promise.all(sessions.map(r=>new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(new Error(`connect_timeout:${r.id}`)),a.timeout);r.session.once('connect',()=>{clearTimeout(t);resolve();});r.session.once('error',e=>{clearTimeout(t);reject(e);});})));
  await Promise.all(sessions.map(r=>r.settings?Promise.resolve():new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(new Error(`settings_timeout:${r.id}`)),a.timeout);r.session.once('remoteSettings',()=>{clearTimeout(t);resolve();});})));
  if(url.protocol==='https:' && sessions.some(r=>r.alpn!=='h2')) throw new Error(`TLS did not negotiate h2: ${sessions.map(r=>r.alpn).join(',')}`);
  const outdir=path.dirname(a.out); fs.mkdirSync(outdir,{recursive:true}); const barrier=a.barrierDir;
  if(barrier) touch(path.join(barrier,'ready'));
  let warmupResult={offered:0,rejected:0,dropped:0,rows:[]};
  if(a.warmup>0) warmupResult=await runPeriod(a,a.warmup,sessions,[],true);
  if(barrier) { touch(path.join(barrier,'warmup.done')); await waitForFile(path.join(barrier,'resume')); }
  for(const r of sessions) { if(!r.settings) throw new Error(`missing_remote_settings:${r.id}`); if(r.settings.maxConcurrentStreams!==a.streams) throw new Error(`remote_max_concurrent_streams_${r.settings.maxConcurrentStreams}_expected_${a.streams}`); r.peak=0;r.requests=0; }
  const rows=[]; const measureStart=now(); const outcome=await runPeriod(a,a.seconds,sessions,rows,false); const measureEnd=now();
  if(barrier) { touch(path.join(barrier,'measurement.done')); await waitForFile(path.join(barrier,'release')); }
  const allLat=rows.map(r=>r.arrival_latency_ms).filter(Number.isFinite), allService=rows.map(r=>r.service_latency_ms).filter(Number.isFinite);
  const okRows=rows.filter(r=>r.outcome==='ok'), okLat=okRows.map(r=>r.arrival_latency_ms), okService=okRows.map(r=>r.service_latency_ms), ttfb=okRows.map(r=>r.ttfb_ms).filter(Number.isFinite);
  const okCount=okRows.length, errors=rows.filter(r=>r.outcome==='error'||r.outcome==='http_error'||r.outcome==='grpc_error').length;
  const grpcStatusCounts=rows.filter(r=>r.grpc_status!==null&&r.grpc_status!==undefined).reduce((acc,r)=>(acc[r.grpc_status]=(acc[r.grpc_status]||0)+1,acc),{});
  const summary={schema:'cauce.increment8.h2load.v1',mode:a.mode,method:a.mode==='grpc'?a.method:null,url:a.url,rate:a.rate,seconds:a.seconds,warmup_seconds:a.warmup,
    offered:outcome.offered,completed:rows.length,ok:okCount,errors,expected_overload_503:rows.filter(r=>r.outcome==='overload_expected').length,
    unexpected_http_503:rows.filter(r=>r.outcome==='http_error'&&r.status===503).length,rejected:outcome.rejected,dropped:outcome.dropped,
    measurement_elapsed_s:(measureEnd-measureStart)/1000,drain_elapsed_s:outcome.drain_elapsed_ms/1000,
    throughput_offered_window_per_s:okCount/Math.max(.001,a.seconds),throughput_actual_elapsed_per_s:okCount/Math.max(.001,(measureEnd-measureStart)/1000),
    arrival_latency_ms:{all:quantiles(allLat),success:quantiles(okLat)},service_latency_ms:{all:quantiles(allService),success:quantiles(okService)},ttfb_ms:quantiles(ttfb),
    scheduler_lag_ms:quantiles(rows.map(r=>r.scheduler_lag_ms).filter(Number.isFinite)),queue_wait_ms:quantiles(rows.map(r=>r.queue_wait_ms).filter(Number.isFinite)),
    grpc_status_counts:grpcStatusCounts,grpc_response_messages:rows.reduce((n,r)=>n+(r.grpc_message_count||0),0),
    grpc_backend_trailer_complete:rows.filter(r=>r.grpc_backend_trailer==='complete').length,
    sessions:sessions.map(r=>({id:r.id,requests:r.requests,peak_inflight:r.peak,remote_max_concurrent_streams:r.settings?.maxConcurrentStreams??null,alpn:r.alpn})),
    remote_max_concurrent_streams:sessions.map(r=>r.settings?.maxConcurrentStreams??null),negotiated_alpn:sessions.map(r=>r.alpn),
    warmup:{offered:warmupResult.offered,completed:warmupResult.rows.filter(r=>r.outcome==='ok').length,
      errors:warmupResult.rows.filter(r=>r.outcome!=='ok').length,rejected:warmupResult.rejected,dropped:warmupResult.dropped,
      outcomes:warmupResult.rows.reduce((acc,r)=>(acc[r.outcome]=(acc[r.outcome]||0)+1,acc),{})},
    connections:sessions.length,streams_per_connection:a.streams,protocol:url.protocol==='https:'?'TLS+HTTP/2':'h2c',out:a.out};
  const jsonl=a.out.endsWith('.jsonl')?a.out:`${a.out}.jsonl`; const sumfile=a.out.endsWith('.jsonl')?a.out.replace(/\.jsonl$/,'')+'.summary.json':`${a.out}.summary.json`;
  const raw=rows.map(r=>JSON.stringify(r)).join('\n')+(rows.length?'\n':'');
  fs.writeFileSync(jsonl,raw);
  fs.writeFileSync(sumfile,JSON.stringify(summary,null,2)+'\n');
  for(const r of sessions) r.session.close();
  OPEN_SESSIONS.length=0;
  console.log(JSON.stringify(summary));
  if(summary.errors||summary.dropped||summary.rejected||(!a.allow503&&summary.unexpected_http_503)) process.exitCode=2;
}

async function runPeriod(a, seconds, sessions, rows, warmup) {
  const total=Math.floor(a.rate*seconds), capacity=a.connections*a.streams, qmax=a.queue;
  const queue=[]; let offered=0,rejected=0,dropped=0,done=false,rr=0; const started=now();
  const results=[];
  async function worker() {
    while(!done||queue.length) {
      if(!queue.length){await sleep(1);continue;}
      const item=queue.shift(); const qstart=now();
      let chosen;
      for(let tries=0;tries<sessions.length;tries++){const r=sessions[(rr++)%sessions.length];if(r.inflight<a.streams){chosen=r;break;}}
      if(!chosen){await sleep(1);queue.unshift(item);continue;}
      const row=await execute(a,chosen,item,(qstart-item.queuedAt),warmup,started); results.push(row); if(rows)rows.push(row);
    }
  }
  const workers=Array.from({length:capacity},()=>worker());
  const step=1000/a.rate;
  for(let n=0;n<total;n++) {
    const planned=started+n*step;
    while(now()<planned) await sleep(Math.max(1,Math.ceil(planned-now())));
    const actual=now(), lag=actual-planned; offered++;
    const item={n,plannedAt:planned,actualAt:actual,lag,queuedAt:actual,periodStart:started};
    if(lag>250){dropped++;if(rows)rows.push({seq:n,scheduled_at_ms:planned-started,started_at_ms:null,ended_at_ms:actual-started,queue_wait_ms:null,service_latency_ms:null,arrival_latency_ms:actual-planned,ttfb_ms:null,status:null,bytes:0,session_id:null,stream_id:null,outcome:'dropped_scheduler_lag',scheduler_lag_ms:lag,error:null,warmup:false,grpc_status:null,grpc_message_count:null});continue;}
    if(queue.length>=qmax){rejected++;if(rows)rows.push({seq:n,scheduled_at_ms:planned-started,started_at_ms:null,ended_at_ms:actual-started,queue_wait_ms:null,service_latency_ms:null,arrival_latency_ms:actual-planned,ttfb_ms:null,status:null,bytes:0,session_id:null,stream_id:null,outcome:'rejected_queue_full',scheduler_lag_ms:lag,error:null,warmup:false,grpc_status:null,grpc_message_count:null});continue;}
    queue.push(item);
  }
  const offeredEnd=started+seconds*1000; if(offeredEnd>now())await sleep(offeredEnd-now());
  done=true; await Promise.all(workers);
  const finished=now(); return {offered,rejected,dropped,capacity,elapsed_ms:finished-started,drain_elapsed_ms:Math.max(0,finished-offeredEnd),rows:rows||results};
}

async function execute(a,conn,item,queueWait,warmup,periodStart=0) {
  const begin=now(); const row={seq:item.n,scheduled_at_ms:item.plannedAt-periodStart,started_at_ms:begin-periodStart,ended_at_ms:null,
    session_id:conn.id,stream_id:null,outcome:'error',scheduler_lag_ms:item.lag,queue_wait_ms:queueWait,service_latency_ms:null,arrival_latency_ms:null,
    latency_ms:null,ttfb_ms:null,status:null,bytes:0,error:null,warmup,grpc_status:null,grpc_message_count:null,grpc_backend_trailer:null};
  conn.inflight++; conn.requests=(conn.requests||0)+1; conn.peak=Math.max(conn.peak,conn.inflight);
  let stream=null,timer=null;
  try {
    const grpc=a.mode==='grpc'; const headers=grpc?{':method':'POST',':path':`/fixture.Echo/${a.method}`,'content-type':'application/grpc','te':'trailers'}:{':method':'GET',':path':a.path,accept:'application/octet-stream'};
    stream=conn.session.request(headers,{endStream:grpc&&a.method==='Unary'?false:!grpc}); stream.once('ready',()=>{row.stream_id=stream.id;}); row.stream_id=stream.id??null;
    const chunks=[]; let trailers={}; let responseHeaders={}; let first=true;
    let timeoutReject; const ended=new Promise((resolve,reject)=>{timeoutReject=reject;stream.on('response',h=>{responseHeaders=h;row.status=Number(h[':status']);if(first){first=false;row.ttfb_ms=now()-begin;}});stream.on('trailers',h=>{trailers=h;});stream.on('data',d=>{row.bytes+=d.length;if(row.bytes>16*1024*1024){stream.close(http2.constants.NGHTTP2_CANCEL);reject(new Error('response_too_large'));return;}chunks.push(d);});stream.on('end',resolve);stream.on('error',reject);stream.on('aborted',()=>reject(new Error('stream_aborted')));});
    timer=setTimeout(()=>{stream.close(http2.constants.NGHTTP2_CANCEL);timeoutReject(new Error('request_timeout'));},a.timeout);
    if(grpc) {
      if(a.method==='Unary') stream.end(frame(Buffer.from('bench')));
      else if(a.method==='ClientStream'||a.method==='Bidi') {const p=a.method==='ClientStream'?['bench0','bench1','bench2']:['bench0','bench1','bench2'];for(const x of p)stream.write(frame(Buffer.from(x)));stream.end();}
      else stream.end(frame(Buffer.from('bench')));
    }
    await ended;
    if(row.status===503&&a.allow503){row.outcome='overload_expected';row.error=null;return row;}
    if(row.status!==200){row.outcome='http_error';throw new Error(`http_status_${row.status}`);}
    const body=Buffer.concat(chunks);
    if(grpc){
      row.grpc_status=String(trailers['grpc-status']??responseHeaders['grpc-status']??'')||null;
      const result=parseGrpcResponse(row.status,responseHeaders,trailers,body); if(result.kind!=='ok'){row.outcome='grpc_error';throw new Error(result.error);}
      const expected=expectedGrpc(a.method),messages=result.messages;
      row.grpc_message_count=messages.length;
      row.grpc_backend_trailer=trailers['backend-trailer']??null;
      if(row.grpc_backend_trailer!=='complete'){row.outcome='grpc_error';throw new Error('grpc_backend_trailer_missing');}
      if(messages.length!==expected.length||messages.some((m,i)=>!m.equals(expected[i]))) {row.outcome='grpc_error';throw new Error('grpc_payload_mismatch');}
    } else if(a.size!==undefined) {
      if(body.length!==a.size) throw new Error(`response_size_${body.length}_expected_${a.size}`);
      const expected=Buffer.alloc(a.size,120);if(!body.equals(expected))throw new Error('response_body_mismatch');
    }
    row.outcome='ok';
  } catch(e) {if(row.outcome==='error')row.outcome='error';row.error=String(e?.message||e);}
  finally {if(timer)clearTimeout(timer);const finished=now();row.ended_at_ms=finished-periodStart;row.service_latency_ms=finished-begin;row.arrival_latency_ms=finished-item.plannedAt;row.latency_ms=row.arrival_latency_ms;conn.inflight=Math.max(0,conn.inflight-1);}
  return row;
}

if(import.meta.url===new URL(`file://${process.argv[1]}`).href) main().catch(e=>{for(const s of OPEN_SESSIONS){try{s.destroy();}catch{}}OPEN_SESSIONS.length=0;console.error(e.stack||e);process.exitCode=1;});
export { parseArgs, frame, parseFrames, parseGrpcResponse, expectedGrpc, runPeriod, execute };
