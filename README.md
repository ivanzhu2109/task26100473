# task26100473

VM 重启协调组件：仅处理运行中 VM 的重启（先停止再启动），进程在任意
SQLite 提交前后退出都可恢复，重启不多执行、uptime 不重复计算也不混入
停机时间。只依赖 Python 标准库与 SQLite，不监听任何端口。

## 结构

- `vmrest/clock.py` — 可注入整数秒时间源（`FileClock` 跨进程不倒退）
- `vmrest/device.py` — 最薄设备模型，状态持久化到独立设备 SQLite 文件；
  `apply(token, vm_id, target_state)` 首次原子保存目标状态/执行时间/执行
  计数/回执，重复 token 直接返回首次回执（不重新取时间、不再执行）
- `vmrest/control.py` — 控制侧协调组件，持久化到独立控制 SQLite 文件，
  提供 `restart` / `recover` / `inspect` / `check_consistency`
- `tests/child_worker.py` — 子进程入口，用 `os._exit` 在指定持久化边界
  强制退出（真实子进程崩溃注入，不依赖 sleep）
- `tests/test_restart.py` — 自动化测试

## 运行测试

```sh
python3 -m unittest discover -s tests -v
```

## 关键设计

- 两个 SQLite 文件（设备侧/控制侧）各自独立，无跨文件事务；设备命令
  token 为 `{vm_id}:{key}:stop|start`，设备侧幂等保证每个 key 恰好首次
  执行一次停止和一次启动。
- 控制侧把"回执记账 + 请求阶段推进"放在同一事务：停止回执记账后进入
  `starting`，启动回执记账后进入 `done` 并固化结果。崩溃后 `Recover()`
  按阶段重放设备 token 拿到首次回执（含原执行时间），继续记账，因此
  uptime 只按设备回执时间累计，不用恢复时间替代。
- 累计 uptime 保存在 `vm_state(uptime_base, running_since)`，与最近 100
  条变更记录（`events` 表）解耦：截断 events 不影响 uptime，也不影响
  `requests` 表中已固化的各 key 返回值。

## 恢复完成时如何判断控制侧与设备侧一致

`Recover()` 返回（无 pending 请求）后，调用
`Coordinator.check_consistency(vm_id)` 判定，条件全部成立即一致：

1. 控制侧无未完成请求（所有请求均为 `done`）；
2. 控制侧 `vm_state.state` 与设备 `read(vm_id).state` 相同；
3. 控制侧当前运行区间起点 `running_since` 等于设备侧最近一次状态变更
   时间 `since`；
4. 控制侧最近一条变更记录的 `to_state`/`executed_at` 与设备当前
   `state`/`since` 相同。
