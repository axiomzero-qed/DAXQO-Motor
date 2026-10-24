# 设备 Linux 运行时

> 历史设备验收记录（2026-10-24），不是独立仓库版本在所有设备上的兼容承诺。原始数据和报告保留在本地，未随源码公开。独立安装与 MCP 验证见 [RELEASE-VALIDATION.md](RELEASE-VALIDATION.md)。

两台设备现均已部署 Motor Linux runtime，Harness Engine 已重启就绪；部署报告中的 `restart_ready=true`。两机部署均通过 Linux SIGTERM 子进程租约释放测试（Windows 对应测试跳过）。报告：``deployment.json`` (local report)。

| 设备 | runtime | 部署 / Engine | Linux SIGTERM 释放 |
| --- | --- | --- | --- |
| Neo `NEO_SERIAL` | `/root/.local/share/daxqo-motor/runtime` | 成功 / ready | 通过 |
| K70 `XM_SERIAL` | `/home/daxqo/.local/share/daxqo-motor/runtime` | 成功 / ready | 通过 |

Motor 任务守卫在请求到达时绑定唯一的 queued/running Harness task ID，只读 `harness.sqlite3` 中该任务的状态和 `stop_requested`；若活动任务为零个或多个则拒绝自动操作。每次动作和租约 acquire 前后均检查守卫，避免漏转发 MCP 取消以及旧请求停止新任务。守卫不读取任务 prompt。

真实 Harness 取消验收中，两机都在首次约 0.5 秒采样时已释放租约，最终状态为 `stopped`，无需额外 cleanup；完整记录见 ``cancel-acceptance.json`` (local report)。

本次部署同步的组件 SHA：Recorder `195783...`、Collector privacy `8ed2...`、dataset `6a83...`。完整摘要以部署与验收产物为准。
