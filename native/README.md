# Optional Android Motor bridge

Motor's Python core is independent of DAXQO's harness. Its default physical Android adapter requires the paired DAXQO bridge with Motor lease/event endpoints. Native builds currently extend a bridge already staged with the [Collector observer](https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector); recording can stay disabled.

## Reproducible source selection

1. Follow the Collector repository's `docs/INTEGRATION.md` to stage its observer against the pinned DAXQO v0.5 base.
2. Select that source explicitly:

```sh
python scripts/prepare_native.py --source /path/to/collector-staged-source --stage ./dist/native
```

The recorder in that selected source is reused. `--collector-recorder /explicit/TrajectoryRecorder.java` can deliberately override it; API signatures and hashes are checked and recorded. No sibling checkout is searched. Source anchors must match; the source checkout stays unchanged.

Windows build: `scripts/build-native.ps1 -Platform standard -Source /path/to/collector-staged-source -ToolsRoot /path/to/android-tools`. The tools layout is JDK 17 at `temurin-17/`, SDK at `android-sdk/` and Gradle 8.10.2 at `gradle-8.10.2/`. Use `-Platform xm` for XM. Other hosts can run Gradle on `dist/native/android`.

## Device protocol

| Route | Contract |
|---|---|
| GET `/api/motor/status` | Capabilities, current owner, generation, expiry and event sequence |
| POST `/api/motor/lease` | Acquire/renew with owner, expected package and TTL; renewals bind token/generation |
| POST `/api/motor/release` | Release only the matching token |
| POST `/api/motor/cancel` | Token-scoped cancellation, or explicit global stop without a token |
| GET `/api/motor/events?after=N&timeoutMs=T` | Bounded event wait (0-2000 ms) |
| POST `/api/device/execute` | Existing action format plus `motor:{lease_id,owner,generation}` |

Pairing, loopback authentication, original permissions/policy and action idempotency are retained. The executor checks ownership before actual dispatch. Native `tap_node` and `scroll_node` use verified nodes/snapshots; unknown delivery does not trigger automatic write replay.

The overlay adds fresh-tree capability reporting, package/window metadata and selected/checked/action semantics. Cancellation, expiry, foreground change or native touch-takeover events revoke ownership. Touch event availability does not cover every manual interaction; the owner's control switch remains authoritative.

Unit tests compile MotorGate/MotorEvents with a configured JDK. Optional overlay tests need `DAXQO_COLLECTOR_ANDROID_SOURCE` and `DAXQO_COLLECTOR_RECORDER`. Historical phone results are in `docs/ACCEPTANCE.md`; package installation alone does not install this bridge.
