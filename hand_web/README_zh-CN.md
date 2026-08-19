# 灵巧手调试工具

这是独立于 `teleop_web` 的临时控制工具。当前注册强脑 Revo2，页面和 API 不绑定品牌，后续可按同一接口增加因时及其他灵巧手。

## 启动

在项目根目录执行：

```bash
python -m hand_web.server
```

默认地址为 `http://127.0.0.1:18089`。监听地址、端口和设备默认参数在 `hand_web/config.json` 中配置，也可以使用 `--host`、`--port`、`--config` 和 `--log-dir` 覆盖。

## 服务器部署与开机自启

服务应运行在能够访问灵巧手的现场计算机上。USB 模式要求该计算机直接连接 Revo2；DDS 模式要求该计算机接入机器人 DDS 网络。普通云服务器没有 USB 或 DDS 链路时只能打开页面，无法控制设备。

部署整个项目到 `/home/robot/eai-teleop-studio`，确认 teleop 环境及强脑 SDK 可用：

```bash
cd /home/robot/eai-teleop-studio
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip install -r requirements.txt
/home/robot/miniconda3/envs/teleop/bin/python3 -c "import bc_stark_sdk; print('bc_stark_sdk OK')"
```

使用 USB 串口时，将服务用户加入串口设备组：

```bash
sudo usermod -aG dialout robot
getent group dialout
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

页面中的串口可留空以自动检测，也可以明确填写 `/dev/ttyUSB0` 或 `/dev/ttyACM0`。修改用户组后，新启动的 systemd 服务会获取新的组权限。

仓库提供 `systemd/hand-web.service`，配置方式与 `xr-teleop-web.service` 一致。安装脚本会替换服务文件中的 `@PROJECT_ROOT@`、创建日志目录并执行 `systemctl enable --now`：

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh hand-web.service
```

服务默认监听 `0.0.0.0:18089`。浏览器访问：

```text
http://<服务器局域网 IP>:18089/
```

如需覆盖监听地址或端口：

```bash
cp config/hand_web.env.example config/hand_web.env
```

编辑 `config/hand_web.env` 后重启服务：

```env
HAND_WEB_HOST=0.0.0.0
HAND_WEB_PORT=18089
```

设备默认通信方式、串口、设备 ID、安装侧及 DDS 参数继续在 `hand_web/config.json` 中配置。systemd 的 `--host` 和 `--port` 参数会覆盖其中的 Web 监听配置。

常用维护命令：

```bash
systemctl status hand-web.service --no-pager
systemctl is-enabled hand-web.service
systemctl is-active hand-web.service
sudo systemctl restart hand-web.service
sudo systemctl stop hand-web.service
journalctl -u hand-web.service -f
tail -f /home/robot/eai-teleop-studio/logs/app/hand-web.service.log
tail -f /home/robot/eai-teleop-studio/logs/system/hand_web_$(date +%F).log
```

日志规划与 `teleop_web` 保持一致：`logs/app/hand-web.service.log` 保存 systemd 进程标准输出及错误，`logs/system/hand_web_YYYY-MM-DD.log` 保存按日操作日志。操作日志使用与 `teleop_YYYY-MM-DD.log` 相同的时间、级别和 JSON 字段格式，记录服务启停、HTTP 请求、设备连接/断开、手势指令、停止指令及异常；正常的 `/api/status` 轮询不会反复写入日志，实时拖动指令按每秒最多一条采样记录。

取消开机自启：

```bash
sudo systemctl disable --now hand-web.service
```

健康检查：

```bash
curl -s http://127.0.0.1:18089/api/devices
curl -s http://127.0.0.1:18089/api/status
```

服务启动只提供 Web 页面，不会自动连接串口或发送手势。连接仍由页面操作触发。官方上位机、遥操与本工具不能同时控制同一只灵巧手；切换控制程序前应先在页面断开设备。

当前服务没有用户认证，只应部署在可信局域网或 VPN 中，不应将 `18089` 直接暴露到公网。

## 姿态预览

页面使用 `assets/brainco_hand` 中的左右手 URDF 和 STL 模型。预览代码位于 `hand_web/src/hand-preview.js`，修改后在 `hand_web` 目录执行：

```bash
npm install
npm run build
```

生成的 `hand_web/static/hand-preview.js` 已包含 Three.js 和 URDFLoader，运行页面时不需要访问外网。
页面使用原生 HTML、CSS 和 JavaScript，不依赖 Vue。`node_modules` 仅在重新构建预览脚本时需要，构建完成后可以删除。

## 强脑连接方式

- `USB / 官方 SDK`：需要安装 `bc_stark_sdk`，用于电脑通过串口直接连接 Revo2。
- `机器人 DDS`：需要安装 `unitree_sdk2py`，用于验证遥操使用的 BrainCo DDS 主题。

官方上位机、机器人遥操和本工具不能同时控制同一只灵巧手。USB 模式下应先关闭占用串口的官方上位机；机器人 DDS 服务已占用设备串口时，应选择 DDS 模式。

页面中的位置统一为 `0%` 张开、`100%` 闭合。Modbus 适配器负责转换为官方 SDK 的 `0-1000`，DDS 适配器负责发送 `0.0-1.0`。

## 增加设备

1. 在 `teleop/robot_control/devices/<device>/` 中实现底层设备 SDK。
2. 在 `hand_web/adapters/` 中实现 `capabilities/connect/disconnect/status/command/stop` 接口。
3. 在 `hand_web/core/registry.py` 中注册设备。

前端根据 `capabilities()` 动态生成设备、通信方式、连接字段、左右手和关节控件，不需要为新设备复制服务器或页面。临时姿态仅保存在浏览器内存中，不写入配置文件。
