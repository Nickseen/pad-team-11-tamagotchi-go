// Read-only deployment preflight. A pass is not a grade-10 certificate.
const services = (process.env.SERVICES ?? 'user-management,tamagotchi,package-registry,battle,map,notification,guild,monster-raid').split(',');
const expected = Number(process.env.EXPECTED_REPLICAS ?? 2);
const token = process.env.SERVICE_TOKEN;
const sd = process.env.DISCOVERY_URL ?? 'http://localhost:8090';
const broker = process.env.BROKER_URL ?? 'http://localhost:8091';
const allowed = new Set(['user-management','tamagotchi','package-registry','battle','map','notification','guild','monster-raid']);
if (!token || token.startsWith('CHANGE_ME') || !Number.isSafeInteger(expected) || expected < 1 || services.some(s => !allowed.has(s))) {
  console.error('Set a valid SERVICE_TOKEN, EXPECTED_REPLICAS and service list.'); process.exit(2);
}
for (const origin of [sd, broker]) {
  const u = new URL(origin);
  if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password || u.pathname !== '/' || u.search || u.hash) {
    console.error('Discovery/Broker settings must be HTTP origins without credentials or paths.'); process.exit(2);
  }
}
let failed = false;
async function read(base, path) {
  const response = await fetch(base + path, { headers: {'X-Service-Token': token}, redirect: 'error', signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}
async function check(label, action) {
  try { await action(); console.log('PASS ' + label); }
  catch { failed = true; console.error('FAIL ' + label + ' (inspect status/logs; credentials and response bodies are omitted)'); }
}
await check('Broker ready on Lab 4 version', async () => {
  const status = await read(broker, '/api/v1/status');
  if (!status.ready || !/^4\.\d+\.\d+$/.test(status.version)) throw new Error();
});
await check('Broker topic inspection', () => read(broker, '/api/v1/topics'));
for (const service of services) await check(service + ': ready replicas and Lab 4 metadata', async () => {
  const reply = await read(sd, '/api/v1/internal/discovery/services/' + service + '/instances');
  if (!Array.isArray(reply.instances) || reply.instances.length < expected) throw new Error();
  for (const instance of reply.instances) {
    if (instance.state !== 'ready' || !/^4\.\d+\.\d+$/.test(instance.version) || !Array.isArray(instance.topics) ||
        !instance.eventsPath?.startsWith('/api/v1/') || !['http','grpc'].includes(instance.eventsTransport) ||
        !instance.load || !Number.isFinite(instance.load.ratio) || instance.load.maxTasks < 1) throw new Error();
    if (['user-management','tamagotchi','package-registry','guild','map','monster-raid'].includes(service) && !instance.grpcAddress) throw new Error();
  }
});
console.log('Preflight only. Verify PDF evidence, reviewed images/pins, logs, gRPC, durability, failover and dashboards separately.');
if (failed) process.exitCode = 1;
