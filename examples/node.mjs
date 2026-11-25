import http from 'node:http';
const server = http.createServer((req, res) => {
  if (req.url === '/sse') {
    res.writeHead(200, {'content-type': 'text/event-stream'});
    let n = 0;
    const timer = setInterval(() => {
      res.write(`data: ${n++}\n\n`);
      if (n === 5) { clearInterval(timer); res.end(); }
    }, 250);
    res.on('close', () => clearInterval(timer));
    return;
  }
  const data = JSON.stringify({server: 'node', path: req.url.split('?')[0]});
  res.writeHead(200, {'content-type': 'application/json', 'content-length': Buffer.byteLength(data)});
  res.end(data);
});
server.listen(Number(process.env.PORT || 8001), '0.0.0.0');
process.on('SIGTERM', () => server.close());
