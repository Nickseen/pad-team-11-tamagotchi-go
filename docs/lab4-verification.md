# Lab 4 verification and release gate (#165)

Status: preparation only. The shared stack has not been certified for grades 2–10.
Use the reviewed Lab 4 images and actual released main commits before recording final evidence.
Private service unit tests or an isolated fake consumer do not prove the complete team deployment.

## Before the rehearsal

1. Merge the reviewed #151 contract after all four owners confirm it. Publish dependency protos
   from their actual owner implementations. Pending schemas use Broker HTTP queues, not invented RPCs.
2. Merge SD/Gateway/Broker and each domain implementation through protected dev PRs.
3. Release each image from dev via release/v4.x.y into main with two approvals. Check successful
   main publication, immutable Git tag and the image's revision/version labels.
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

The following preflight is read-only and checks only SD/Broker prerequisites, not grades 2–10:

```sh
# Export SERVICE_TOKEN privately. Do not paste it into the terminal command history.
DISCOVERY_URL=http://localhost:8090 BROKER_URL=http://localhost:8091 \
  node scripts/verify-lab4.mjs
```

SD may stay private. Run the script on the Compose network or use an explicit localhost diagnostics
binding. `EXPECTED_REPLICAS=2` and all eight services are checked by default. A partial local
integration can set `SERVICES=map,monster-raid` for diagnosis; that is not a final release check.

## Current evidence and remaining gates

The implementation PRs for #151/#152/#163/#164 record their own exact tests, coverage and isolated
integration scope. Their candidate `4.0.0` versions do not establish published images. The existing
UM schema from #166 is the released UM 4.0.0 source; Guild/Tamagotchi/Registry schemas must be
published by their owners before their callers can finish the all-gRPC grade 8 migration.

No grade above is checked off until its actual shared-stack receipt is recorded on #165.
Keep #165 open and the Project item out of Done until all checks and publication are complete.

## Release CPR

Once every receipt passes, cut `release/v4.x.y` from the fully integrated dev. Complete the CPR PR
summary/testing/breaking-change/checklist template, link completed issues with closing keywords,
obtain two independent approvals, resolve all conversations and wait for required CI. Squash merge
without bypass, tag the actual main release commit, and synchronize main's release metadata back
into dev through a reviewed PR. Do not tag feature commits or rewrite immutable release tags.
