import http2 from 'node:http2';
const mode=process.argv[2];
const session=http2.connect('http://proxy-clear:8080');
const request=session.request({':method':'GET',':path':'/lifecycle'});
let status=0, body='', goaway=false;
request.on('response',headers=>{status=headers[':status'];console.log('ACTIVE')});
request.on('data',chunk=>{body+=chunk});
request.on('end',()=>{
  if(status!==200||body!=='lifecycle-body')throw new Error(`active request returned ${status} with body ${JSON.stringify(body)}`);
  setTimeout(()=>{
    if(mode==='term'&&!goaway)throw new Error('SIGTERM did not send GOAWAY');
    console.log(JSON.stringify({result:'PASS',status,goaway}));
    session.close();
  },100);
});
session.on('goaway',()=>{goaway=true});
request.on('error',error=>{console.error(error);process.exitCode=1});
request.end();
