# Public release validation (2026-10-24)

- Core regression: 100 tests on Python 3.12 / Windows; 99 passed, one POSIX signal test skipped on Windows.
- Configured JDK/native-source checks: MotorGate/MotorEvents behavior, snapshot freshness, exact overlay anchors and the pinned bridge source were included.
- Independent wheel installation: Motor installed into a fresh environment containing no Collector package. Android tree parsing/privacy checks worked without Collector.
- External client: official MCP Python SDK 1.30.0 negotiated 2025-11-25, discovered all five tools and verified synthetic click/back outcomes from a temporary working directory.
- Source staging: Collector overlay against DAXQO commit `3de65f9e9751c67d7118e72e789f898901a50ce6`, followed by the Motor overlay with explicitly selected recorder source, succeeded.
- Both wheel and source distribution built in isolated build environments.

These release checks did not reinstall APKs, change device permissions or run new physical-phone/model tasks. Historical real-device measurements remain separately labeled.

Motor includes a fixed MIT-licensed snapshot of Collector's privacy rules so Android perception remains protected without the optional Collector package. `motor/_vendor/PROVENANCE.json` records the source/version/hash. Updating it requires an explicitly selected source through `scripts/update_privacy.py`.

Repository CI repeats Python regression, package builds and the external MCP client test on Linux/Windows with Python 3.10/3.12.
