# Postman Collections

Each microservice contributes its own collection here so the whole team can exercise every service
from one place, without hunting through individual service repositories.

| File | Service | Default `baseUrl` |
| ---- | ------- | ------------------ |
| `package-registry.postman_collection.json` | Package Registry | `http://localhost:8083` |
| `guild.postman_collection.json` | Guild | `http://localhost:8087` |

Every collection ships its own `baseUrl` collection variable with a working default, so importing a
single collection is enough to run it — no shared environment is required.

If a shared `tamagotchi-go.postman_environment.json` exists in this folder (contributed by whoever
adds the next service), it's an optional convenience for people testing several services in one
Postman session — see its `values` for the variable names it exposes before wiring `baseUrl` to it.

## Importing

1. In Postman, **File → Import** and select one or more `*.postman_collection.json` files.
2. Start the corresponding service locally (see that service's own README).
3. Run requests individually, or use **Collection Runner** to execute a whole folder top-to-bottom —
   later requests in a folder reuse ids (`packageId`, `guildId`, `monsterId`, …) captured from
   earlier responses via collection variables, so the happy-path flow works end to end in one pass.

## Conventions used across every collection

- Requests are grouped into folders that mirror the service's README endpoint tables.
- A trailing **Error Scenarios** folder exercises at least one `401`, `404` and `403` case per
  service, matching the "Verified error scenarios" section of that service's README.
- Auth is stubbed the same way the underlying services stub it until User Management issues real
  JWTs: client-authenticated requests carry `X-User-Id: <uuid>`, admin-only requests carry
  `X-Roles: admin`. See each service's README for the current status of that placeholder.

## Adding your own service's collection

Export your collection as **Collection v2.1**, name it `<service-name>.postman_collection.json`,
and add a row to the table above in the same pull request.

**If you're adding or editing `tamagotchi-go.postman_environment.json`**: check whether it already
exists on `dev` first. It's meant to be a single shared file — if it's already there, add your
service's variables to the existing file instead of creating a competing one, to avoid an add/add
merge conflict with whoever touches it next.
