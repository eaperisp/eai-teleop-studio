# eai-teleop-studio 部署

本项目用于 H2 数据采集、相机预览、数据转换、OSS 传输、训练命令生成、数据同步和拨闸 API 服务。推荐部署用户为 `robot`，默认部署根目录为 `/home/robot`。

## 基础目录

项目目录：

```bash
/home/robot/eai-teleop-studio
```

数据目录不放在项目内，统一放到 `/home/robot/data`：

```text
/home/robot/data/datasets/robot               原始采集任务
/home/robot/data/datasets/lerobot             LeRobot 数据集
/home/robot/data/datasets/lerobot/packages    LeRobot 压缩包
/home/robot/data/datasets/openpi              OpenPI 归一化工作目录
/home/robot/data/datasets/training            训练数据集与运行态配置
/home/robot/data/models/openpi_downloads      模型回传下载目录
```

项目内目录约定：

```text
config/       项目配置、模板、证书、初始姿态
logs/app/     常驻服务日志：Web、相机、同步工具、API
logs/system/  Web 平台业务日滚动日志
logs/tasks/   任务日志，按任务名和日期分目录
systemd/      开机自启服务模板
scripts/      部署、自启、同步安装脚本
teleop_web/   数据采集平台 Web 前后端
robot_sync_tool/ 数据同步工具
api/          H2 拨闸 HTTP API
tools/        数据转换、OpenPI、设备辅助工具
```

如果部署目录变化，优先通过 `scripts/install_autostart_services.sh` 重新安装 systemd 服务。服务文件中的 `@PROJECT_ROOT@` 会由安装脚本替换为当前项目目录。

## 系统依赖

```bash
sudo apt update
sudo apt install -y \
  git curl wget rsync build-essential python3-dev cmake \
  ffmpeg libusb-1.0-0-dev libturbojpeg0-dev v4l-utils openssl \
  netcat-openbsd
```

安装 Miniconda：

```bash
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh
~/miniconda3/bin/conda init bash
~/miniconda3/bin/conda config --set auto_activate false
source ~/.bashrc
```

## 克隆项目

```bash
cd /home/robot
git clone https://github.com/eaperisp/eai-teleop-studio eai-teleop-studio
```

## Python环境

本部署拆成 3 个运行环境：

```text
teleimager  相机服务环境，负责 RGB/深度相机采集、ZMQ、WebRTC
teleop      数据采集服务环境，负责 Web 控制台、H2 遥操作、episode 采集、API
lerobot     数据转换服务环境，负责 H2 原始数据转 LeRobot，以及 OpenPI norm stats
```

### 相机服务 teleimager

```bash
conda create -n teleimager python=3.10 numpy=1.26.4 -y --override-channels -c conda-forge
conda activate teleimager

cd /home/robot/eai-teleop-studio
pip install -e "teleop/teleimager[server]"
```

### 数据采集服务 teleop

```bash
conda create -n teleop python=3.10 pinocchio=3.1.0 numpy=1.26.4 -y --override-channels -c conda-forge
conda activate teleop

cd /home/robot/eai-teleop-studio
pip install -r requirements.txt
pip install -e teleop/teleimager --no-deps
pip install -e teleop/televuer
pip install -e teleop/robot_control/dex-retargeting
```

安装 Unitree SDK：

```bash
cd /home/robot
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd /home/robot/unitree_sdk2_python
conda activate teleop
pip install -e .
```

验证：

```bash
cd /home/robot/eai-teleop-studio
conda activate teleop

python - <<'PY'
import numpy
import unitree_sdk2py
from vuer import Vuer
print("numpy", numpy.__version__)
print("imports ok")
PY
```
安装 inspire:
```bash
sudo apt update
sudo apt install cmake -y \
  build-essential g++ \
  libboost-all-dev \
  libspdlog-dev \
  libyaml-cpp-dev \
  libeigen3-dev \
  libfmt-dev
```
安装 C++ 版 unitree_sdk2（DFX 服务的编译依赖）:
```bash
cd /home/robot

git clone https://github.com/unitreerobotics/unitree_sdk2.git

cmake -S unitree_sdk2 \
  -B unitree_sdk2/build \
  -DCMAKE_BUILD_TYPE=Release

cmake --build unitree_sdk2/build -j6

sudo cmake --install unitree_sdk2/build
sudo ldconfig
```

```bash
cd /home/robot
git clone https://github.com/unitreerobotics/DFX_inspire_service.git

# 第二次执行 git apply 会提示“补丁未应用”
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



### 数据转换服务 lerobot

```bash
conda create -n lerobot python=3.12 numpy=1.26.4 -y --override-channels -c conda-forge
conda activate lerobot
pip install "numpy==1.26.4" pillow tyro datasets==3.6.0 huggingface_hub safetensors lerobot==0.3.3
pip check
```

验证：

```bash
conda activate lerobot
python - <<'PY'
import numpy
import lerobot
print("numpy:", numpy.__version__)
print("lerobot:", getattr(lerobot, "__version__", "unknown"))
try:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
except ImportError:
    from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
print("LeRobotDataset ok")
PY
```

## 日志规划

服务日志放到 `logs/app/`：

```text
logs/app/xr-teleop-web.service.log
logs/app/hand-web.service.log
logs/app/teleimager-camera-capture.service.log
logs/app/robot-sync-tool.service.log
logs/app/robot_sync.log
logs/app/h2_switch_flip_api_17002.log
logs/app/h2_switch_flip_api_17002.nohup.out
```

这些常驻服务日志不在启动时追加时间戳。当前日志保持固定文件名，方便服务写入和 `tail -f`；历史日志由 logrotate 自动轮转、压缩并追加日期后缀。logrotate 只处理 `logs/app/*.log` 和 `logs/app/*.out`，不处理 `logs/system/` 和 `logs/tasks/`。

默认轮转策略位于 `config/eai-teleop-studio.logrotate`。安装自启动服务时会自动安装到 `/etc/logrotate.d/eai-teleop-studio`。

默认规则：

```text
每天检查，或单文件超过 50M 时轮转
保留 14 份历史日志
历史日志压缩
使用 copytruncate，服务无需重启即可继续写当前日志
```

Web 平台业务日志按日期放到 `logs/system/teleop_YYYY-MM-DD.log`，灵巧手调试操作日志放到 `logs/system/hand_web_YYYY-MM-DD.log`。两者使用相同的日志格式。

数据转换、归一化、OSS 上传/下载等任务日志按任务名和日期分目录：

```text
logs/tasks/<task_name>/YYYY-MM-DD/<job_kind>_<job_id>.log
```

拨闸 API 每次动作请求单独建目录，并自动只保留最近 20 个动作目录：

```text
logs/tasks/h2_switch_flip_api/YYYY-MM-DD/YYYYmmdd_HHMMSS_<task_id>/
  vla.jsonl
  stdout.out
  debug_images/
```

## 相机服务

相机配置文件：

```text
teleop/teleimager/cam_config_server.yaml
```

### 生成 HTTPS 证书

WebRTC 使用 HTTPS。证书建议包含相机服务对外访问地址和本机地址：

```bash
cd /home/robot/eai-teleop-studio
mkdir -p config

openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout config/key.pem \
  -out config/cert.pem


openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 3650 \
  -keyout config/key.pem \
  -out config/cert.pem \
  -subj "/CN=192.168.60.60" \
  -addext "subjectAltName=IP:192.168.60.60,DNS:robot"

chmod 600 config/key.pem
```




### 查找相机配置

核心规则是看每个 `/dev/videoX` 的 `serial_number`、`usb_interface`、`video_index` 和 `formats`：

```text
formats=YUYV/MJPG  -> RGB 节点
formats=Z16        -> 深度节点
formats=GREY/BA81  -> 不选
formats 为空       -> 不选
```

列出相机：

```bash
cd /home/robot/eai-teleop-studio
conda activate teleimager
python tools/generate_h2_camera_config.py --list
```

生成配置草稿：

```bash
python tools/generate_h2_camera_config.py \
  --head-serial <head_serial> \
  --torso-serial <torso_serial> \
  --left-serial <left_wrist_serial> \
  --right-serial <right_wrist_serial> \
  --depth head torso \
  --color-size 640x480 \
  --head-color-size 1920x1080 \
  --right-color-size 1920x1080 \
  --depth-size 640x480 \
  --head-depth-size 1280x800 \
  -o /tmp/cam_config_server.yaml
```

```bash
python tools/generate_h2_camera_config.py \
  --head-serial CP0T263000BE \
  --torso-serial CPCBC530002E\
  --left-serial CP0F463000HS \
  --right-serial CP06563000E6 \
  --depth head torso \
  --head-color-size 1920x1080 \
  --torso-color-size 640x480 \
  --left-color-size 640x480 \
  --right-color-size 1920x1080 \
  --head-depth-size 1280x800 \
  --torso-depth-size 640x480 \
  --color-size 640x480 \
  --depth-size 640x480 \
  -o /tmp/cam_config_server.yaml
```
`--depth` 后可填写一个或多个位置；选中的 RGB-D 流默认启用 ZMQ，`--depth none` 不生成 RGB-D 配置。

`--depth` 可选值：

```text
不传或 head       生成 4 路 RGB + 头部 RGB-D
head torso        生成 4 路 RGB + 头部/腰部 RGB-D
left right        生成 4 路 RGB + 左腕/右腕 RGB-D
none              只生成 4 路 RGB
all               生成 4 路 RGB + 头部/腰部/左腕/右腕 RGB-D
```

如果现场只插了一部分相机，可以加 `--allow-missing` 生成草稿。确认无误后再覆盖正式配置：

```bash
cp teleop/teleimager/cam_config_server.yaml teleop/teleimager/cam_config_server.yaml.bak_$(date +%Y%m%d_%H%M%S)
cp /tmp/cam_config_server.yaml teleop/teleimager/cam_config_server.yaml
```

端口建议：

```text
RGB ZMQ:      55555 head, 55556 torso, 55557 left_wrist, 55558 right_wrist
RGB-D ZMQ:    55560 head, 55564 left_wrist, 55565 right_wrist, 55566 torso
WebRTC RGB:   60001 head, 60002 torso, 60003 left_wrist, 60004 right_wrist
配置服务:     60000
```

注意：

```text
深度 enable_webrtc 目前没有实际作用，保持 false。
WebRTC 是 RGB 预览视频，算法要原始深度请走 ZMQ。
数据采集只录 RGB，data_format: depth_z16 不会进入 RGB 录制列表。
```

### 手动启动相机服务

```bash
conda activate teleimager
cd /home/robot/eai-teleop-studio

XR_TELEOP_WEBRTC_SCHEME=https \
XR_TELEOP_CERT=/home/robot/eai-teleop-studio/config/cert.pem \
XR_TELEOP_KEY=/home/robot/eai-teleop-studio/config/key.pem \
python -m teleimager.image_server --no-affinity
```

检查端口：

```bash
ss -ltnp | grep -E ':60000|:60001|:60002|:60003|:60004|:55555|:55559'
curl -k -I https://127.0.0.1:60001/
```

浏览器检查：

```text
https://<相机服务 IP>:60001/
https://<相机服务 IP>:60002/
https://<相机服务 IP>:60003/
https://<相机服务 IP>:60004/
```

首次打开自签名 HTTPS 时，需要在浏览器里信任证书。

测试头部深度：

```bash
cd /home/robot/eai-teleop-studio
conda activate teleimager
python teleop/teleimager/examples/head_depth_client_demo.py --host 127.0.0.1 --frames 12 --timeout 5
```

### systemd 安装相机服务

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh teleimager-camera-capture.service
sudo systemctl restart teleimager-camera-capture.service
```

常用命令：

```bash
systemctl status teleimager-camera-capture.service --no-pager
journalctl -u teleimager-camera-capture.service -n 120 --no-pager
tail -f /home/robot/eai-teleop-studio/logs/app/teleimager-camera-capture.service.log
```

## Web 数据采集平台

手动启动：

```bash
conda activate teleop
cd /home/robot/eai-teleop-studio

XR_TELEOP_DATA_DIR=/home/robot/data \
XR_TELEOP_DEFAULT_WEBRTC_SERVER_IP=<相机服务 IP> \
XR_TELEOP_DEFAULT_IMAGE_SERVER_IP=<相机服务 IP> \
XR_TELEOP_WEBRTC_SCHEME=https \
XR_TELEOP_CERT=/home/robot/eai-teleop-studio/config/cert.pem \
XR_TELEOP_KEY=/home/robot/eai-teleop-studio/config/key.pem \
XR_TELEOP_LEROBOT_PYTHON=/home/robot/miniconda3/envs/lerobot/bin/python \
HF_LEROBOT_HOME=/home/robot/data/datasets/lerobot \
python -m teleop_web.server \
  --host 0.0.0.0 \
  --port 18099 \
  --dataset-dir /home/robot/data/datasets/robot \
  --config /home/robot/eai-teleop-studio/config/web_console.json \
  --task-file /home/robot/data/datasets/robot/tasks.json \
  --log-dir /home/robot/eai-teleop-studio/logs
```

访问：

```text
http://<机器人 IP>:18099/
```

状态检查：

```bash
curl -s http://127.0.0.1:18099/api/state | python -m json.tool | sed -n '1,160p'
```

设备页面建议：

```text
机器人型号: H2
图像服务 IP: <相机服务 IP>
WebRTC 服务 IP: <相机服务 IP>
数据目录: /home/robot/data
DDS 网络接口: 按现场 H2 网卡填写
显示模式: pass-through 或 head
VR 相机视角: head
```

如果相机服务不在 Web 服务所在机器，图像服务 IP 和 WebRTC 服务 IP 必须填写实际运行 `teleimager.image_server` 的机器 IP。

配置文件优先放在 `config/`。训练命令模板使用 `config/delivery_templates.json`。运行态数据和较大的任务状态放到 `/home/robot/data/datasets/...`，不要放在项目代码目录。

安装 Web 自启动服务：

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh xr-teleop-web.service
sudo systemctl restart xr-teleop-web.service
```

常用命令：

```bash
systemctl status xr-teleop-web.service --no-pager
journalctl -u xr-teleop-web.service -f
tail -f /home/robot/eai-teleop-studio/logs/app/xr-teleop-web.service.log
tail -f /home/robot/eai-teleop-studio/logs/system/teleop_$(date +%F).log
```

数据采集任务启动后，`teleop_hand_and_arm.py` 会在 `8012` 创建 TeleVuer HTTPS/WSS 服务；`8012` 不是独立的 systemd 服务。检查方式：

```bash
ss -ltnp 'sport = :8012'
curl -kI https://127.0.0.1:8012/
```

如果遥操进程仍在运行但 `8012` 未监听，并出现 `Vuer encountered an error: [Errno 2] No such file or directory`，通常是 TeleVuer 找不到 HTTPS 证书。确认以下文件存在且服务用户可读，然后重新安装 `xr-teleop-web.service` 并重新进入数据采集：

```bash
ls -l config/cert.pem config/key.pem
sudo bash scripts/install_autostart_services.sh xr-teleop-web.service
```

服务模板会将它们显式设置为 `XR_TELEOP_CERT` 和 `XR_TELEOP_KEY`，不再依赖登录 shell 的临时环境变量。不要在遥操或录制过程中重启 `xr-teleop-web.service`；先从页面安全结束当前任务。

## 开机自启服务

项目提供 6 个 systemd 服务：

```text
can0.service                          达妙电机 SocketCAN can0 初始化服务，1 Mbps
xr-teleop-web.service                 Web 数据采集平台，端口 18099
hand-web.service                      灵巧手调试工具，端口 18089
teleimager-camera-capture.service     相机 WebRTC/ZMQ 服务
robot-sync-tool.service               数据同步工具，默认端口 18090
h2-switch-flip-api.service            H2 拨闸 API，端口 17002
```

### CAN 电机服务

`can0.service` 用于在 CAN USB 设备出现后配置 `can0`：先关闭接口，再设置 `1 Mbps` 波特率、`restart-ms 100` 和 `txqueuelen 1000`，最后拉起接口。不能只执行 `ip link set can0 up`；接口尚未配置波特率时会返回 `RTNETLINK answers: Invalid argument`。

只安装 CAN 服务：

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh can0.service
```

手动安装方式：

```bash
cd /home/robot/eai-teleop-studio
sudo install -m 0644 systemd/can0.service /etc/systemd/system/can0.service
sudo systemctl daemon-reload
sudo systemctl enable can0.service
sudo systemctl restart can0.service
```

验证服务及接口参数：

```bash
systemctl is-enabled can0.service
systemctl is-active can0.service
ip -details link show can0
```

正常状态应包含 `enabled`、`active`、接口 `UP`、`bitrate 1000000`、`restart-ms 100` 和 `qlen 1000`。`can state ERROR-ACTIVE` 表示 CAN 控制器正在正常参与总线通信，不是服务错误。

服务异常时执行：

```bash
sudo systemctl reset-failed can0.service
sudo systemctl restart can0.service
journalctl -u can0.service -n 50 --no-pager
```

服务通过 `BindsTo=sys-subsystem-net-devices-can0.device` 绑定 `can0` 设备，CAN USB 拔出时服务会停止，重新插入并再次识别为 `can0` 后会自动启动。如果设备被识别成 `can1`，需要将 `systemd/can0.service` 中的设备名统一改为实际名称，然后重新安装服务。

安装全部服务：

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh
```

只安装某几个服务：

```bash
sudo bash scripts/install_autostart_services.sh xr-teleop-web.service h2-switch-flip-api.service
```

只安装灵巧手调试服务：

```bash
sudo usermod -aG dialout robot
sudo bash scripts/install_autostart_services.sh hand-web.service
sudo systemctl restart hand-web.service
```

灵巧手服务还要求官方 `bc-stark-sdk` 安装在 `/home/robot/miniconda3/envs/teleop` 环境。视觉控制默认使用访问页面电脑的浏览器摄像头；远程 HTTP 下可通过 SSH 端口转发访问本机地址。Linux 串口权限、FT2232 双通道识别、Revo2 端口确认、视觉访问、首次部署和更新部署的完整步骤见 [`hand_web/README_zh-CN.md`](hand_web/README_zh-CN.md)。

查看状态：

```bash
systemctl is-active xr-teleop-web.service
systemctl is-active can0.service
systemctl is-active hand-web.service
systemctl is-active teleimager-camera-capture.service
systemctl is-active robot-sync-tool.service
systemctl is-active h2-switch-flip-api.service
```

重启：

```bash
sudo systemctl restart xr-teleop-web.service
sudo systemctl restart can0.service
sudo systemctl restart hand-web.service
sudo systemctl restart teleimager-camera-capture.service
sudo systemctl restart robot-sync-tool.service
sudo systemctl restart h2-switch-flip-api.service
```

## 数据同步工具

配置示例：`config/robot_sync.env.example`。首次安装时会生成 `config/robot_sync.env`。

关键配置：

```env
ROBOT_SYNC_REMOTE_HOST=robot@<远端机器 IP>
ROBOT_SYNC_REMOTE_DIR=~/data/datasets/robot
ROBOT_SYNC_LOCAL_DIR=/home/robot/data/datasets/robot
ROBOT_SYNC_RECORDS_FILE=data/robot_sync_records.json
ROBOT_SYNC_LOG_FILE=logs/app/robot_sync.log
```

单独安装同步工具：

```bash
cd /home/robot/eai-teleop-studio
sudo bash scripts/robot_sync/install_autostart.sh
```

### 移动硬盘同步

挂载硬盘：

```bash
sudo mkdir -p /media/robot/<源盘名称> /media/robot/<目标盘名称>
sudo chown robot:robot /media/robot/<源盘名称> /media/robot/<目标盘名称>

sudo mount -t exfat \
  -o uid=$(id -u robot),gid=$(id -g robot),umask=0022 \
  LABEL=<源盘卷标> /media/robot/<源盘名称>

sudo mount -t exfat \
  -o uid=$(id -u robot),gid=$(id -g robot),umask=0022 \
  LABEL=<目标盘卷标> /media/robot/<目标盘名称>
```

模拟检查将要同步的内容：

```bash
rsync -rtvhn \
  --modify-window=1 \
  --info=progress2 \
  "/media/robot/<源盘名称>/data/" \
  "/media/robot/<目标盘名称>/data/"
```

正式同步：

```bash
sudo rsync -rtvh \
  --modify-window=1 \
  --info=progress2 \
  --partial \
  "/media/robot/<源盘名称>/data/" \
  "/media/robot/<目标盘名称>/data/"
```

卸载硬盘：

```bash
sudo umount /media/robot/<源盘名称>
sudo umount /media/robot/<目标盘名称>
```

## LeRobot 转换

Web 页面会根据 `/home/robot/data/datasets/robot/tasks.json` 中的任务名和 instruction 生成转换参数。转换输出支持图片和视频编码，保留 `png`、`jpg/jpeg`，并支持 `video`。

`video` 会把每路相机编码为 LeRobot MP4，CPU 和磁盘压力明显高于 `jpg/png`。当前默认策略：

```text
图片模式：image-writer-processes=0，image-writer-threads=2
video 模式：image-writer-processes=2，image-writer-threads=4
```

如果机器 CPU 充足、内存和 IO 也够，可以在页面里把 `writer-threads` 调到 6-8；如果内存紧张，则把 `writer-processes` 降到 1 或 0。大数据集首次转 video 建议先用较小 `batch-size` 验证。

手动 dry-run：

```bash
conda activate lerobot
cd /home/robot/eai-teleop-studio
export HF_LEROBOT_HOME=/home/robot/data/datasets/lerobot

python tools/convert_h2_to_lerobot.py \
  --src /home/robot/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --urdf /home/robot/eai-teleop-studio/assets/h2/H2.urdf \
  --dry-run
```

手动转换：

```bash
python tools/convert_h2_to_lerobot.py \
  --src /home/robot/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --task "<instruction>" \
  --urdf /home/robot/eai-teleop-studio/assets/h2/H2.urdf \
  --camera-map color_0:image,color_2:left_wrist_image,color_3:right_wrist_image \
  --image-writer-processes 0 \
  --image-writer-threads 2 \
  --overwrite \
  --batch-size 50
```

断点续转：

```bash
python tools/convert_h2_to_lerobot.py \
  --src /home/robot/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --task "<instruction>" \
  --urdf /home/robot/eai-teleop-studio/assets/h2/H2.urdf \
  --camera-map color_0:image,color_2:left_wrist_image,color_3:right_wrist_image \
  --image-writer-processes 0 \
  --image-writer-threads 2 \
  --resume \
  --resume-overlap 2 \
  --batch-size 50
```

常用 camera-map：

```text
三路: head + left_wrist + right_wrist
color_0:image,color_2:left_wrist_image,color_3:right_wrist_image

如果只有 head + torso + right_wrist:
color_0:image,color_1:left_wrist_image,color_2:right_wrist_image
```

输出检查：

```bash
find /home/robot/data/datasets/lerobot/local/<task_name> -maxdepth 3 -type f | sort | sed -n '1,120p'
```

## OpenPI

OpenPI 推荐目录：

```text
/home/robot/openpi
```

安装：

```bash
cd /home/robot
git clone --recurse-submodules https://github.com/Physical-Intelligence/openpi.git
cd /home/robot/openpi
git submodule update --init --recursive

conda activate lerobot
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python -m pip install -e .
```

归一化前检查：

```bash
test -f /home/robot/data/datasets/lerobot/local/<task_name>/meta/info.json
test -f /home/robot/openpi/scripts/compute_norm_stats.py
```

手动归一化：

```bash
conda activate lerobot
cd /home/robot/data/datasets/openpi

export HF_LEROBOT_HOME=/home/robot/data/datasets/lerobot
export HF_HUB_OFFLINE=1
export OPENPI_H2_REPO_ID=local/<task_name>
export PYTHONPATH=/home/robot/openpi/src

/home/robot/miniconda3/envs/lerobot/bin/python \
  /home/robot/openpi/scripts/compute_norm_stats.py \
  --config-name pi05_h2_lerobot
```

如果推理服务缺少 `openpi_client`，在 OpenPI 目录安装 client 包：

```bash
cd /home/robot/openpi
uv pip install --python ./.venv/bin/python -e packages/openpi-client
./.venv/bin/python -c "import openpi_client; print(openpi_client.__file__)"
```

启动推理服务示例：

```bash
cd /home/robot/openpi
OPENPI_ROOT=/home/robot/openpi nohup ./scripts/deploy/run_pi05_h2_server.sh > logs/h2_openpi_server.log 2>&1 &
tail -f logs/h2_openpi_server.log
```

模型部署命令由训练模板生成，`DEFAULT_PROMPT` 使用采集任务中的 instruction。

## H2 拨闸 API

API 脚本：`api/h2_switch_flip_api.py`。

systemd 服务：`h2-switch-flip-api.service`。

手动启动：

```bash
cd /home/robot/eai-teleop-studio
mkdir -p logs/app logs/tasks

nohup /home/robot/miniconda3/envs/teleop/bin/python3 api/h2_switch_flip_api.py \
  --host 0.0.0.0 \
  --port 17002 \
  --log-file logs/app/h2_switch_flip_api_17002.log \
  > logs/app/h2_switch_flip_api_17002.nohup.out 2>&1 &
```

调用：

```bash
curl -X POST http://127.0.0.1:17002/task/flip \
  -H 'Content-Type: application/json' \
  -d '{"language":"Change the switch from close to remote","retries":3}'
```

查询状态：

```bash
curl -s http://127.0.0.1:17002/task/status | python3 -m json.tool
```

中止任务：

```bash
curl -X POST http://127.0.0.1:17002/task/abort
```

当前支持：

```text
Change the switch from close to remote
```

暂不支持：

```text
Change the switch from remote to close
```

## 验收清单

相机服务：

```bash
systemctl status teleimager-camera-capture.service --no-pager
journalctl -u teleimager-camera-capture.service -n 120 --no-pager
ss -ltnp | grep -E ':60000|:60001|:60002|:60003|:60004'
```

浏览器：

```text
https://<相机服务 IP>:60001/
https://<相机服务 IP>:60002/
https://<相机服务 IP>:60003/
https://<相机服务 IP>:60004/
http://<机器人 IP>:18088/
```

深度数据：

```bash
cd /home/robot/eai-teleop-studio
conda activate teleimager
python teleop/teleimager/examples/head_depth_client_demo.py --host 127.0.0.1 --frames 12 --timeout 5
```

Web 服务：

```bash
curl -s http://127.0.0.1:18088/api/state | python -m json.tool | sed -n '1,160p'
```

转换服务：

```bash
conda activate lerobot
cd /home/robot/eai-teleop-studio
export HF_LEROBOT_HOME=/home/robot/data/datasets/lerobot
python tools/convert_h2_to_lerobot.py \
  --src /home/robot/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --urdf /home/robot/eai-teleop-studio/assets/h2/H2.urdf \
  --dry-run
```

## 常用排查

Web 服务：

```bash
ps -ef | grep '[t]eleop_web.server'
ss -lntp | grep 18088
tail -n 100 /home/robot/eai-teleop-studio/logs/system/teleop_$(date +%F).log
```

相机服务：

```bash
systemctl status teleimager-camera-capture.service --no-pager
journalctl -u teleimager-camera-capture.service -n 100 --no-pager
ss -ltnp | grep -E ':60000|:60001|:55555|:55559'
python teleop/teleimager/examples/head_depth_client_demo.py --host 127.0.0.1 --frames 12
```

API 服务：

```bash
systemctl status h2-switch-flip-api.service --no-pager
tail -f /home/robot/eai-teleop-studio/logs/app/h2_switch_flip_api_17002.log
```

数据转换卡住：

```bash
pgrep -af 'convert_h2_to_lerobot|compute_norm_stats|teleop_hand_and_arm|ossutil'
free -h
ps -o pid,ppid,pgid,stat,etime,%cpu,%mem,rss,vsz,cmd -C python
```

检查是否仍有旧路径：

```bash
grep -RIn '/data02\|/data03\|/home/ubuntu\|eai_teleoperate_studio' config systemd teleop_web api tools | head
```

## 常见问题

### No module named teleop_web

通常是 systemd 的 `WorkingDirectory` 指向了错误目录。检查：

```bash
systemctl cat xr-teleop-web.service
ls /home/robot/eai-teleop-studio/teleop_web
```

### 页面能打开，但相机预览灰屏

确认设备页面的 WebRTC 服务 IP 是实际运行相机服务的机器，而不是固定填 Web 服务机器。自签 HTTPS 证书需要浏览器先信任一次。

### 60001 无法访问

先确认端口是否监听：

```bash
ss -ltnp | grep 60001
```

如果没有监听，看日志：

```bash
journalctl -u teleimager-camera-capture.service -n 120 --no-pager
```

常见原因：

```text
head_camera offline
配置里的 serial_number 不在 /dev/v4l/by-id 中
相机被 Orbbec SDK/ROS 占用，lsusb -t 显示 Driver=usbfs
配置用了 1920x1080@30，但当前 USB2 只支持低帧率
```

### 相机节点很多，不知道怎么选

只看格式：

```text
YUYV/MJPG  -> RGB
Z16        -> 深度
GREY/BA81  -> 不选
空 formats -> 不选
```

示例：

```text
serial + interface=1.4 + index=0 -> RGB
serial + interface=1.0 + index=0 -> depth
```

### 深度 demo 报 enable_zmq false

说明客户端拿到的配置里深度没有开启，或者请求服务端失败后读了旧缓存。检查服务端返回：

```bash
python - <<'PY'
import sys
sys.path.insert(0, "/home/robot/eai-teleop-studio/teleop/teleimager/src")
from teleimager.image_client import ImageClient
c = ImageClient(host="127.0.0.1", request_bgr=False)
print(c.get_cam_config().get("head_depth_camera"))
c.close()
PY
```

如果请求远端：

```bash
nc -vz <相机服务 IP> 60000
```

必要时删除客户端缓存：

```bash
rm -f /home/robot/eai-teleop-studio/teleop/teleimager/cam_config_client.yaml
```

### 转换慢或失败

大数据集建议：

```bash
--batch-size 50 --image-writer-processes 0 --image-writer-threads 2
```

观察进程和内存：

```bash
free -h
pgrep -af 'convert_h2_to_lerobot|compute_norm_stats|teleop_hand_and_arm'
ps -o pid,ppid,pgid,stat,etime,%cpu,%mem,rss,vsz,cmd -C python
```

### 数据采集是否会录深度

不会。采集脚本只把 `data_format` 为空或 jpeg 的相机当 RGB 录制。`data_format: depth_z16` 只给算法服务或 demo 主动读取。

### 配置修改后页面仍然旧

运行中的 Web 进程不会自动重读所有配置，重启：

```bash
sudo systemctl restart xr-teleop-web.service
```

### OSS 或转换记录很多后变慢

任务日志已经按 `logs/tasks/<task_name>/<date>/` 分目录存放。页面运行态记录会保留必要索引，不应再扫描整个日志目录。若手工归档旧任务，只移动 `logs/tasks/<task_name>/` 下的历史日期目录即可。
