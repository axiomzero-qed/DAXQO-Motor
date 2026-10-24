# Installation

## Python tool / another harness

Python 3.10+ is required. Install the release from GitHub into a virtual environment:

```sh
python -m pip install "git+https://github.com/axiomzero-qed/DAXQO-Motor.git@v0.1.0"
daxqo-motor --adapter motor.demo:create_adapter doctor
```

Use the absolute Python path in the host's MCP configuration. See [ADAPTERS.md](ADAPTERS.md) and [examples/mcp-host.json](../examples/mcp-host.json). No Collector or DAXQO harness installation is required for the core/demo/custom backend path.

## Physical Android

1. Build the compatible bridge using [native/README.md](../native/README.md).
2. Select the correct Standard/XM application and a version code above the installed version.
3. The device owner installs the APK through Android's normal interface, enables the required accessibility/control permissions and pairs the bridge.
4. Check readiness with `daxqo-motor --serial DEVICE_SERIAL doctor` before starting a real task.

Python package installation does not modify Android permissions, an existing Linux runtime, model-provider configuration or installed APKs.

The Windows fixture builder accepts an explicit tools directory: `scripts/build-fixture.ps1 -ToolsRoot /path/to/android-tools`. Configure ADB through PATH, `--adb` or `DAXQO_ADB`.

Optional recording requires a separately installed Collector and an explicit shared Dataset root. Device acceptance scripts require deliberately selected serials and do not belong in automatic repository tests.
