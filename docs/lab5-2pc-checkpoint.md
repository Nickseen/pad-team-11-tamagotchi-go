# Lab 5 two-phase commit checkpoint

All **11 scenarios passed** at 2026-11-27T00:02:35.131Z on CPR source commit `1c4e3662693f8466bf932ad57f3d4e0ebabf724e`.
The run used a disposable Compose project with synthetic data and the published images below.
The additional test configuration changed project-specific container addresses for isolation.

| Participant | Published image | Source commit |
| --- | --- | --- |
| Message Broker | `nickseen/tamagotchi-message-broker:5.0.1` | `100dd24f1cfd0b951d828a20101936fd0e3eb377` |
| Battle | `amzavladislav/tamagotchi-battle:5.1.0` | `84ac66ef8dc6240ee990abfae7f51febc72f9df4` |
| User Management | `amzavladislav/tamagotchi-user-management:5.1.1` | `e670cbc6690d24792efe765306c3962cc211c684` |
| Tamagotchi | `crislp/tamagotchi-tamagotchi:5.2.0` | `ae1b830eaec5c10a6ef6a2328075a1f91437f462` |
| Monster Raid | `nickseen/tamagotchi-monster-raid:5.0.0` | `a69760ee3e2a1cfbec9400ebc4f5064a73c858c6` |

[Detailed observations](lab5-2pc-runs/20261127T000235Z.md) · [JSON report](lab5-2pc-runs/20261127T000235Z.json) · [Exact running image IDs and registry digests](lab5-2pc-runs/20261127T000235Z-images.json)

## Reproduce

Set up a fresh Compose project as described in the [README](../README.md) using these versions. Keep the promotion scenario last so the original primary is available for the preceding scenarios.

```sh
docker compose up -d --wait user-management tamagotchi package-registry battle notification guild-1 guild-2 monster-raid-1 monster-raid-2 gateway
python3 scripts/tx/check-2pc.py --scenario 1,2,3,4,5,6,7,8,10,11,9 --allow-promotion
```

The [scenario guide](lab5-2pc-scenarios.md) describes the expected outcomes and database rejoin procedure.
