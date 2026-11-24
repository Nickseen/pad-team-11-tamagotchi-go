import assert from 'node:assert/strict';
import test from 'node:test';
import { checkReleasePins } from './check-release-pins.mjs';

const image = 'example/tamagotchi-map';
const pin = '1234567890abcdef1234567890abcdef12345678';
const catalog = [
  '### Docker Hub images',
  '',
  '| Service | Image | Tag | Status | Host ports | Store | CPR submodule commit |',
  '| --- | --- | --- | --- | --- | --- | --- |',
  '| Map | [`example/tamagotchi-map`](https://hub.docker.com/r/example/tamagotchi-map) | `5.0.0` | published | 8085 | Redis | `1234567` |',
  '',
  '### Prerequisites',
].join('\n');
const gitlinks = new Map([['map', pin]]);
const services = {
  'map-1': { image: `${image}:5.0.0` },
  'map-2': { image: `docker.io/${image}:5.0.0` },
  'map-3': { image: `${image}:5.0.0`, profiles: ['scale'] },
  'map-redis-1': { image: 'redis:7.4-alpine' },
};

test('checks all application replicas while skipping the underlying store', () => {
  assert.deepEqual(checkReleasePins(catalog, services, gitlinks, '5'), { count: 1, errors: [] });
});

test('reports version and build drift in a profiled application replica', () => {
  const invalid = { ...services, 'map-3': { image: `${image}:5.0.1`, build: { context: '.' } } };
  assert.deepEqual(checkReleasePins(catalog, invalid, gitlinks).errors, [
    'map-3: Compose must use the documented release image example/tamagotchi-map:5.0.0.',
  ]);
});

test('rejects release-line and gitlink drift', () => {
  const other = new Map([['map', 'abcdef1234567890abcdef1234567890abcdef12']]);
  assert.deepEqual(checkReleasePins(catalog, services, other, '6').errors, [
    'map: README pin 1234567 differs from gitlink abcdef123456.',
    'map: 5.0.0 does not belong to release line 6.x.',
  ]);
});

test('reports a missing catalog row, application container or gitlink', () => {
  assert.deepEqual(checkReleasePins(catalog, {}, new Map([['map', pin], ['raid', pin]])).errors, [
    'map: no Compose application service found.',
    'raid: service gitlink has no README row.',
  ]);
  assert.match(checkReleasePins(catalog, services, new Map([['raid', pin]])).errors[0], /map: README row has no service gitlink/);
});
