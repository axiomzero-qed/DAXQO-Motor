# Standalone Linux runtime

Motor runs with Python 3.10+ and the standard library. Install the Python package and configure any stdio MCP host directly; no DAXQO agent server or Collector Python package is required.

An offline source-only bundle can be built with `python scripts/package_motor.py`. Add `--collector-root /explicit/collector/checkout` only when intentionally bundling the optional Collector. The bundle's manifest records source hashes.

To install a bundle into an existing Linux account:

```sh
python3 scripts/install_linux.py --source /path/to/extracted/bundle --runtime-root /path/to/motor/runtime
python3 /path/to/motor/runtime/run_motor_mcp.py --adapter motor.demo:create_adapter --map-db /path/to/map.sqlite3
```

The optional `--integrate-harness --harness-root /path/to/daxqo/backend` mode only adapts DAXQO's config writer. It backs up changes and preserves existing provider configuration. Other harnesses configure the process normally; the installer does not require their source directories.
