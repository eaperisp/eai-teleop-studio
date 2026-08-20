# 灵巧手调试工具

这是独立于 `teleop_web` 的临时控制工具。当前注册强脑 Revo2，页面和 API 不绑定品牌，后续可按同一接口增加因时及其他灵巧手。

## 启动

在项目根目录执行：

```bash
python -m hand_web.server
```

默认地址为 `http://127.0.0.1:18089`。灵巧手调试工具固定使用 `18089`，`18090` 专用于数据同步服务，服务端会拒绝其他端口。监听地址和设备默认参数在 `hand_web/config.json` 中配置，也可以使用 `--host`、`--config` 和 `--log-dir` 覆盖。

摄像头视觉控制是可选能力。在 `teleop` 环境中安装一次视觉依赖：

```bash
python -m pip install -r hand_web/requirements-vision.txt
```

`mediapipe` 会安装它匹配的 `opencv-contrib-python`，不要再同时安装 `opencv-python`，否则两个发行包会共同提供 `cv2` 并可能造成加载冲突。未安装视觉依赖时，设备连接、关节调试和三维预览不受影响。安装后可使用“仅识别”模式先验证摄像头与手势映射而不连接灵巧手。

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

使用服务器本机摄像头时，还需授予服务用户视频设备权限：

```bash
sudo usermod -aG video robot
getent group video
ls -l /dev/video* 2>/dev/null
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

如需覆盖监听地址：

```bash
cp config/hand_web.env.example config/hand_web.env
```

编辑 `config/hand_web.env` 后重启服务：

```env
HAND_WEB_HOST=0.0.0.0
```

设备默认通信方式、串口、设备 ID、安装侧及 DDS 参数继续在 `hand_web/config.json` 中配置。systemd 的 `--host` 参数会覆盖其中的 Web 监听地址，端口始终为 `18089`。

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

日志规划与 `teleop_web` 保持一致，且只使用项目根目录下的正式日志目录：`logs/app/hand-web.service.log` 保存 systemd 进程标准输出及错误，`logs/system/hand_web_YYYY-MM-DD.log` 保存按日操作日志。操作日志使用与 `teleop_YYYY-MM-DD.log` 相同的时间、级别和 JSON 字段格式，记录服务启停、HTTP 请求、设备连接/断开、手势指令、停止指令及异常；正常的 `/api/status` 和视觉帧轮询不会反复写入日志，实时拖动及视觉指令按每秒最多一条采样记录。

取消开机自启：

```bash
sudo systemctl disable --now hand-web.service
```

健康检查：

```bash
curl -s http://127.0.0.1:18089/api/devices
curl -s http://127.0.0.1:18089/api/status
curl -s http://127.0.0.1:18089/api/vision/status
```

服务启动只提供 Web 页面，不会自动连接串口或发送手势。连接仍由页面操作触发。官方上位机、遥操与本工具不能同时控制同一只灵巧手；切换控制程序前应先在页面断开设备。

同一项目目录启用了跨端口单实例锁。即使指定不同的 HTTP 端口，也不能同时启动两份 `hand_web`，以免两个页面争用同一串口。出现“已有灵巧手调试服务运行”时，应使用正在运行的页面或先停止旧进程，不要通过更换端口绕过检查。

视觉控制由 `hand_web` 进程直接打开服务器摄像头，浏览器只显示服务端返回的预览帧，因此部署到机器人后使用的是机器人上的 `/dev/video*`，不是访问页面那台电脑的摄像头。视觉控制启动后会独占本工具内的指令发送权；手动滑块、快捷姿态、断开设备均会锁定。停止视觉控制、手部持续丢失或线程异常时，服务会停止当前运动并释放控制权和摄像头。

页面默认勾选“仅识别（不控制）”。点击“启动摄像头”只会打开相机并显示识别结果，不发送灵巧手指令；确认识别正常后，取消勾选“仅识别（不控制）”，按钮会变为“启动视觉控制”。服务端还会校验显式控制确认字段，旧页面或不完整 API 请求不能意外进入控制模式。

视觉跟随优先使用 MediaPipe 世界坐标关键点，并将关键点转换到由腕部、食指、中指和小指建立的手掌局部坐标系。四指使用 MCP/PIP/DIP 综合弯曲角，拇指屈伸使用掌面法向运动与关节弯曲角，拇指侧摆使用手掌平面内的带方向角。设备重定向层再将通用人手特征映射到强脑的六个主动关节，因此后续增加其他品牌时可以复用跟踪、特征和滤波代码。

目标关节使用 One Euro 自适应滤波，并经过关节范围、最大变化速度、端点吸附和死区限制。BrainCo 重定向还会将拇指内收按 `thumb_flex_aux_coupling` 比例联动到拇指屈伸，适配 Revo2 贴掌时需要两个主动关节协同的结构。页面“跟踪坐标”显示“世界坐标”时表示正在使用三维世界关键点；显示“图像坐标”表示 MediaPipe 未提供世界坐标，系统已回退到归一化图像关键点。

### 视觉标定

视觉标定只能在“仅识别”模式下进行，不会向灵巧手发送指令：

1. 启动仅识别摄像头，确认页面显示“世界坐标”。
2. 点击“视觉标定”。
3. 依次保持完全张开、完全握拳、拇指最大外展、拇指最大内收，并点击对应的“采集”。
4. 四项采集完成且动作范围有效后，标定自动保存并立即应用。

标定按设备和左右手保存在 `hand_web/vision_calibration.json`，属于运行数据且已从 Git 排除。部署迁移时应和 `hand_web/poses.json` 一起备份。标定动作幅度不足时不会覆盖原有标定，可以重新采集对应步骤；“恢复默认标定”会删除当前设备和安装侧的个人标定。

相关接口：

```text
GET  /api/vision/calibration?device_id=brainco_revo2&side=right
POST /api/vision/calibration/capture
POST /api/vision/calibration/reset
```

当前服务没有用户认证，只应部署在可信局域网或 VPN 中，不应将 `18089` 直接暴露到公网。

## 姿态库

页面支持新增、编辑、删除和载入姿态。每个姿态包含英文名称、中文描述和该设备完整的关节位置；新增时默认采用页面当前的“设置”值。载入姿态只更新设置值和三维预览，不会立即驱动设备，确认无误后再点击“执行姿态”。默认的张开、半握、握合与用户新增姿态使用同一套管理方式。

姿态按设备型号保存在 `hand_web/poses.json`，首次修改姿态库时自动创建。该文件属于运行数据，已从 Git 排除；迁移服务器或备份项目时应单独备份它。服务写入时先生成 `hand_web/poses.json.tmp` 再原子替换，避免写入中断留下半份数据。

相关接口：

```text
GET  /api/poses?device_id=brainco_revo2
POST /api/poses/save
POST /api/poses/delete
```

关节位置区域同时显示“设置”和“当前”：设置值是下一次执行姿态要发送的目标，当前值来自设备反馈。两者不同不代表异常，执行过程中当前值会逐步接近设置值。

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
4. 如需视觉控制，在 `hand_web/vision/retargeters/` 中增加通用人手特征到设备关节的重定向器，并在 `hand_web/vision/retargeters/__init__.py` 注册。

前端根据 `capabilities()` 动态生成设备、通信方式、连接字段、左右手和关节控件，不需要为新设备复制服务器或页面。姿态库按设备 ID 分开保存，新增其他品牌时不需要复制姿态管理代码。
