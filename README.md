# eai-teleop-studio 部署与访问

该项目提供数据采集平台、遥操控制台、图像服务和数据同步工具。推荐部署用户为 `robot`，默认项目目录为：

```bash
/home/robot/eai-teleop-studio
```

如果部署到其他目录，例如 `/data/eai-teleop-studio`，优先只修改 systemd 服务里的 `WorkingDirectory`。Web 服务启动参数中的 `--config config/web_console.json` 和 `--log-dir logs` 会按当前项目目录解析。

## 目录约定

```text
config/     项目配置文件，例如 web_console.json、h2_pose_init.json
logs/       Web 服务、图像服务、同步工具日志
models/     模型文件或模型缓存
scripts/    安装、维护、同步脚本
systemd/    开机自启服务文件
teleop/     遥操、图像服务、机器人控制代码
teleop_web/ 数据采集平台后端和前端
robot_sync_tool/ 数据同步工具模块
```

项目内文件建议使用相对路径，例如：

```json
"init_arm_pose_file": "config/h2_pose_init.json"
```

数据保存目录建议使用绝对路径，例如：

```json
"data_dir": "/home/robot/data"
```

## Web 控制台部署

确认项目目录存在：

```bash
cd /home/robot/eai-teleop-studio
mkdir -p logs
```

安装 systemd 服务：

```bash
sudo cp systemd/xr-teleop-web.service /etc/systemd/system/xr-teleop-web.service
sudo systemctl daemon-reload
sudo systemctl enable xr-teleop-web.service
sudo systemctl restart xr-teleop-web.service
```

查看状态：

```bash
sudo systemctl status xr-teleop-web.service --no-pager
```

查看日志：

```bash
tail -f /home/robot/eai-teleop-studio/logs/teleop_$(date +%F).log
journalctl -u xr-teleop-web.service -f
```

默认访问地址：

```text
http://<机器人局域网 IP>:18088
```

例如：

```text
http://192.168.61.228:18088
```

## Web 服务启动参数

当前推荐的 systemd 启动方式：

```ini
WorkingDirectory=/home/robot/eai-teleop-studio
ExecStart=/home/robot/miniconda3/envs/teleop/bin/python3 -m teleop_web.server --host 0.0.0.0 --port 18088 --config config/web_console.json --log-dir logs
```

这里的 `config/web_console.json` 和 `logs` 都是相对于 `WorkingDirectory` 的路径。

如果项目部署到 `/data/eai-teleop-studio`，修改：

```ini
WorkingDirectory=/data/eai-teleop-studio
```

然后重新安装服务并重启：

```bash
sudo cp /data/eai-teleop-studio/systemd/xr-teleop-web.service /etc/systemd/system/xr-teleop-web.service
sudo systemctl daemon-reload
sudo systemctl restart xr-teleop-web.service
```

## 图像服务部署

安装图像服务：

```bash
cd /home/robot/eai-teleop-studio
sudo cp systemd/teleimager-camera-capture.service /etc/systemd/system/teleimager-camera-capture.service
sudo systemctl daemon-reload
sudo systemctl enable teleimager-camera-capture.service
sudo systemctl restart teleimager-camera-capture.service
```

查看状态和日志：

```bash
sudo systemctl status teleimager-camera-capture.service --no-pager
tail -f /home/robot/eai-teleop-studio/logs/teleimager-camera-capture.service.log
```

图像服务相机配置位于：

```text
teleop/teleimager/cam_config_server.yaml
```

WebRTC 相机流默认端口由相机配置决定，页面会从图像服务读取相机列表。

## 数据同步工具部署

同步工具作为独立模块保留在：

```text
robot_sync_tool/
```

日志统一写入项目的 `logs/` 目录。

如果使用模板服务文件，需要将 `@PROJECT_ROOT@` 替换为实际项目目录，或使用安装脚本：

```bash
cd /home/robot/eai-teleop-studio
bash scripts/robot_sync/install_autostart.sh
```

查看日志：

```bash
tail -f /home/robot/eai-teleop-studio/logs/robot-sync-tool.service.log
```

## 常用排查命令

查看 Web 服务是否运行：

```bash
ps -ef | grep '[t]eleop_web.server'
ss -lntp | grep 18088
```

查看是否仍有旧路径：

```bash
grep -RIn '/home/ubuntu\|eai_teleoperate_studio' config systemd teleop_web | head
```

检查配置文件：

```bash
cat /home/robot/eai-teleop-studio/config/web_console.json
```

检查当天业务日志：

```bash
tail -n 100 /home/robot/eai-teleop-studio/logs/teleop_$(date +%F).log
```

## 常见问题

### No module named teleop_web

通常是 systemd 的 `WorkingDirectory` 指向了没有代码的目录。确认：

```bash
systemctl cat xr-teleop-web.service
ls /home/robot/eai-teleop-studio/teleop_web
```

`WorkingDirectory` 必须指向项目根目录。

### Permission denied: /home/ubuntu

说明旧配置或旧进程还在使用 `/home/ubuntu`。处理：

```bash
grep -RIn '/home/ubuntu' /home/robot/eai-teleop-studio/config /home/robot/eai-teleop-studio/teleop_web
sudo systemctl restart xr-teleop-web.service
```

页面里的数据集目录建议填绝对路径：

```text
/home/robot/data
```

### 修改配置后页面仍报旧错误

配置文件修改后，运行中的 Web 进程不会自动重新读取，需要重启：

```bash
sudo systemctl restart xr-teleop-web.service
```

确认进程启动时间已经变化：

```bash
ps -eo pid,user,lstart,cmd | grep '[t]eleop_web.server'
```
