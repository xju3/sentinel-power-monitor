const http = require('http');
const fs = require('fs');
const path = require('path');

const HOST = process.env.WEB_HOST || '0.0.0.0';
const START_PORT = Number(process.env.WEB_PORT || 9000);
const PORT_FIXED = Boolean(process.env.WEB_PORT);
const ROOT = __dirname;

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
};

function resolvePath(urlPath) {
  const cleanPath = decodeURIComponent(urlPath.split('?')[0]);
  const relativePath = cleanPath === '/' ? '/index.html' : cleanPath;
  const filePath = path.normalize(path.join(ROOT, relativePath));
  if (!filePath.startsWith(ROOT)) return null;
  return filePath;
}

function handleRequest(req, res) {
  const filePath = resolvePath(req.url || '/');
  if (!filePath) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }

    const ext = path.extname(filePath);
    res.writeHead(200, {
      'Content-Type': MIME_TYPES[ext] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    });
    res.end(data);
  });
}

function listen(port) {
  const server = http.createServer(handleRequest);

  server.once('error', (err) => {
    if (err.code === 'EADDRINUSE' && !PORT_FIXED) {
      console.log(`[web] port ${port} is busy, trying ${port + 1}`);
      listen(port + 1);
      return;
    }

    console.error(`[web] failed to start on port ${port}: ${err.message}`);
    process.exit(1);
  });

  server.listen(port, HOST, () => {
    console.log(`[web] http://localhost:${port}`);
  });
}

listen(START_PORT);
