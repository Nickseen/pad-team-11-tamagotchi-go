# Database schemas

The schema of every service that owns a SQL database, collected so it can be read without access to
the service repositories. Each service remains the single owner of its schema: these are copies of
the migrations embedded in the published images, not a second source of truth.

| Service | Database | Schema | How it is applied |
| ------- | -------- | ------ | ----------------- |
| User Management | `usermgmt` | [`user-management/0001_init.sql`](user-management/0001_init.sql) | Embedded in the binary, applied on start-up and recorded in `schema_migrations` |
| Battle | `battle` | [`battle/0001_init.sql`](battle/0001_init.sql) | Embedded in the binary, applied on start-up and recorded in `schema_migrations` |
| Monster Raid | `raid` | [`monster-raid/001_init.sql`](monster-raid/001_init.sql) | Embedded in the binary, idempotent, applied on start-up |
| Tamagotchi | `tamagotchi` | — | Created from the SQLAlchemy models on start-up; no SQL file exists |
| Notification | `notification` | — | Created from the SQLAlchemy models on start-up; no SQL file exists |
| Package Registry | `registry` | — | Created from the SQLAlchemy models on start-up; no SQL file exists |
| Guild | `guild` | — | Created from the SQLAlchemy models on start-up; no SQL file exists |
| Map | — | — | Redis only; keys are created at run time with a five-minute TTL |

Every service creates its own schema when it starts, so an empty database is all the team
[`docker-compose.yml`](../docker-compose.yml) needs. **Do not mount these files into
`/docker-entrypoint-initdb.d`:** User Management and Battle track applied migrations themselves,
and a schema created behind their backs makes their first start fail on an existing table.

No script populates the databases with test data; the Postman collections in
[`../postman`](../postman) create everything a run needs.
