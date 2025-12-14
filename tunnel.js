const lt = require('localtunnel');

(async () => {
  const tunnel = await lt({ port: 5000 });
  console.log('Tunnel URL:', tunnel.url);
  tunnel.on('close', () => console.log('tunnel closed'));
})();
