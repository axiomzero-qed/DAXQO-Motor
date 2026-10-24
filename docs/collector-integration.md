# Optional Trajectory Collector integration

Separate repository: https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector

Motor core, standalone USB transport, default CLI/MCP and map operations work without the Collector Python package.

## Ownership

Motor owns exploration, navigation, maps and arbitration. Collector owns trajectory schema/privacy, durable storage, receiver cursor and ACK protocol. The device bridge owns physical permissions, pairing and device-wide leases. The harness owns task intent, cancellation and model configuration.

`--data` is the Motor map root. `--trajectory-data` is an explicit shared Dataset root. No directory under another checkout is inferred. Coordinate one ACK receiver per device stream.

## Modes

- Default MCP: no collection, status/feedback calls or Dataset.
- MCP `--trajectory-feedback`: attach public context to an existing bridge recording session; no start/stop/sync/ACK.
- CLI `scout --collect`: start only when no recording session is active, attach context, stop and sync into the selected Dataset.

Native action UUIDs correlate feedback. MCP request IDs only correlate responses. Caller verification remains `agent_claim`, not independent evidence.

The public schema is `daxqo.trajectory/0.1`. Install the Collector GitHub release before using Motor's optional `collector` extra until a package-index release is available.

The default Android native overlay currently builds on a bridge containing the native Collector observer, which may remain disabled. That device-runtime dependency is separate from the optional Python package. Custom adapters can use other device runtimes.
