# DAXQO Motor

Local GUI exploration, verified navigation and persistent app maps for **any harness**.

Motor 将页面观察、有限探索、语义导航和动作核验封装为独立工具。支持 Python API、命令行、MCP 和可替换设备适配器，采集器为可选组件。

- Python 3.10+, standard-library-only core, MIT license.
- Persistent app maps, bounded exploration and observed action verification.
- Device leases, cancellation and escalation when verification is uncertain.
- No model provider or cloud service required.

## Install and try without a phone

```sh
python -m pip install "git+https://github.com/axiomzero-qed/DAXQO-Motor.git@v0.1.0"
daxqo-motor --adapter motor.demo:create_adapter doctor
daxqo-motor --adapter motor.demo:create_adapter --data ./data scout demo.app --page-only --max-depth 1
```

From a source checkout: `python -m pip install .`. Motor runs without Trajectory Collector or a sibling source directory. The demo is a **synthetic in-memory backend**, not a physical phone.

## Connect another harness over MCP

```sh
python -m motor.mcp_server --adapter motor.demo:create_adapter --map-db ./data/map.sqlite3 --source my_harness
```

Tools: `motor_map`, `motor_scout`, `motor_navigate`, `motor_reflex`, `motor_cancel`.

Replace the demo with Android or a trusted `module:factory`. Every entry point uses the same guarded Motor engine. See [host config](examples/mcp-host.json) and [adapter contract](docs/ADAPTERS.md).

## Physical Android adapter

A phone needs a compatible DAXQO Android bridge with Motor lease/event routes and owner-enabled control permissions. Pairing and Android permissions remain enforced. Another harness does not need DAXQO's agent server or model configuration.

On the phone's existing Linux:

```sh
python -m motor.mcp_server --state-dir /path/to/bridge/pairing-state --map-db ./data/map.sqlite3
```

From an authorized USB host:

```sh
daxqo-motor --adb /path/to/adb --serial DEVICE_SERIAL doctor
python -m motor.mcp_server --adb /path/to/adb --serial DEVICE_SERIAL --map-db ./data/map.sqlite3
```

ADB comes from `--adb`, `DAXQO_ADB`, PATH or Android SDK environment variables. `--android-package` selects another compatible bridge APK. Use `--device-id` with a stable UUID if the bridge has no identity endpoint.

[Native build instructions](native/README.md) use explicit compatible source paths. Python installation neither installs an APK nor enables phone permissions.

## Optional recording

Install [Collector](https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector) separately before using recording or the `collector` extra:

```sh
python -m pip install "git+https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector.git@v0.1.0"
daxqo-motor --serial DEVICE_SERIAL --trajectory-data /path/to/shared-dataset scout io.daxqo.motorfixture --collect
```

MCP defaults to no collection. `--trajectory-feedback` attaches context to an existing bridge recording session without starting/stopping it or ACKing records. CLI `--collect` explicitly owns a recording session and requires a Dataset root. Map and Dataset paths are separate. See [Collector integration](docs/collector-integration.md).

## Python API

```python
from pathlib import Path
from motor import Motor, MapStore
from motor.plugins import load_adapter

adapter = load_adapter("motor.demo:create_adapter")
with MapStore(Path("./data/map.sqlite3")) as store:
    result = Motor(adapter, store).scout("demo.app")
```

External adapters supply observations, truthful execution results and device-wide ownership enforcement. An optional recording sink can be passed to `Motor(adapter, store, sink=None)`.

## Development

```sh
python -m pip install ".[dev]"
python -m unittest discover -s tests
python -m build
python examples/mcp_smoke.py
```

The independent MCP client test uses the synthetic backend. Physical-device acceptance remains separate. Historical results: [ACCEPTANCE.md](docs/ACCEPTANCE.md), [BENCHMARK.md](docs/BENCHMARK.md).

Source releases exclude device data, credentials, generated reports, APKs and website sources.
