import test from 'node:test';
import assert from 'node:assert/strict';
import http2 from 'node:http2';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parseArgs, frame, parseFrames, parseGrpcResponse, expectedGrpc, runPeriod, execute } from '../load_h2.mjs';

test('CLI defaults and validates HTTP/2/gRPC modes',()=>{
  const a=parseArgs(['--url','http://localhost:8080','--out','/tmp/run']);
  assert.equal(a.mode,'http2'); assert.equal(a.connections,2); assert.equal(a.streams,16);
  assert.throws(()=>parseArgs(['--url','https://proxy','--out','x','--mode','grpc','--method','Nope']),/--method/);
  assert.throws(()=>parseArgs(['--url','ftp://proxy','--out','x']),/URL/);
  assert.throws(()=>parseArgs(['--url','http://proxy','--out','x','--rate','200000']),/invalid rate/);
  assert.equal(parseArgs(['--url','http://proxy','--out','x','--allow-503','true']).allow503,true);
  assert.throws(()=>parseArgs(['--url','http://proxy','--out','x','--allow-503','yes']),/true or false/);
});

test('gRPC framing decodes coalesced frames and rejects malformed frames',()=>{
  const b=Buffer.concat([frame(Buffer.from('one')),frame(Buffer.from('two'))]);
  assert.deepEqual(parseFrames(b).map(x=>x.toString()),['one','two']);
  assert.throws(()=>parseFrames(Buffer.from([0,0,0])),/truncated_grpc_frame_header/);
  assert.throws(()=>parseFrames(Buffer.from([1,0,0,0,0])),/compressed_grpc_frame_unsupported/);
  assert.throws(()=>parseFrames(Buffer.from([0,0,0,0,4,65])),/truncated_grpc_frame_payload/);
  assert.deepEqual(expectedGrpc('Bidi').map(x=>x.toString()),['bidi:bench0','bidi:bench1','bidi:bench2']);
  assert.equal(parseGrpcResponse(200,{'content-type':'application/grpc'},{'grpc-status':'0'},frame(Buffer.from('ok'))).kind,'ok');
  assert.equal(parseGrpcResponse(200,{'content-type':'application/grpc'},{},Buffer.alloc(0)).error,'grpc_status_missing:');
  assert.equal(parseGrpcResponse(200,{'content-type':'application/grpc'},{'grpc-status':'7','grpc-message':'failed'},Buffer.alloc(0)).error,'grpc_status_7:failed');
  assert.equal(parseGrpcResponse(200,{'content-type':'application/json'},{'grpc-status':'0'},Buffer.alloc(0)).error,'invalid_grpc_content_type');
});

test('open-loop scheduler multiplexes streams and records bounded-queue rejections',async()=>{
  let active=0,peak=0;
  const server=http2.createServer();
  server.on('stream',(s)=>{active++;peak=Math.max(peak,active);s.respond({':status':200,'content-type':'application/octet-stream'});setTimeout(()=>s.end(Buffer.alloc(8,120)),15);s.on('close',()=>active--);});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port=server.address().port; const session=http2.connect(`http://127.0.0.1:${port}`);
  await new Promise((resolve,reject)=>{session.once('connect',resolve);session.once('error',reject);});
  const a={mode:'http2',method:'Unary',path:'/bytes',size:8,timeout:1000,rate:100,connections:1,streams:4,queue:20};
  const rows=[];const stats=await runPeriod(a,.1,[{id:0,session,inflight:0,peak:0}],rows,false);
  assert.equal(stats.offered,10); assert.equal(rows.length,10); assert.equal(rows.filter(x=>x.outcome==='ok').length,10); assert.ok(peak>1,`expected multiplexed streams; backend peak=${peak}`);
  for(const row of rows){assert.ok(row.started_at_ms>=row.scheduled_at_ms,JSON.stringify(row));assert.ok(row.ended_at_ms>=row.started_at_ms,JSON.stringify(row));assert.ok(row.arrival_latency_ms>=0,JSON.stringify(row));}
  session.close(); await new Promise(resolve=>server.close(resolve));

  const slow=http2.createServer(); slow.on('stream',s=>{s.respond({':status':200});setTimeout(()=>s.end(Buffer.alloc(8,120)),45);});
  await new Promise(resolve=>slow.listen(0,'127.0.0.1',resolve));
  const client=http2.connect(`http://127.0.0.1:${slow.address().port}`);await new Promise((r,j)=>{client.once('connect',r);client.once('error',j);});
  const saturated={...a,rate:1000,connections:1,streams:1,queue:1};const out=[];
  const result=await runPeriod(saturated,.1,[{id:0,session:client,inflight:0,peak:0}],out,false);
  assert.equal(result.offered,100);assert.ok(result.rejected>0);assert.ok(out.some(x=>x.outcome==='rejected_queue_full'));
  for(const row of out){assert.ok(row.ended_at_ms>=row.scheduled_at_ms,JSON.stringify(row));assert.ok(row.arrival_latency_ms>=0,JSON.stringify(row));}
  client.close(); await new Promise(resolve=>slow.close(resolve));
});

test('execute validates gRPC terminal status and payload; clears completed timeout timer',async()=>{
  let behavior='ok'; const server=http2.createServer();
  server.on('stream',(s)=>{
    const body=behavior==='wrong'?frame(Buffer.from('wrong')):frame(Buffer.from('echo:bench'));
    const trailers=behavior==='missing'?{'backend-trailer':'complete'}:behavior==='nonzero'?{'grpc-status':'7','grpc-message':'denied','backend-trailer':'complete'}:{'grpc-status':'0','backend-trailer':'complete'};
    if(behavior==='missing') { s.respond({':status':200,'content-type':'application/grpc'});s.end(body); }
    else { s.respond({':status':200,'content-type':'application/grpc'},{waitForTrailers:true});s.on('wantTrailers',()=>s.sendTrailers(trailers));s.end(body); }
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const session=http2.connect(`http://127.0.0.1:${server.address().port}`);await new Promise((r,j)=>{session.once('connect',r);session.once('error',j);});
  const conn={id:0,session,inflight:0,peak:0,requests:0};
  const call=async()=>execute({mode:'grpc',method:'Unary',timeout:800},conn,{n:0,plannedAt:performance.now(),actualAt:performance.now(),lag:0},0,false,0);
  const started=performance.now(); let row=await call();assert.equal(row.outcome,'ok');assert.equal(row.grpc_status,'0');assert.equal(row.grpc_message_count,1);assert.ok(row.ended_at_ms!==null);
  assert.ok(performance.now()-started<500,'completed call retained timeout timer');
  behavior='missing';row=await call();assert.equal(row.outcome,'grpc_error');assert.match(row.error,/grpc_status_missing/);
  behavior='nonzero';row=await call();assert.equal(row.outcome,'grpc_error');assert.match(row.error,/grpc_status_7/);
  behavior='wrong';row=await call();assert.equal(row.outcome,'grpc_error');assert.match(row.error,/payload_mismatch/);
  session.close();await new Promise(resolve=>server.close(resolve));
});

test('HTTP 503 overload is an explicit expected result and skips body-size assertion',async()=>{
  const server=http2.createServer();server.on('stream',s=>{s.respond({':status':503});s.end('busy');});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const session=http2.connect(`http://127.0.0.1:${server.address().port}`);await new Promise((r,j)=>{session.once('connect',r);session.once('error',j);});
  const conn={id:0,session,inflight:0,peak:0,requests:0};const item={n:0,plannedAt:performance.now(),actualAt:performance.now(),lag:0};
  try {
    let row=await execute({mode:'http2',path:'/x',size:1024,allow503:true,timeout:500},conn,item,0,false,0);assert.equal(row.outcome,'overload_expected');assert.equal(row.status,503);
    row=await execute({mode:'http2',path:'/x',size:1024,allow503:false,timeout:500},conn,item,0,false,0);assert.equal(row.outcome,'http_error');assert.equal(row.status,503);
  } finally { session.close();await new Promise(resolve=>server.close(resolve)); }
});

test('CLI warmup and barrier handshake defer result files until release',async()=>{
  const server=http2.createServer({settings:{maxConcurrentStreams:1}});
  server.on('stream',s=>{s.respond({':status':200,'content-type':'application/octet-stream'});s.end(Buffer.alloc(8,120));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'cauce-i8-load-'));const barriers=path.join(root,'barriers');const out=path.join(root,'result');
  const script=fileURLToPath(new URL('../load_h2.mjs',import.meta.url));
  const child=spawn(process.execPath,[script,'--url',`http://127.0.0.1:${server.address().port}`,'--mode','http2','--path','/bytes?size=8','--size','8',
    '--connections','1','--streams','1','--rate','10','--seconds','0.2','--warmup','0.2','--timeout','1000','--out',out,'--barrier',barriers],{stdio:['ignore','pipe','pipe']});
  let stdout='',stderr='';child.stdout.setEncoding('utf8').on('data',x=>stdout+=x);child.stderr.setEncoding('utf8').on('data',x=>stderr+=x);
  const has=async name=>{const end=Date.now()+4000;while(Date.now()<end){if(fs.existsSync(path.join(barriers,name)))return;await new Promise(r=>setTimeout(r,10));}throw new Error(`barrier not reached: ${name}; ${stderr}`);};
  const exited=new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',(code,signal)=>resolve({code,signal}));});
  try {
    await has('warmup.done');fs.writeFileSync(path.join(barriers,'resume'),'go');await has('measurement.done');
    assert.equal(fs.existsSync(`${out}.jsonl`),false);assert.equal(fs.existsSync(`${out}.summary.json`),false);
    fs.writeFileSync(path.join(barriers,'release'),'go');const status=await exited;assert.equal(status.code,0,stderr||stdout);
    const summary=JSON.parse(fs.readFileSync(`${out}.summary.json`,'utf8'));
    assert.equal(summary.warmup.completed,2);assert.equal(summary.offered,2);assert.equal(summary.ok,2);assert.equal(summary.sessions[0].requests,2);
    assert.equal(fs.readFileSync(`${out}.jsonl`,'utf8').trim().split('\n').length,2);
  } finally {
    if(child.exitCode===null)child.kill('SIGKILL');
    await new Promise(resolve=>server.close(resolve));fs.rmSync(root,{recursive:true,force:true});
  }
});
