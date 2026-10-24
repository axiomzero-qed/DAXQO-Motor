# Harness and backend adapters

Motor's core has no harness or model-provider import. The Android adapter implements `motor.plugins.DeviceAdapter`.

| Member | Contract |
|---|---|
| `device_id` | Stable non-empty identity for map variants |
| `capabilities()` | Supported features and readiness |
| `acquire(owner, ttl_ms, package, previous=None)` | Acquire/renew an exclusive lease |
| `release(lease)` | Release only the matching lease |
| `cancel(reason, lease=None)` | Stop matching work; global stop is explicit |
| `read(lease=None, package="", context=None)` | Fresh `motor.models.State` |
| `events(sequence, timeout_ms=500)` | Bounded wait and sequence/generation |
| `perform(kind, args, ident, lease=None, context=None)` | Execute once under lease; return actual result |

Translate `tap_node`, `scroll_node` and `back` into your backend's protocol. Node actions bind `nodeId`/`snapshotId`; `expectedPackage` is the current foreground.

The backend must enforce device-wide ownership, lease expiry/generation, package matching and idempotency at the executor. A process-local lock is insufficient across independent harnesses. Report unknown delivery or unverifiable results; do not silently replay actions.

## Trusted factories

Expose `def create_adapter(**config)` from an installed module:

```sh
python -m motor.mcp_server --adapter my_backend:create_adapter --adapter-config /path/to/backend.json --map-db /path/to/map.sqlite3
```

The operator selects trusted executable code at startup. Tools cannot change the factory or load modules. An optional `close()` is called on shutdown. The bundled `motor.demo:create_adapter` is synthetic and unsuitable as a physical-device ownership implementation.

## Harness entry points

Use `Motor(adapter, store, sink=None)`, CLI, or stdio MCP. Supported MCP revisions are 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25. Attribution defaults to `mcp`; set `--source` for a specific harness. MCP cancellation, shutdown and `motor_cancel` reach the guarded engine.

`--harness-task-guard` and `scripts/install_linux.py --integrate-harness` are optional DAXQO-specific adapters. Other harnesses only configure the MCP process; they need no DAXQO server/model files.

## Optional Collector

Default CLI/MCP execution does not import Collector or create a trajectory dataset. Recording/sync/export require an explicit Collector installation and Dataset root. `--collector-root` can explicitly select development sources; no sibling directory is inferred.

Android perception uses a bundled, MIT-licensed snapshot of Collector's privacy policy. Its exact source version/hash is recorded in `motor/_vendor/PROVENANCE.json`; it does not import the Collector runtime. Update that snapshot explicitly with `scripts/update_privacy.py --collector-root PATH`.

Android MCP `--trajectory-feedback` needs the bridge endpoint, not the Collector Python package. A separate receiver owns synchronization/ACKs.

Protocol reference: https://modelcontextprotocol.io/docs/develop/connect-local-servers
