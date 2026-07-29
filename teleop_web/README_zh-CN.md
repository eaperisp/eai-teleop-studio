# XR 数采控制�?
这是一个直接连�?`teleop/teleop_hand_and_arm.py` 的单机本�?Web 管理页面。页面包含“当前设备信息”和“数据采集”：当前设备只有一台，修改后会立即覆盖本地配置文件；设备配置会转换为启动参数。任务记录任�?ID、任务名称、实际采集进度、状态和创建时间。任务英文名作为数据目录名，中文描述写入任务元数据及每个 episode 的描述字段�?
## 启动

先进入项目原�?Python 环境，然后在仓库根目录执行：

```bash
python -m teleop_web.server --host 0.0.0.0 --port 8080
```

浏览器打开 `http://<控制电脑IP>:8080`。服务默认只监听 `127.0.0.1`；只有需要从局域网其他设备访问时才�?`--host 0.0.0.0`。当前页面没有用户认证，请勿直接暴露到公网�?
默认数据目录�?`~/xr_teleoperate/data/datasets/robot`，可覆盖�?
```bash
python -m teleop_web.server --dataset-dir /mnt/data/datasets/robot
```

当前设备信息默认保存�?`~/.config/xr_teleoperate/web_console.json`。页面点击“保存设备配置”会原子更新这个文件。也可指定其他配置文件：

```bash
python -m teleop_web.server --config /home/unitree/.config/xr_teleoperate/web_console.json
```

数采任务清单单独保存�?`~/xr_teleoperate/data/datasets/robot/tasks.json`。创建任务后会立即写入任�?ID、英文任务名、中文描述、目标采集条数、已完成条数、进度百分比、状态和创建时间；episode 保存完成后会自动同步最新进度。服务重启时从该文件恢复任务列表。可另行指定�?
```bash
python -m teleop_web.server --task-file /home/unitree/xr_teleoperate/data/datasets/robot/tasks.json
```

如需用指定虚拟环境启动遥操子进程�?
```bash
XR_TELEOP_PYTHON=/home/unitree/xr_teleoperate/.venv/bin/python \
python -m teleop_web.server --host 0.0.0.0
```

先在“当前设备信息”中填写并保存本机配置，再到“数据采集”创建任务。所有任务自动使用当前设备，不需要选择设备。页面创�?`h2_pico_inspire_dfx_test` 后，数据及元数据位于�?
```text
~/xr_teleoperate/data/datasets/robot/h2_pico_inspire_dfx_test/
├── task.json
└── episode_.../
```

后端固定启用 `--record --ipc`。页面的“开始遥操”“开始录�?保存本段”“安全停止”分别调用现�?IPC 命令 `R`、`S`、`Q`。关闭网页不会停止正在运行的遥操进程；应使用页面的“安全停止”�?
同一任务可以跨多次启动续录。安全停止后，下次直接从任务列表点击“开始采集”，后台会保留已�?episode，并从最大编号的下一条继续。例如目录中已有 `episode_0001` �?`episode_0010`，下一条会写入 `episode_0011`，不会覆盖前十条。采集进度只统计已经结束并成功写�?`data.json` �?episode，正在录制的半成品不会提前计数�?
## 归档数据�?
任务安全停止后，可以在任务列表点击“归档数据集”。系统将完整任务目录压缩为带日期时间�?ZIP，并记录最近归档时间和路径。正在运行的任务以及没有完整 episode 的任务不能归档�?
归档文件默认保存在：

```text
~/xr_teleoperate/data/datasets/robot/archives/<任务�?_YYYYMMDD_HHMMSS.zip
```

## 开机自�?
仓库提供 `teleop_web/xr-teleop-web.service`。该服务只启�?Web 数采控制台，不会自动启动机械臂遥操或录制任务。安装前确认 conda 环境 Python 位于 `/home/unitree/anaconda3/envs/tv/bin/python`�?
```bash
cd /home/unitree/xr_teleoperate
sudo install -m 0644 teleop_web/xr-teleop-web.service /etc/systemd/system/xr-teleop-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now xr-teleop-web.service
sudo systemctl status xr-teleop-web.service --no-pager
```

查看日志�?
```bash
journalctl -u xr-teleop-web.service -f
```

重启或停�?Web 服务前，应先在页面中安全停止正在运行的遥操任务�?
## 左右手异构末端执行器

页面支持分别选择左手和右手末端执行器。左右相同的主动末端执行器会继续使用兼容参数 `--ee=<类型>`；左右不同的场景会生�?`--left-ee=<类型> --right-ee=<类型>`�?
当前已支持的异构安全组合是“单侧因时灵巧手 + 另一侧被动件”，例如�?
```bash
--left-ee=rubber --right-ee=inspire_dfx
```

页面只保留两个被动末端选项：`none` 表示不控制，`rubber` 表示橡胶手。二者都不会主动发送末端执行器控制命令。录制数据中会在 `left_ee.type` / `right_ee.type` 记录选择结果；被动侧�?`qpos` 为空数组�?
## 日志与实际启动命�?
数采平台会按天保存本地日志，默认目录�?
```text
~/xr_teleoperate/logs/
```

当天日志文件类似�?
```text
~/xr_teleoperate/logs/teleop_2026-06-30.log
```

可以实时查看�?
```bash
tail -f ~/xr_teleoperate/logs/teleop_$(date +%F).log
```

日志中会记录�?
- Web 服务启动/停止
- 设备配置保存
- 创建数采任务
- 实际执行�?`teleop_hand_and_arm.py` 完整命令
- 子进�?PID
- 页面按钮控制动作：开始遥操、开�?结束录制、安全停�?- 遥操子进�?stdout/stderr 输出
- 启动失败和接口异�?
如果要指定日志目录：

```bash
python -m teleop_web.server --log-dir /home/unitree/xr_teleoperate/logs
```

页面“数采控制”弹窗中的“启动命令”也会显示当前任务实际运行命令；接口 `/api/state` 会返�?`process.command`、`log_dir` �?`log_file`�?