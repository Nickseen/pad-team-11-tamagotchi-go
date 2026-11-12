import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import http from 'node:http';
import { test } from 'node:test';

const token = 'preflight-fixture-token-not-a-credential';

async function run(t, { gatewayVersion = '4.0.0', sdVersion = '4.0.0', domainVersion = '4.0.0' } = {}) {
  const origins = {};
  for (const role of ['sd', 'broker', 'gateway']) {
    const server = http.createServer((req, res) => {
      assert.equal(req.headers['x-service-token'], token);
      let body;
      if (role === 'gateway') body = { service: 'gateway', version: gatewayVersion };
      else if (req.url.endsWith('/instances')) body = { instances: [{
        state: 'ready', version: domainVersion, topics: [], eventsPath: '/api/v1/internal/events',
        eventsTransport: 'http', grpcAddress: 'map-fixture:9085', load: { ratio: 0, maxTasks: 10 },
      }] };
      else if (req.url.endsWith('/topics')) body = { topics: [] };
      else body = { ready: true, version: role === 'sd' ? sdVersion : '4.0.0' };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origins[role] = `http://127.0.0.1:${server.address().port}`;
    t.after(() => new Promise(resolve => {
      server.closeAllConnections();
      server.close(resolve);
    }));
  }
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [new URL('./verify-lab4.mjs', import.meta.url).pathname], {
      env: { ...process.env, SERVICE_TOKEN: token, SERVICES: 'map', EXPECTED_REPLICAS: '1',
        DISCOVERY_URL: origins.sd, BROKER_URL: origins.broker, GATEWAY_URL: origins.gateway },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    child.stdout.on('data', data => { output += data; });
    child.stderr.on('data', data => { output += data; });
    child.once('error', reject);
    child.once('close', code => {
      assert.ok(!output.includes(token), 'preflight must not print the service token');
      resolve({ code, output });
    });
  });
}

test('a ready Lab 4 fixture passes the preflight without claiming full-stack certification', async t => {
  const result = await run(t);
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, /Preflight only/);
});

for (const [setting, label] of [
  ['gatewayVersion', 'Gateway on Lab 4 version'],
  ['sdVersion', 'Discovery ready on Lab 4 version'],
  ['domainVersion', 'map: ready replicas and Lab 4 metadata'],
]) test(`an otherwise-ready deployment with ${setting} 3.x fails`, async t => {
  const result = await run(t, { [setting]: '3.0.0' });
  assert.equal(result.code, 1);
  assert.ok(result.output.includes('FAIL ' + label), result.output);
});
