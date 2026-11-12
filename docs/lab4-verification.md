# Lab 4 verification and release gate (#165)

Status: preparation only. The shared stack has not been certified for grades 2–10.
Use the reviewed Lab 4 images and actual released main commits before recording final evidence.
Private service unit tests or an isolated fake consumer do not prove the complete team deployment.

## Before the rehearsal

1. Merge the reviewed #151 contract after all four owners confirm it. Publish dependency protos
   from their actual owner implementations. Pending schemas use Broker HTTP queues, not invented RPCs.
2. Merge SD/Gateway/Broker and each domain implementation through protected dev PRs.
3. Release each image from dev via release/v4.x.y into main under its repository's approval policy.
   Map/Raid use Nickseen's explicit solo-merge policy; shared repositories retain their review gates.
   Check successful main publication, immutable Git tag and the image's revision/version evidence.
   Broker release is currently deferred by its owner: local builds may be prepared, but they do not
   complete the published-release gate.
4. Adopt actual released main SHAs in CPR submodules and matching image versions in Compose.
   Integrate #154 (Compose/metrics), #155 (logs) and #156 (README/diagram/Postman).
5. Bring up two ready replicas per domain on the private network. Keep client REST through Gateway;
   Broker inspection is localhost-only with the service token; WebSockets use negotiated direct URLs.

## Record evidence for every grade

Run from the CPR root. Record UTC time, the CPR commit, image digests and correlated log excerpts.
Never paste tokens, JWTs, WebSocket tickets, passwords or full personal request bodies.

| Grade | Demonstration | Required evidence |
| --- | --- | --- |
| 2 | Open private Broker repository and protection/CI/release pages | Rust implementation, team/teaching access, reviewed history, successful release checks |
| 3 | Compare README, diagram and imported existing Postman collections | Topology matches the running images; all intended collection assertions pass |
| 4 | Download one service's logs, then `all`, using the reviewed #155 API | Both downloads work; JSON logs retain service/instance/correlation context and redact credentials |
| 5 | Make normal client requests through Gateway | Broker queues handle them; Broker logs, not Gateway, select instances and own physical breakers; no domain calls Gateway |
| 6 | Request through a service queue and publish a domain event | Queue metrics, upstream instance header, SD topics, `GET /api/v1/topics`; each subscriber service gets a copy |
| 7 | Make several requests through Broker | Broker logs show named OS threads `req-<n>` for individual requests; list-topics endpoint works |
| 8 | Call an internal RPC through Broker with metadata/deadline | Published proto is used; Broker `grpc.completed` logs and server result; client `/internal` routes rejected by Gateway |
| 9 | Queue an event with its consumer unavailable; restart Broker | Same durable pending event survives; after recovery it commits; permanent error appears in dead letters and can be replayed |
| 10 | Trip one replica's endpoint breaker with a controlled fault; send a safe request | Request goes to the healthy sibling; SD removal/attempt IDs match; separately show least-load selection under uneven load and Grafana/Prometheus panels |

Do not treat a graceful stop as a simulated failed physical request: graceful deregistration proves
membership handling; a counted failing endpoint proves breaker/failover behavior. Use a disposable
fault instance and queue journal for disruptive checks, never the only copy of shared data.

### Read-only inspection commands

```sh
docker compose ps
docker compose config --images
docker compose logs --since 2m message-broker
docker compose logs --since 2m service-discovery
docker compose logs --since 2m map-1 map-2 monster-raid-1 monster-raid-2
```

Use `grep -E` if `rg` is not installed. Keep original JSON log lines for correlation; avoid filtering
away the event that identifies why a breaker tripped.

The following preflight is read-only and checks Gateway/SD/Broker and registration prerequisites,
not the full grades 2–10 behavior:

```sh
# Export SERVICE_TOKEN privately. Do not paste it into the terminal command history.
DISCOVERY_URL=http://localhost:8090 BROKER_URL=http://localhost:8091 GATEWAY_URL=http://localhost:8080 \
  node scripts/verify-lab4.mjs
```

SD may stay private. Run the script on the Compose network or use an explicit localhost diagnostics
binding. `EXPECTED_REPLICAS=2` and all eight services are checked by default. A partial local
integration can set `SERVICES=map,monster-raid` for diagnosis; that is not a final release check.

With a running stack, the Gateway container already has the private origins and SERVICE_TOKEN:

```sh
docker compose exec -T gateway node --input-type=module < scripts/verify-lab4.mjs
```

The script checks Gateway/SD/Broker versions as well as domain registrations; a 3.x Gateway must
fail even if ready 4.x domain replicas are registered. The current staged rollout is expected to
fail until the remaining releases are adopted. Do not run disruptive demos as acceptance checks
before this preflight passes.

The preflight regression fixtures can be checked without starting the stack:

```sh
node --test scripts/verify-lab4.test.mjs
```

## Current evidence and remaining gates

The accepted contract (#167) and all dependency schemas (#166/#169/#172) are merged. Map #19,
Raid #22 and SD #10 implementations are merged into their dev branches. Broker #13 is also merged:
Go callers now receive the real trailers-only gRPC status, and duplicate token metadata is rejected.
Their CI and isolated integration receipts do not certify the complete team deployment.

Published and adopted here: Map 4.0.0 (`25699bb`), SD 4.0.0 (`120d0d3`), Message Broker 4.0.0
(`cd194d5`), User Management 4.0.1 (`70edafc`), Battle 4.0.1 (`8e369a6`), Guild 4.0.0 (`c643213`)
and Package Registry 4.0.1 (`8fb32bd`). Map's, Guild's and Package Registry's image revision labels
match main. SD, the Broker, User Management and Battle each have a successful main
publication and an immutable `v4.x.y` tag on that commit. UM/Battle public image provenance records
the main ref, source SHA and publication run for both architectures, even without private-repository
access.

Gateway, Raid, Tamagotchi and Notification still need published 4.x adoption in CPR.
Raid's implementation is merged, but its 3.x selected image/mocked default is not Lab 4 evidence.
Switch Raid's default to grpc with its 4.x image and released main pin. Existing .env mode overrides
must also be checked before the rehearsal; Map now defaults to grpc.

Two limitations to settle before recording final evidence:
- Gateway dev still performs Guild/Raid WebSocket eligibility reads through Broker HTTP queues;
  demonstrate the required internal gRPC migration or agree the infrastructure exception explicitly.
- SD 4.0.0 has no /metrics endpoint (401 without a service token, 404 with one). Do not scrape a
  nonexistent route. Its status/registry and structured Loki logs remain available; an SD metrics
  scrape requires an actual released endpoint.

No grade above is checked off until its actual shared-stack receipt is recorded on #165.
Keep #165 open and the Project item out of Done until all checks and publication are complete.

## Release CPR

Once every receipt passes, cut `release/v4.x.y` from the fully integrated dev. Complete the CPR PR
summary/testing/breaking-change/checklist template, link completed issues with closing keywords,
obtain two independent approvals, resolve all conversations and wait for required CI. Squash merge
without bypass, tag the actual main release commit, and synchronize main's release metadata back
into dev through a reviewed PR. Do not tag feature commits or rewrite immutable release tags.
