import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

function catalogRows(readme) {
  const section = readme.split(/^### /m).find(part => part.startsWith('Docker Hub images\n'));
  if (!section) throw new Error('README is missing the Docker Hub images section.');

  const rows = new Map();
  for (const line of section.split('\n')) {
    if (!line.startsWith('|') || /^\| *(?:Service\s*\||-)/.test(line)) continue;
    const columns = line.split('|').slice(1, -1).map(value => value.trim());
    const image = columns[1]?.match(/^\[`([^\s`]+)`\]\(/)?.[1];
    const tag = columns[2]?.match(/^`(\d+\.\d+\.\d+)`$/)?.[1];
    const pin = columns[6]?.match(/^`([a-f0-9]{7,40})`$/)?.[1];
    if (!image || !tag || !pin) throw new Error(`Invalid README Docker Hub row for ${columns[0]}.`);
    const slug = image.match(/\/tamagotchi-([a-z0-9-]+)$/)?.[1];
    if (!slug || rows.has(slug)) throw new Error(`Invalid or repeated README image for ${columns[0]}.`);
    rows.set(slug, { image, tag, pin });
  }
  if (!rows.size) throw new Error('README Docker Hub table has no service rows.');
  return rows;
}

export function checkReleasePins(readme, services, gitlinks, expectedMajor) {
  const rows = catalogRows(readme);
  if (!gitlinks.size) throw new Error('Git index has no service submodule pins.');
  const errors = [];
  for (const slug of rows.keys()) {
    if (!gitlinks.has(slug)) errors.push(`${slug}: README row has no service gitlink.`);
  }
  for (const [slug, commit] of gitlinks) {
    const row = rows.get(slug);
    if (!row) {
      errors.push(`${slug}: service gitlink has no README row.`);
      continue;
    }
    if (!commit.startsWith(row.pin)) errors.push(`${slug}: README pin ${row.pin} differs from gitlink ${commit.slice(0, 12)}.`);
    if (expectedMajor && row.tag.split('.')[0] !== expectedMajor) {
      errors.push(`${slug}: ${row.tag} does not belong to release line ${expectedMajor}.x.`);
    }
    const replicas = Object.entries(services).filter(([name]) => name === slug ||
      (name.startsWith(`${slug}-`) && /^\d+$/.test(name.slice(slug.length + 1))));
    if (!replicas.length) errors.push(`${slug}: no Compose application service found.`);
    for (const [name, service] of replicas) {
      const expectedImage = `${row.image}:${row.tag}`;
      if (service.image?.replace(/^docker\.io\//, '') !== expectedImage || service.build) {
        errors.push(`${name}: Compose must use the documented release image ${expectedImage}.`);
      }
    }
  }
  return { count: gitlinks.size, errors };
}

function readCommand(command, args, cwd) {
  try {
    return execFileSync(command, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    throw new Error(`Could not run ${command} ${args.join(' ')}.`);
  }
}

function main(args) {
  const expectedMajor = args[1];
  if (args.length && (args.length !== 2 || args[0] !== '--major' || !/^[1-9]\d*$/.test(expectedMajor))) {
    console.error('Usage: node scripts/check-release-pins.mjs [--major VERSION]');
    return 2;
  }
  const root = fileURLToPath(new URL('../', import.meta.url));
  const readme = readFileSync(new URL('../README.md', import.meta.url), 'utf8');
  const index = readCommand('git', ['ls-files', '--stage', '-z', '--', 'services'], root);
  const gitlinks = new Map();
  for (const entry of index.split('\0')) {
    const match = entry.match(/^160000 ([a-f0-9]{40}) 0\tservices\/([^/]+)$/);
    if (match) gitlinks.set(match[2], match[1]);
  }
  const compose = JSON.parse(readCommand('docker', [
    'compose', '--env-file', '.env.example', '--profile', '*', 'config', '--format', 'json',
  ], root));
  const result = checkReleasePins(readme, compose.services, gitlinks, expectedMajor);
  if (result.errors.length) {
    for (const error of result.errors) console.error(`FAIL ${error}`);
    return 1;
  }
  console.log(`PASS ${result.count} service gitlinks, release catalog rows and Compose image references agree.`);
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
