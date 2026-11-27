# Lab 5 acceptance, 2026-11-27 (#200)

The Lab 5 rubric checked against the assembled stack before the CPR `v5.0.0` release. The stack ran the tree of
`dev` at `2948478` (the release branch `release/v5.0.0` has the same tree), with only published images: Gateway
5.0.0, Message Broker 5.1.0, Service Discovery 4.0.1, User Management 5.2.0, Tamagotchi 5.2.0, Package Registry
5.0.0, Battle 5.2.0, Map 4.0.1, Notification 5.0.0, Guild 5.0.0, Monster Raid 5.1.0. One laptop ran the whole
stack; the Gateway was published on host port 18080 through a local override because 8080 was taken.

| Grade | Criterion | Result | Evidence |
| --- | --- | --- | --- |
| 2–3 | The final diagram shows the earlier components plus Warehouse, ETL and Cache | pass | [`architecture.svg`](architecture.svg) (#215) draws `warehouse-db`, the `etl` job with `etl/extract.sh` and its inputs, and `cache-1..3` next to the Lab 0–4 components |
| 4 | Warehouse and repeatable ETL from Battle and Raid in Compose | pass | `check-etl.sh` and a live load below; the released Raid export on [#194](https://github.com/Nickseen/pad-team-11-tamagotchi-go/issues/194) |
| 5 | Replica counts, primary loss, promotion and resumed operations | pass | `check-replication.sh` and `check-service-failover.sh` for six services below |
| 6–7 | GET and key-set caching; shard distribution and remapping | pass | live cache checks below; the ring report on #195 and in the README "Gateway cache" |
| 8 | Reproducible 2PC checkpoint: commit, rollback and recovery for both flows | pass | [checkpoint](lab5-2pc-checkpoint.md) (#220), and scenarios 1–4 again on Broker 5.1.0 with `TX_PROTOCOL=2pc` below |
| 9 | Final Saga: both flows, restart, replay and compensation | pass | all 15 scenarios in [`lab5-saga-runs/`](lab5-saga-runs/) (#225), and scenarios 1, 2, 4, 5 and 13 again below |
| 10 | Postman, diagram, Docker Hub, endpoints/DTOs and image/source pins | pass, Newman runs recorded by the owners | pins below; service deliverables on [#199](https://github.com/Nickseen/pad-team-11-tamagotchi-go/issues/199) |

## Grade 4: Warehouse and ETL

- `bash scripts/etl/check-etl.sh` (disposable project): first load, a repeated run that changes nothing, a new
  and a changed record, an interrupted load and its repetition, rejected input and a header-only export all
  passed, and the README's example queries answered (exit 0).
- Live stack: `bash scripts/etl/extract.sh` exported 40 battles with Battle's own `/app/server export-results`;
  `docker compose run --rm etl` applied `001_terminal_results` and inserted 40, and a second run read 40 and
  inserted and updated nothing. Raid was skipped there, because its export runs from the private Raid
  checkout; its released SQL and reload without duplicates are recorded on #194.

## Grade 5: replication and failover

- `bash scripts/ha/check-replication.sh` (exit 0): the PostgreSQL standbys replay the primary; after the
  primary is fenced and a standby promoted, the same connection reads the earlier row and writes, the other
  standby follows the new writer, and the old one rejoins as a standby. Redis under Sentinel: the same
  connection reads the earlier key after promotion, TTLs still expire, and the old writer rejoins as a replica.
- `bash scripts/ha/check-service-failover.sh user-management battle tamagotchi notification package-registry
  guild` (exit 0), each on its released image with the Compose connection string: a write through the service
  was replayed by both standbys; after the primary was killed and a standby promoted, the same running service
  read it, accepted a new write (`201`) and stayed ready (`GET /api/v1/status` 200 ×3). Error lines logged after
  the promotion: 0 for every service except Guild, which logged one while it reconnected.
- Map's Redis and Monster Raid's PostgreSQL recovery are recorded on #193.

## Grades 6–7: cache and sharding

Live through the Gateway, with two fresh users:

| Request | Observed |
| --- | --- |
| `GET /package-registry/api/v1/packages/{id}`, first read | `200`, `X-Cache: MISS` |
| the same read again | `200`, `X-Cache: HIT`, the same body |
| user B, the same URL | `200`, `X-Cache: MISS`: never user A's entry |
| no token | `401` before the cache |
| `PATCH` the package, then read | `MISS` with the new data, then `HIT` |
| a list with its query reordered | `HIT` |
| `GET /tamagotchi/api/v1/combat-types` as another user | `HIT`: one public entry |
| `GET /tamagotchi/api/v1/tamagotchis` | no `X-Cache`: outside the cache rules |
| 12 packages read twice, then `cache-2` stopped | populate 12 `MISS`, again 12 `HIT`; with `cache-2` down 8 `HIT` and 4 `BYPASS`, all `200` |

The last row shows the shard keys: the stopped node's keys are answered by the service, and the other two nodes
keep theirs. Distribution and remapping on adding or removing a node: `npm run ring-report` in the Gateway, as
recorded in the README "Gateway cache" (36.4% / 31.0% / 32.6%; a fourth node moves 26.4% of the keys, all to it).

## Grade 8: two-phase commit

The checkpoint of #220 ran every scenario of the transaction contract on the published images, including a
database promotion. On the release stack, the Broker 5.1.0 still serves two-phase commit with
`TX_PROTOCOL=2pc`: `check-2pc.py --scenario 1,2,3,4` passed 4 of 4 (a battle committed, a second settlement of
the same creature rolled back with `TAMAGOTCHI_NOT_OWNED`, a defeated raid committed, a payout for an active
raid rolled back with `RAID_NOT_DEFEATED`). Report: [`lab5-acceptance/2pc-20261127T141346Z.md`](lab5-acceptance/2pc-20261127T141346Z.md).
The Broker was then restored to its default `saga`.

## Grade 9: Saga

All 15 scenarios of the Saga contract are recorded in #225. On the release stack, `check-saga.py --scenario
1,2,4,5,13` passed 5 of 5: both flows completed; a refusal at `creatures` compensated every step; the
`TX_REJECT_STEP=complete` rehearsal undid the pivot; the Broker killed after `currency` and after `creatures`
continued without applying anything twice; a compensation before its `execute` made the late `execute` answer
`409 TRANSACTION_ABORTED`. Report: [`lab5-acceptance/saga-20261127T141655Z.md`](lab5-acceptance/saga-20261127T141655Z.md).

## Grade 10: published images and source pins

For every service the Compose image, the README release row, the submodule gitlink and the Docker Hub image
agree, and each image is published for amd64 and arm64. The Git tag was read through the GitHub API where this
account can read the repository; the other owners verified their tag against the same commit on #199.

| Service | Image | Compose | README | Submodule | GitHub tag | Docker Hub | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| API Gateway | `nickseen/tamagotchi-gateway` | `5.0.0` | `5.0.0`, `6af5740` | `6af5740` | `v5.0.0` = `6af5740` | amd64/arm64 | ok |
| Message Broker | `nickseen/tamagotchi-message-broker` | `5.1.0` | `5.1.0`, `9e393f4` | `9e393f4` | `v5.1.0` = `9e393f4` | amd64/arm64 | ok |
| Service Discovery | `nickseen/tamagotchi-service-discovery` | `4.0.1` | `4.0.1`, `2294a0a` | `2294a0a` | `v4.0.1` = `2294a0a` | amd64/arm64 | ok |
| User Management | `amzavladislav/tamagotchi-user-management` | `5.2.0` | `5.2.0`, `f48a2bc` | `f48a2bc` | `v5.2.0` = `f48a2bc` | amd64/arm64 | ok |
| Tamagotchi | `crislp/tamagotchi-tamagotchi` | `5.2.0` | `5.2.0`, `ae1b830` | `ae1b830` | on #199 | amd64/arm64 | ok |
| Package Registry | `gabimiric/tamagotchi-package-registry` | `5.0.0` | `5.0.0`, `3caf86e` | `3caf86e` | on #199 | amd64/arm64 | ok |
| Battle | `amzavladislav/tamagotchi-battle` | `5.2.0` | `5.2.0`, `ca8e65d` | `ca8e65d` | `v5.2.0` = `ca8e65d` | amd64/arm64 | ok |
| Map | `nickseen/tamagotchi-map` | `4.0.1` | `4.0.1`, `3ca778c` | `3ca778c` | on #199 | amd64/arm64 | ok |
| Notification | `crislp/tamagotchi-notification` | `5.0.0` | `5.0.0`, `2e69d4a` | `2e69d4a` | on #199 | amd64/arm64 | ok |
| Guild | `gabimiric/tamagotchi-guild` | `5.0.0` | `5.0.0`, `0f6e68b` | `0f6e68b` | on #199 | amd64/arm64 | ok |
| Monster Raid | `nickseen/tamagotchi-monster-raid` | `5.1.0` | `5.1.0`, `95fb77c` | `95fb77c` | on #199 | amd64/arm64 | ok |

Postman: Newman is not installed on the machine that ran this acceptance, so the collections were not run here.
The Lab 5 collections were run by their owners against the published images: `gateway-cache` 28 of 28
assertions (#214), the Raid Saga folder 8 of 8 (#223), Tamagotchi's "Distributed transactions" folder (#212,
#216), and the Saga requests of User Management, Battle and the Broker (#224). The collections of the services
whose API did not change in Lab 5 are those of the Lab 4 acceptance. Map and Raid WebSockets are unchanged since
Lab 4 and were not run again. Endpoints and DTOs: the README sections linked per service on #199.
