# Robot Sync Tool

独立的数据同步模块，用于从机器人采集机增量同步数据目录。

## 项目内结构

```text
robot_sync_tool/          # 后端服务、同步逻辑、独立前端页面
robot_sync_tool/static/   # 同步工具自己的前端界面
config/robot_sync*.json   # 同步配置
data/robot_sync*.json     # 同步记录
logs/robot_sync_*.log     # 运行日志
systemd/                  # 开机自启 service
scripts/robot_sync/       # 安装、卸载脚本
```

## 启动

```bash
python -m robot_sync_tool.main
```

默认访问：

```text
http://127.0.0.1:18090
```

安装开机自启：

```bash
sudo bash scripts/robot_sync/install_autostart.sh
```

卸载开机自启：

```bash
sudo bash scripts/robot_sync/uninstall_autostart.sh
```

