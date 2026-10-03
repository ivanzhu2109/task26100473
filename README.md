# VM 重启协调组件

只用 Python 标准库 + SQLite。控制状态与设备状态分别持久化到两个独立
SQLite 文件，无跨文件事务，无可恢复信息存放在内存对象中。

## 结构

- `vmcoord/device.py` — 最薄设备模型。`apply(token, vm_id, target_state)`
  首次执行在单个事务中原子保存目标状态、执行时间、执行计数与回执；
  重复 token 返回首次回执，不重新执行、不重新取时间。`read(vm_id)` 读当前状态。
- `vmcoord/control.py` — 协调组件。`restart(vm_id, key)` / `recover()` /
  `inspect(vm_id)`。每次重启派生 `stop:{key}`、`start:{key}` 两个设备 token；
  设备侧靠 token 幂等，控制侧靠 requests 表进度标志在单事务内记账。
- `vmcoord/clock.py` — 可注入时间源（文件时钟），跨进程重启不倒退。
- `vmcoord/fault.py` — 故障注入点（`VM_FAULT_AT` 环境变量 + `os._exit`），仅供测试。
- `vmcoord/worker.py` — 子进程入口，测试用它制造真实进程崩溃。

## 运行测试

```
python3 -m unittest discover -s tests -v
```

## 恢复完成时如何判断一致

Recover 把控制库中所有 `pending` 请求推进到 `completed`：每一步先向设备
重放同一 token（设备已执行则返回持久化的首次回执，不会重复执行），再按
回执中的 `exec_time` 在控制侧单事务记账。恢复完成后一致性判据：

1. 控制侧 `vms.state` == 设备 `read(vm_id)` == `running`；
2. 控制侧 `running_since` == 设备 `start:{key}` 回执中的 `exec_time`；
3. 设备执行计数恰好 +2（一次停止、一次启动），控制侧 `changes` 恰好新增
   对应两条记录。

uptime 只累计设备实际 running 的区间：停止时按设备回执时间结算
`uptime_accum += stop_time - running_since`，启动时只记录新的
`running_since = start_time`；查询时若处于 running 再加上
`now - running_since`。恢复时刻本身从不参与计算。
