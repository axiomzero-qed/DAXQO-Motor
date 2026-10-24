# 双机任务对比

> 历史设备验收记录（2026-10-24），不是独立仓库版本在所有设备上的兼容承诺。原始数据和报告保留在本地，未随源码公开。独立安装与 MCP 验证见 [RELEASE-VALIDATION.md](RELEASE-VALIDATION.md)。

数据来自 Harness 的真实 DeepSeek 任务 feed；同一 fixture 任务分别运行 Android 基线、cold Motor、warm Motor。每格仅一次，六个任务 oracle 均通过；这些样本不构成总体成功率统计。

| 设备 | 模式 | 成功 / oracle | 秒 | Token usage | Android MCP 调用 | 已观察模型响应 | Motor tool calls | model API calls |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Neo | baseline | pass | 27 | 217,820 | 14 | 15 | null | null |
| Neo | cold Motor | pass | 26 | 51,625 | 0 | 4 | null | null |
| Neo | warm Motor | pass | 14 | 51,241 | 0 | 4 | null | null |
| K70 | baseline | pass | 22 | 170,940 | 12 | 13 | null | null |
| K70 | cold Motor | pass | 26 | 58,221 | 1 | 5 | null | null |
| K70 | warm Motor | pass | 17 | 46,867 | 0 | 4 | null | null |

`Token usage` 含缓存上下文计数，不等于付费 token。Harness feed 没有 provider 请求计数器，因此 `model API calls=null`；feed 也无法独立识别 Motor 工具调用，故 `Motor tool calls=null`。Android MCP 调用和已观察模型响应均不是云 API 调用数。不要由这些数字推导费用或性能百分比。该样本里 cold Motor 并非总更快；两台设备 warm 样本用时更短。

首次 cold Motor 尝试因 Harness 约 2,000-token 结果摘要限制而被截断，模型随后停止；失败报告保存在 `dist/attempts/`。之后将 Motor 摘要限制在约 3,000 字符内并优先保留任务页信息，再完成通过 oracle 的任务。原始逐任务报告：``benchmark-NEO_SERIAL-baseline.json`` (local report)、``benchmark-NEO_SERIAL-motor.json`` (local report)、``benchmark-NEO_SERIAL-warm.json`` (local report)、``benchmark-XM_SERIAL-baseline.json`` (local report)、``benchmark-XM_SERIAL-motor.json`` (local report)、``benchmark-XM_SERIAL-warm.json`` (local report)。
