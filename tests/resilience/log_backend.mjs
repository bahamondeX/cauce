import http from 'node:http';
http.createServer((req, res) => {
  req.resume();
  req.on('end', () => {
    res.writeHead(200, {'content-type': 'text/plain', 'content-length': '14'});
    res.end('resilience-ok\n');
  });
}).listen(8000, '0.0.0.0');
