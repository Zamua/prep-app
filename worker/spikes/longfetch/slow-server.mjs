// GET /sleep/<seconds> answers after that many seconds. Loopback only.
import { createServer } from 'node:http';
createServer((req, res) => {
  const m = (req.url ?? '').match(/^\/sleep\/(\d+)$/);
  const s = m ? Number(m[1]) : 0;
  setTimeout(() => {
    res.writeHead(200, { 'content-type': 'text/plain' });
    res.end(`slept ${s}s`);
  }, s * 1000);
}).listen(8999, '127.0.0.1', () => console.log('slow-server on 127.0.0.1:8999'));
