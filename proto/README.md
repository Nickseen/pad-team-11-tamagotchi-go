# gRPC contracts

Lab 4 moves internal routes to gRPC through the Message Broker (contract issue #151). This folder
holds the `.proto` file of every service that serves internal RPCs, so a caller can generate a
client without access to the owner's repository.

## Published protos

| File | Package | Service | Owner | Served by |
| ---- | ------- | ------- | ----- | --------- |
| [`tamagotchi/user_management/v1/user_management.proto`](tamagotchi/user_management/v1/user_management.proto) | `tamagotchi.user_management.v1` | `UserManagementInternal` | User Management | 4.0.0 and later, `GRPC_PORT` (default `9081`) |

Not published yet: Tamagotchi (#159), Guild (#161), Package Registry (#162), Map (#163), Monster
Raid (#164) and the Broker's own `tamagotchi/broker/v1/broker.proto` (#151). Until a dependency's
proto is here and its service serves it, call that dependency through its Broker HTTP queue.

## Rules

- One file per service: `proto/tamagotchi/<package>/v1/<package>.proto`. The package is
  `tamagotchi.<service with _ instead of ->.v1`; the Broker routes `/<package>.<Service>/<Rpc>` to
  that service by this name. Each file has one `<Name>Internal` service.
- Each file is a byte-for-byte copy from the owner's release tag. The owner updates it here through
  a reviewed PR when a release changes it. Nobody edits it in place.
- `v1` changes only compatibly: add fields and RPCs; never renumber, retype or remove a field
  (mark it `reserved`). A breaking change is a new `v2` package next to `v1`.
- Field conventions: snake_case names, UUIDs and timestamps as strings (RFC 3339 UTC with
  milliseconds), currency as `int64`.
- `go_package` points at the owner's module. Callers set their own import path when generating
  (see below).

## Calling a service through the Broker

- Address: `message-broker:9091` inside Compose, plaintext HTTP/2 (no TLS). Use one connection for
  the whole process.
- Metadata: `x-service-token` (the shared `SERVICE_TOKEN`; without it the call is `UNAUTHENTICATED`)
  and `x-correlation-id` (the request's correlation ID).
- Deadline: set one on every call, at most the remaining time of your own request.
- Errors: the gRPC status maps the REST status, and its message starts with the CPR error code,
  e.g. `NOT_FOUND` with `USER_NOT_FOUND: No such user.`

  | REST | gRPC |
  | ---- | ---- |
  | `400` | `INVALID_ARGUMENT` |
  | `401` | `UNAUTHENTICATED` |
  | `403` | `PERMISSION_DENIED` |
  | `404` | `NOT_FOUND` |
  | `409`, `422` | `FAILED_PRECONDITION` |
  | `429` | `RESOURCE_EXHAUSTED` |
  | `503 TASK_TIMEOUT` | `DEADLINE_EXCEEDED` |
  | other `503` | `UNAVAILABLE` |
  | `500` | `INTERNAL` |

  `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `INTERNAL` and `UNKNOWN` count for your circuit breaker. The
  Broker's own `UNAVAILABLE` (no ready instance, every circuit open) carries no
  `x-upstream-instance-id` header and should not count.

## Generating a Go client

Copy the file into your repository under `proto/` and generate into your own module with an `M`
option, which overrides the owner's `go_package`:

```sh
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest

M='Mtamagotchi/user_management/v1/user_management.proto=<your module>/gen/tamagotchi/user_management/v1;usermanagementv1'
protoc -I proto --go_out=gen --go_opt=paths=source_relative --go_opt="$M" \
  --go-grpc_out=gen --go-grpc_opt=paths=source_relative --go-grpc_opt="$M" \
  proto/tamagotchi/user_management/v1/user_management.proto
```

```go
conn, err := grpc.NewClient("message-broker:9091", grpc.WithTransportCredentials(insecure.NewCredentials()))
client := usermanagementv1.NewUserManagementInternalClient(conn)

ctx = metadata.AppendToOutgoingContext(ctx, "x-service-token", serviceToken, "x-correlation-id", correlationID)
reply, err := client.GetRelationships(ctx, &usermanagementv1.GetRelationshipsRequest{
	UserId: userID, OtherUserIds: []string{otherID},
})
```

Battle 4.0.0 calls `GetRelationships` exactly this way.
