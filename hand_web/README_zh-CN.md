# 灵巧手调试工具

这是独立于 `teleop_web` 的临时控制工具。当前注册强脑 Revo2、因时 DFX 和因时 FTP；页面、姿态库、视觉识别与设备驱动分层，增加新型号不需要复制 Web 服务。

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

服务应运行在能够访问灵巧手的现场计算机上。强脑 USB 模式要求该计算机直接连接 Revo2；DDS 模式要求该计算机接入机器人 DDS 网络。因时 DFX 还需要 `inspire_h1` 串口转 DDS 服务。普通云服务器没有 USB 或 DDS 链路时只能打开页面，无法控制设备。

以下示例约定：

```text
部署用户      robot
项目目录      /home/robot/eai-teleop-studio
Python 环境   /home/robot/miniconda3/envs/teleop
Web 端口      18089
```

### 首次部署

部署整个项目后，必须把依赖安装到 systemd 实际使用的 `teleop` Python 中。不要只根据当前 Shell 的环境判断是否安装成功：

```bash
cd /home/robot/eai-teleop-studio
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip install -r requirements.txt
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip show bc-stark-sdk
/home/robot/miniconda3/envs/teleop/bin/python3 -c \
  "import bc_stark_sdk.main_mod as sdk; print('BrainCo SDK OK', sdk.__version__)"
```

`requirements.txt` 当前固定使用 `bc-stark-sdk==2.0.3`。如果出现“当前 Python 环境未安装官方 bc_stark_sdk”，先检查报错中的 Python 路径是否为上述路径，再使用该路径执行安装命令。

视觉控制按需安装：

```bash
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip install \
  -r hand_web/requirements-vision.txt
```

使用 USB 串口时，将服务用户加入 `dialout` 组：

```bash
sudo usermod -aG dialout robot
id robot
```

`id robot` 的结果应包含 `dialout`。默认的浏览器摄像头由访问页面的电脑打开，不需要服务器 `video` 权限；只有显式选择“服务器摄像头”时才需要执行 `sudo usermod -aG video robot`。修改用户组后必须重启 systemd 服务，已经运行的进程不会自动获得新权限。

仓库提供 `systemd/hand-web.service`，配置方式与 `xr-teleop-web.service` 一致。安装脚本会替换服务文件中的 `@PROJECT_ROOT@`、创建日志目录并设置开机自启：

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh hand-web.service
sudo systemctl restart hand-web.service
```

确认服务使用了预期的用户、Python 和端口：

```bash
systemctl show hand-web.service -p User -p Group -p ExecStart
systemctl status hand-web.service --no-pager
curl -s http://127.0.0.1:18089/api/status
```

服务默认监听 `0.0.0.0:18089`。浏览器访问：

```text
http://<服务器局域网 IP>:18089/
```

HTTP 可以用于设备连接、关节控制和姿态管理，但浏览器不允许远程 HTTP 页面调用访问者电脑的摄像头。需要浏览器视觉控制且不启用 HTTPS 时，在访问页面的电脑上建立 SSH 端口转发：

```bash
ssh -L 18089:127.0.0.1:18089 robot@<服务器局域网 IP>
```

保持该 SSH 会话运行，再访问 `http://127.0.0.1:18089/`。浏览器会把它视为本机安全来源，摄像头仍来自访问页面的电脑，API 请求则通过 SSH 隧道到达服务器。也可以使用服务内置的可选 `HAND_WEB_SCHEME=https`、`HAND_WEB_CERT` 和 `HAND_WEB_KEY` 配置直接启用 HTTPS。

### 查找强脑 USB 串口

`lsusb` 显示的是 USB 总线号和设备号，不是页面需要填写的串口号。例如：

```text
Bus 003 Device 006: ID 0403:6010 FTDI FT2232 Dual UART/FIFO
```

该设备会创建两个串口接口。使用以下命令查看映射：

```bash
lsusb -d 0403:6010
ls -l /dev/serial/by-id/ 2>/dev/null
udevadm info -q property -n /dev/ttyUSB0 | grep -E 'DEVNAME|ID_USB_INTERFACE_NUM|ID_PATH'
udevadm info -q property -n /dev/ttyUSB1 | grep -E 'DEVNAME|ID_USB_INTERFACE_NUM|ID_PATH'
```

典型映射如下：

```text
FT2232 通道 A，if00 -> /dev/ttyUSB0
FT2232 通道 B，if01 -> /dev/ttyUSB1
```

Revo2 通常使用通道 B，因此可优先填写 `/dev/ttyUSB1`；也可以让页面串口字段留空，由服务依次读取设备信息并自动选择。不要把 `Bus 003 Device 006`、`003` 或 `006` 填入串口字段。若存在稳定链接，生产部署可填写：

```text
/dev/serial/by-id/usb-FTDI_Dual_RS232-HS-if01-port0
```

不同转接器的名称可能不同，以服务器实际的 `/dev/serial/by-id/` 输出为准。判断最终端口应以连接成功日志为准：

```bash
grep -E 'Revo2 已通过|连接 Revo2 失败' \
  /home/robot/eai-teleop-studio/logs/system/hand_web_$(date +%F).log | tail -n 20
```

检查设备权限和占用情况：

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
sudo -u robot test -r /dev/ttyUSB1 && echo readable
sudo -u robot test -w /dev/ttyUSB1 && echo writable
sudo fuser -v /dev/ttyUSB0 /dev/ttyUSB1
```

设备一般属于 `root:dialout` 且权限为 `0660`。若读写检查没有输出，先修复 `dialout` 组权限并重启服务。若 `fuser` 显示官方上位机、遥操或其他进程正在占用串口，应先正常退出占用程序。

### 更新已有部署

更新前备份不会提交到 Git 的姿态和个人视觉标定：

```bash
cd /home/robot/eai-teleop-studio
mkdir -p /home/robot/hand-web-backup
cp -a hand_web/poses.json hand_web/vision_calibration.json \
  /home/robot/hand-web-backup/ 2>/dev/null || true
```

在原目录通过 Git 更新时：

```bash
cd /home/robot/eai-teleop-studio
git pull --ff-only
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip install -r requirements.txt
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip install \
  -r hand_web/requirements-vision.txt
sudo bash scripts/install_autostart_services.sh hand-web.service
sudo systemctl restart hand-web.service
```

重新安装服务文件后仍需显式执行 `systemctl restart`，因为 `enable --now` 不会重启一个已经处于运行状态的服务。不要使用会删除未跟踪文件的清理命令，否则可能删除 `hand_web/poses.json` 和 `hand_web/vision_calibration.json`。

更新完成后验证：

```bash
/home/robot/miniconda3/envs/teleop/bin/python3 -m pip check
systemctl is-enabled hand-web.service
systemctl is-active hand-web.service
curl -s http://127.0.0.1:18089/api/devices
curl -s http://127.0.0.1:18089/api/status
tail -n 100 /home/robot/eai-teleop-studio/logs/app/hand-web.service.log
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

强脑 Revo2 切换到 DDS 通信方式时，页面默认网卡为 `enp86s0`。如果目标机器的机器人网卡名称不同，应修改 `devices.brainco_revo2.dds.network_interface`，可使用 `ip -br link` 查看实际网卡名称。

因时 DFX/FTP 的默认网卡同样是 `enp86s0`，分别保存在 `devices.inspire_dfx.dds` 和 `devices.inspire_ftp.dds`。视觉阈值保存在 `vision.devices.<device_id>`，个人标定仍按设备型号和左右手写入 `vision_calibration.json`，不会把强脑阈值套到因时手上。

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

### 常见连接错误

| 日志信息 | 原因 | 处理方式 |
| --- | --- | --- |
| `当前 Python 环境未安装官方 bc_stark_sdk` | SDK 没有安装到服务使用的 `teleop` Python | 使用 `/home/robot/miniconda3/envs/teleop/bin/python3 -m pip install -r requirements.txt` |
| `Failed to open Modbus context: Permission denied` | `robot` 不在 `dialout`，或服务尚未重启获取新组权限 | `sudo usermod -aG dialout robot` 后重启 `hand-web.service` |
| `read_holding_registers ... timeout` | 选错 FT2232 通道、设备 ID 不正确、设备未供电或链路异常 | 优先测试 `/dev/ttyUSB1`，再测试 `/dev/ttyUSB0`，Revo2 默认设备 ID 为 `127` |
| `Device or resource busy` / `拒绝访问` | 串口被官方上位机、遥操或另一份调试服务占用 | 使用 `fuser` 查找占用者，正常停止后再连接 |
| 页面可访问但无法控制 | Web 服务正常不代表 USB 或 DDS 链路正常 | 检查 `/api/status`、设备节点、用户组和业务日志 |
| 浏览器提示需要 HTTPS | 通过远程 HTTP 地址访问，浏览器拒绝摄像头 API | 使用 HTTPS，或通过 SSH 转发访问 `http://127.0.0.1:18089` |
| 浏览器拒绝摄像头 | 页面权限被禁止，或访问页面的电脑没有可用摄像头 | 在浏览器站点权限中允许摄像头 |
| 服务器摄像头画面获取失败 | 显式选择了服务器来源，但编号错误、无 `video` 权限或设备被占用 | 检查 `v4l2-ctl --list-devices`、`id robot` 和 `fuser /dev/video*` |

服务启动只提供 Web 页面，不会自动连接串口或发送手势。连接仍由页面操作触发。官方上位机、遥操与本工具不能同时控制同一只灵巧手；切换控制程序前应先在页面断开设备。

同一项目目录启用了跨端口单实例锁。即使指定不同的 HTTP 端口，也不能同时启动两份 `hand_web`，以免两个页面争用同一串口。出现“已有灵巧手调试服务运行”时，应使用正在运行的页面或先停止旧进程，不要通过更换端口绕过检查。

视觉控制默认由浏览器通过 `getUserMedia` 打开访问页面这台电脑的摄像头，按比例压缩为 JPEG 后上传到 `hand_web`，服务端继续负责 MediaPipe 识别、标定、关节映射、滤波和设备控制。上传队列只保留最新画面，不会因网络抖动积压旧帧；页面关闭、停止上传或手部持续丢失时，服务会停止当前运动并释放控制权。

页面保留“服务器摄像头”来源，用于确实需要读取机器人 `/dev/video*` 的现场模式。该模式必须选择 YUYV/MJPG 彩色节点，不能选择 Z16 深度节点；如果 `teleimager.image_server` 已占用彩色节点，不应同时打开同一设备。Orbbec 的 `/dev/video0` 常为深度流，不能仅凭最小编号判断摄像头用途。

页面默认勾选“仅识别（不控制）”。点击“启动摄像头”只会打开相机并显示识别结果，不发送灵巧手指令；确认识别正常后，取消勾选“仅识别（不控制）”，按钮会变为“启动视觉控制”。服务端还会校验显式控制确认字段，旧页面或不完整 API 请求不能意外进入控制模式。

视觉跟随优先使用 MediaPipe 世界坐标关键点，并将关键点转换到由腕部、食指、中指和小指建立的手掌局部坐标系。四指使用 MCP/PIP/DIP 综合弯曲角，拇指屈伸使用掌面法向运动与关节弯曲角，拇指侧摆使用手掌平面内的带方向角。设备重定向层再将通用人手特征映射到统一的六关节语义，强脑与因时只在适配器内部处理顺序、方向和量程。

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

## 因时连接方式

- `因时 DFX`：使用聚合主题 `rt/inspire/cmd`、`rt/inspire/state`。物理串口由官方 `inspire_h1` 服务独占，Web 工具只连接 DDS。
- `因时 FTP`：使用左右手独立主题 `rt/inspire_hand/ctrl/l|r`、`rt/inspire_hand/state/l|r`，消息角度范围为 `0-1000`。

页面对两种因时协议都使用“大拇指弯曲、大拇指旋转、食指、中指、无名指、小指”的顺序和 `0%` 张开、`100%` 闭合。适配器会转换因时硬件的 `[小指, 无名指, 中指, 食指, 拇指弯曲, 拇指旋转]` 顺序及相反的开合方向。

`inspire_h1` 必须运行在真正连接因时串口的计算机上，通常是机器人 PC2，不要求与 `hand-web.service` 同机。两台机器只要位于同一 DDS 网络、Domain 均为 `0`，Web 服务器就能通过 `enp86s0` 收发主题。Web 服务器看不到 `/dev/ttyUSB*` 并不代表手未连接，应到 PC2 检查串口；但 `rt/inspire/state` 没有反馈时，页面不会开放关节执行按钮。

### DFX 串口桥首次部署

以下操作在实际连接因时灵巧手串口的计算机上执行，而不是默认在 Web 服务器上执行。官方源码编译后，服务程序位于 `/home/robot/DFX_inspire_service/build/inspire_h1`。

先确认系统能够识别 USB 转串口：

```bash
lsusb
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
ls -l /dev/serial/by-id /dev/serial/by-path 2>/dev/null
```

Ubuntu 22.04 可能将 `1a86:7523` CH340 错认成盲文设备。若 `lsusb` 能看到 CH340，但没有 `/dev/ttyUSB*`，并且内核日志包含 `brltty` 和 `converter now disconnected`，执行：

```bash
journalctl -k -b --no-pager | grep -iE 'ch341|ttyUSB|brltty'
sudo systemctl disable --now brltty.service brltty-udev.service
sudo systemctl mask brltty.service brltty-udev.service
sudo apt purge -y brltty
```

完成后重新插拔 USB 转串口，再次确认出现 `/dev/ttyUSB0`。CH340 通常没有 USB 序列号，因此 `/dev/serial/by-id` 可能为空；此时优先使用 `/dev/serial/by-path`，最后才使用可能随插拔顺序变化的 `/dev/ttyUSB0`。

安装编译依赖：

```bash
sudo apt update
sudo apt install -y \
  git cmake g++ build-essential \
  libboost-all-dev libspdlog-dev \
  libyaml-cpp-dev libeigen3-dev libfmt-dev
```

安装 C++ 版 `unitree_sdk2`。Python 包 `unitree_sdk2py` 不能替代这些头文件和库：

```bash
cd /home/robot
git clone https://github.com/unitreerobotics/unitree_sdk2.git
cmake -S unitree_sdk2 -B unitree_sdk2/build -DCMAKE_BUILD_TYPE=Release
cmake --build unitree_sdk2/build -j"$(nproc)"
sudo cmake --install unitree_sdk2/build
sudo ldconfig

test -f /usr/local/include/unitree/robot/channel/channel_publisher.hpp
test -f /usr/local/include/unitree/idl/go2/MotorCmds_.hpp
```

下载并编译官方 DFX 串口桥：

```bash
cd /home/robot
git clone https://github.com/unitreerobotics/DFX_inspire_service.git

PROJECT_ROOT=/home/robot/eai-teleop-studio
if ! grep -q 'find_package(fmt REQUIRED)' DFX_inspire_service/CMakeLists.txt; then
  git -C DFX_inspire_service apply \
    "$PROJECT_ROOT/patches/inspire_dfx_ubuntu22_fmt.patch"
fi

cmake -S DFX_inspire_service \
  -B DFX_inspire_service/build \
  -DCMAKE_BUILD_TYPE=Release
cmake --build DFX_inspire_service/build --target inspire_h1 -j"$(nproc)"
test -x /home/robot/DFX_inspire_service/build/inspire_h1
```

Ubuntu 22.04 的 `spdlog` 使用外部 `fmt`。若链接阶段出现大量 `undefined reference to fmt::v8`，说明官方 DFX 的 CMake 没有链接这两个库；上面的仓库补丁会增加 `find_package(fmt/spdlog)` 及 `spdlog::spdlog`、`fmt::fmt` 链接。重复部署时可用下面的命令确认补丁是否已经应用：

```bash
grep -nE 'find_package\((fmt|spdlog)|spdlog::spdlog|fmt::fmt' \
  /home/robot/DFX_inspire_service/CMakeLists.txt
```

最后确认 DDS 网卡与程序参数：

```bash
ip -br link show enp86s0
/home/robot/DFX_inspire_service/build/inspire_h1 --help
```

先用只读状态探针验证 DDS：

```bash
/home/robot/miniconda3/envs/teleop/bin/python3 tools/inspire_dfx_dds_probe.py \
  --network-interface enp86s0 --side right --command state
```

仅当因时串口与 Web 服务位于同一台机器时，可创建环境文件并安装仓库提供的串口桥服务：

```bash
cat > config/inspire_dfx.env <<'EOF'
DDS_IFACE=enp86s0
HAND_SERIAL=/dev/serial/by-path/<因时设备物理路径>
INSPIRE_DFX_SERVICE=/home/robot/DFX_inspire_service/build/inspire_h1
EOF

sudo bash scripts/install_autostart_services.sh inspire-dfx.service hand-web.service
systemctl status inspire-dfx.service hand-web.service --no-pager
tail -f logs/app/inspire-dfx.service.log logs/system/hand_web_$(date +%F).log
```

`HAND_SERIAL` 优先使用 `/dev/serial/by-id`；无序列号的 CH340 使用 `/dev/serial/by-path`。重新插拔后 `/dev/ttyUSB0` 可能变化，而稳定链接通常不变。因时串口桥、遥操和其他串口程序不能同时直接占用该设备。

## 增加设备

1. 在 `teleop/robot_control/devices/<device>/` 中实现底层设备 SDK。
2. 在 `hand_web/adapters/` 中实现 `capabilities/connect/disconnect/status/command/stop` 接口。
3. 在 `hand_web/core/registry.py` 中注册设备。
4. 如需视觉控制，在 `hand_web/vision/retargeters/` 中增加通用人手特征到设备关节的重定向器，并在 `hand_web/vision/retargeters/__init__.py` 注册。

前端根据 `capabilities()` 动态生成设备、通信方式、连接字段、左右手和关节控件，不需要为新设备复制服务器或页面。姿态库按设备 ID 分开保存，新增其他品牌时不需要复制姿态管理代码。
