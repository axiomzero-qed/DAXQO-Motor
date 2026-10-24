# Motor 验收

> 历史设备验收记录（2026-10-24），不是独立仓库版本在所有设备上的兼容承诺。原始数据和报告保留在本地，未随源码公开。独立安装与 MCP 验证见 [RELEASE-VALIDATION.md](RELEASE-VALIDATION.md)。

## 设备与部署

| 设备 | Android | Fixture | 原生 Motor overlay | Linux runtime / Engine |
| --- | --- | --- | --- | --- |
| Neo / StepX PZ261A (`NEO_SERIAL`) | 16 | `io.daxqo.motorfixture` 1.1 / code 2 | code 10 已安装 | `/root/.local/share/daxqo-motor/runtime`；Engine 重启就绪 |
| K70 / Xiaomi 23117RK66C (`XM_SERIAL`) | 16 | `io.daxqo.motorfixture` 1.1 / code 2 | code 10 已安装 | `/home/daxqo/.local/share/daxqo-motor/runtime`；Engine 重启就绪 |

每机 12 项 fixture checks 全通过。逐机报告：``acceptance-NEO_SERIAL.json`` (local report)、``acceptance-XM_SERIAL.json`` (local report)。覆盖 quick/deep 四至六标签页探索、返回恢复、长列表去重/边界、地图复用、危险与陈旧动作拒绝、弹窗核验、懒加载、无限列表预算、共享执行器、取消栅栏及失败样本保留。

USB 开发验收的 Deep Scout（Python 调度在电脑，UI 操作在真机）：

| 设备 | 耗时 | 标签页覆盖 | 去重内容 | 恢复 | Motor `cloud_calls` 指标 |
| --- | ---: | ---: | ---: | --- | ---: |
| Neo | 14.453 秒 | 6/6 | 48 | 精确恢复 | 0 |
| K70 | 17.594 秒 | 6/6 | 48 | 精确恢复 | 0 |

另测 quick 两机均 6/6 标签页并恢复。`cloud_calls=0` 是 Motor 本地指标；它不是模型提供方的请求计数器。

手机 PSS 是**完整 DAXQO 进程**：Neo 27,740 KB、K70 40,796 KB，不能解释为 Motor 单独开销。主机 Python 峰值内存包含 Dataset，不作为手机 Motor 单独内存数据。

## 回归和收集

最终完整自动回归：96 项，其中 95 项通过、1 项因 Windows 不支持而跳过。两台设备 Linux runtime 均通过额外 SIGTERM 释放测试；详见 [device-runtime.md](device-runtime.md)。System One 目前只有评分接口、schema 和测试；没有训练模型。

手机本机 Linux 的执行由真实 Harness 任务验证：两机 baseline、cold Motor、warm Motor 共六项任务均通过独立页面结果核验；warm 结果已更新，见 [BENCHMARK.md](BENCHMARK.md)。

两机真实 Harness stop 均在约 0.5 秒首次采样时释放租约，最终任务状态为 `stopped`；没有额外 cleanup。`stop_ack_status=running` 是停止请求确认时的中间状态。具体报告见 ``cancel-acceptance.json`` (local report)。

唯一轨迹接收根为 `<operator-selected-shared-dataset>`。Motor MCP 刷新当前 Collector task，把 task、候选动作、动作 UUID 和验证来源关联到轨迹；不启停采集、不 ACK。反馈默认来源为 `agent_claim`，不能当作独立验证标签。当前接收游标从共享 Dataset 数据库读取；640 仅是首次历史合并后的值。

Neo fixture APK 的 UI 安装路径和核验记录见 [INSTALLATION.md](INSTALLATION.md)。本次文件管理器操作没有被记录成轨迹：采集关闭且文件管理器不在采集白名单，因此不作为学习样本。

收尾时共享 Dataset 为 2,792 条记录、2 台设备、36 个任务，包含本项目与 Collector 项目的历史记录，不能全部计为 Motor 产出。6 条失败动作仍为失败；两个本次陈旧快照动作已逐条核验。导出 77 条合格样本（train 76 / test 1），未独立验证的成功动作不会自动变成正例。两机缓存与待写队列均为 0、采集关闭、租约释放、Harness Engine 在线且无活动任务。最终接收游标为 Neo 1327、K70 1819；详见 ``final-device-status.json`` (local report)。可运行 `scripts/finalize_acceptance.py --serial DEVICE_SERIAL --trajectory-data DATASET_ROOT` 在设备空闲时重新核验、同步并刷新汇总。

## 局限

每种设备/模式只有一个 benchmark 样本，不据此计算成功率。模型 API 调用次数与 Motor 工具调用次数均无法从 Harness feed 单独识别，应记为 `null`；触摸事件检测也不保证覆盖所有手动触摸。完整的 baseline、cold Motor、warm Motor 数据及统计口径见 [BENCHMARK.md](BENCHMARK.md)。

v0.1 自动探索依赖可用的无障碍树；自绘画布、语义缺失或不能验证的页面需要移交 DeepSeek 判断。尚未测量独立 Motor CPU/内存增量，也未在真实支付或私人聊天页面做业务验收。
