import http from 'node:http';
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';

const label = process.env.LABEL || 'backend';
const state = { label, total: 0, active: 0, peak: 0, paths: {} };
const app = http.createServer(async (req, res) => {
  state.total++; state.active++; state.peak = Math.max(state.peak, state.active);
  const done = () => { state.active = Math.max(0, state.active - 1); };
  res.once('close', done);
  if (req.method === 'POST') for await (const _chunk of req) { /* consume the complete upload */ }
  const path = new URL(req.url, 'http://backend').pathname;
  state.paths[path] = (state.paths[path] || 0) + 1;
  if (path === '/stats') { res.off('close', done); done(); res.end(JSON.stringify(state)); return; }
  if (path === '/slow') {
    while (!fs.existsSync('/control/release-slow')) await delay(20);
  }
  if (path.endsWith('/drop')) {
    res.writeHead(200, { 'content-type': 'text/plain', 'x-backend': label });
    res.write('partial');
    setTimeout(() => res.socket?.destroy(), 40);
    return;
  }
  const body = JSON.stringify({ backend: label, path, method: req.method });
  res.writeHead(200, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body), 'x-backend': label });
  res.end(body);
});
app.listen(8000, '0.0.0.0');
