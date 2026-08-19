# eai-teleop-studio 部署

本项目用于 H2 数据采集、相机预览、数据转换、OSS 传输、训练命令生成、数据同步和拨闸 API 服务。推荐部署用户为 `robot`。

常用部署目录：

```bash
/home/robot/eai-teleop-studio
```

如果部署目录变化，优先通过 `scripts/install_autostart_services.sh` 重新安装 systemd 服务。服务文件中的 `@PROJECT_ROOT@` 会由安装脚本替换为当前项目目录。

## 目录约定

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

数据目录不放在项目内。当前生产环境常用：

```text
/home/robot/data/datasets/robot               原始采集任务
/home/robot/data/datasets/lerobot             LeRobot 数据集
/home/robot/data/datasets/lerobot/packages    LeRobot 压缩包
/home/robot/data/datasets/openpi              OpenPI 归一化工作目录
/home/robot/data/datasets/training            训练数据集与运行态配置
/home/robot/data/models/openpi_downloads      模型回传下载目录
```

## 日志规划

服务日志放到 `logs/app/`：

```text
logs/app/xr-teleop-web.service.log
logs/app/teleimager-camera-capture.service.log
logs/app/robot-sync-tool.service.log
logs/app/robot_sync.log
logs/app/h2_switch_flip_api_17002.log
logs/app/h2_switch_flip_api_17002.nohup.out
```

这些常驻服务日志不在启动时追加时间戳。当前日志保持固定文件名，方便服务写入和 `tail -f`；历史日志由 logrotate 自动轮转、压缩并追加日期后缀。logrotate 只处理 `logs/app/*.log` 和 `logs/app/*.out`，不处理 `logs/system/` 和 `logs/tasks/`。

默认轮转策略位于：

```text
config/eai-teleop-studio.logrotate
```

安装自启动服务时会自动安装到：

```text
/etc/logrotate.d/eai-teleop-studio
```

默认规则：

```text
每天检查，或单文件超过 50M 时轮转
保留 14 份历史日志
历史日志压缩
使用 copytruncate，服务无需重启即可继续写当前日志
```

Web 平台业务日志按日期放到 `logs/system/`：

```text
logs/system/teleop_YYYY-MM-DD.log
```

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

## 系统准备

```bash
sudo apt update
sudo apt install -y \
  git curl wget rsync build-essential python3-dev \
  ffmpeg libusb-1.0-0-dev libturbojpeg0-dev v4l-utils openssl
```

## 克隆项目
```bash
cd /home/robot
git clone https://github.com/eaperisp/eai-teleop-studio eai-teleop-studio
```

Miniconda：

```bash
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh
~/miniconda3/bin/conda init bash
~/miniconda3/bin/conda config --set auto_activate false
source ~/.bashrc
```

## Python 环境

Web、遥操和 API 使用 `teleop`：

```bash
conda create -n teleop python=3.10 pinocchio=3.1.0 numpy=1.26.4 -y --override-channels -c conda-forge
conda activate teleop
conda install pip -y --override-channels -c conda-forge

cd /data02/app/eai-teleop-studio
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e teleop/televuer
python -m pip install -e teleop/robot_control/dex-retargeting
```

相机服务使用 `teleimager`：

```bash
conda create -n teleimager python=3.10 numpy=1.26.4 -y --override-channels -c conda-forge
conda activate teleimager
cd /data02/app/eai-teleop-studio
python -m pip install -U pip
python -m pip install -e "teleop/teleimager[server]"
```

LeRobot 转换和 OpenPI 归一化使用 `lerobot`：

```bash
conda create -n lerobot python=3.12 numpy=1.26.4 -y --override-channels -c conda-forge
conda activate lerobot
conda install pip -y --override-channels -c conda-forge
python -m pip install -U pip
python -m pip install "numpy==1.26.4" pillow tyro datasets==3.6.0 huggingface_hub safetensors lerobot==0.3.3
python -m pip check
```

## 开机自启服务

项目提供 4 个 systemd 服务：

```text
xr-teleop-web.service                 Web 数据采集平台，端口 18088
teleimager-camera-capture.service     相机 WebRTC/ZMQ 服务
robot-sync-tool.service               数据同步工具，默认端口 18090
h2-switch-flip-api.service            H2 拨闸 API，端口 17002
```

安装全部服务：

```bash
cd /data02/app/eai-teleop-studio
sudo bash scripts/install_autostart_services.sh
```

只安装某几个服务：

```bash
sudo bash scripts/install_autostart_services.sh xr-teleop-web.service h2-switch-flip-api.service
```

查看状态：

```bash
systemctl is-active xr-teleop-web.service
systemctl is-active teleimager-camera-capture.service
systemctl is-active robot-sync-tool.service
systemctl is-active h2-switch-flip-api.service
```

重启：

```bash
sudo systemctl restart xr-teleop-web.service
sudo systemctl restart teleimager-camera-capture.service
sudo systemctl restart robot-sync-tool.service
sudo systemctl restart h2-switch-flip-api.service
```

查看日志：

```bash
journalctl -u xr-teleop-web.service -f
tail -f logs/app/xr-teleop-web.service.log
tail -f logs/system/teleop_$(date +%F).log
```

## Web 数据采集平台

手动启动：

```bash
cd /data02/app/eai-teleop-studio
/home/robot/miniconda3/envs/teleop/bin/python3 -m teleop_web.server \
  --host 0.0.0.0 \
  --port 18088 \
  --config config/web_console.json \
  --log-dir logs
```

访问：

```text
http://<机器人或训练服务器 IP>:18088
```

状态检查：

```bash
curl -s http://127.0.0.1:18088/api/state | python -m json.tool | sed -n '1,160p'
```

配置文件优先放在 `config/`。训练命令模板使用：

```text
config/delivery_templates.json
```

运行态数据和较大的任务状态放到 `/data03/data/datasets/...`，不要放在项目代码目录。

## 相机服务

相机配置：

```text
teleop/teleimager/cam_config_server.yaml
```

证书：

```bash
cd /data02/app/eai-teleop-studio
mkdir -p config
openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout config/key.pem \
  -out config/cert.pem \
  -subj "/CN=192.168.61.228" \
  -addext "subjectAltName=IP:192.168.61.228,IP:192.168.61.142"
chmod 600 config/key.pem
```

检查相机：

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
```

浏览器检查：

```text
https://<相机服务 IP>:60001/
https://<相机服务 IP>:60002/
https://<相机服务 IP>:60003/
https://<相机服务 IP>:60004/
```

如果 Web 服务在 228，但相机服务在 142，设备页面中的 WebRTC 服务 IP 应填写实际运行 `teleimager.image_server` 的机器 IP。

## 数据同步工具

配置示例：

```text
config/robot_sync.env.example
```

首次安装时会生成：

```text
config/robot_sync.env
```

关键配置：

```env
ROBOT_SYNC_REMOTE_HOST=robot@192.168.61.142
ROBOT_SYNC_REMOTE_DIR=~/data/datasets/robot
ROBOT_SYNC_LOCAL_DIR=/data03/data/datasets/robot
ROBOT_SYNC_RECORDS_FILE=data/robot_sync_records.json
ROBOT_SYNC_LOG_FILE=logs/app/robot_sync.log
```

单独安装同步工具：

```bash
cd /data02/app/eai-teleop-studio
sudo bash scripts/robot_sync/install_autostart.sh
```

## LeRobot 转换

Web 页面会根据 `datasets/robot/tasks.json` 中的任务名和 instruction 生成转换参数。转换输出支持图片和视频编码，保留 `png`、`jpg/jpeg`，并支持 `video`。

`video` 会把每路相机编码为 LeRobot MP4，CPU 和磁盘压力明显高于 `jpg/png`。当前默认策略：

```text
图片模式：image-writer-processes=0，image-writer-threads=2
video 模式：image-writer-processes=2，image-writer-threads=4
```

如果机器 CPU 充足、内存和 IO 也够，可以在页面里把 `writer-threads` 调到 6-8；如果内存紧张，则把 `writer-processes` 降到 1 或 0。大数据集首次转 video 建议先用较小 `batch-size` 验证。

手动 dry-run：

```bash
conda activate lerobot
cd /data02/app/eai-teleop-studio
export HF_LEROBOT_HOME=/data03/data/datasets/lerobot

python tools/convert_h2_to_lerobot.py \
  --src /data03/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --urdf assets/h2/H2.urdf \
  --dry-run
```

手动转换：

```bash
python tools/convert_h2_to_lerobot.py \
  --src /data03/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --task "<instruction>" \
  --urdf assets/h2/H2.urdf \
  --camera-map color_0:image,color_2:left_wrist_image,color_3:right_wrist_image \
  --image-encoding video \
  --image-writer-processes 2 \
  --image-writer-threads 4 \
  --batch-size 50 \
  --overwrite
```

断点续转：

```bash
python tools/convert_h2_to_lerobot.py \
  --src /data03/data/datasets/robot/<task_name> \
  --repo-id local/<task_name> \
  --robot-type robot \
  --task "<instruction>" \
  --urdf assets/h2/H2.urdf \
  --camera-map color_0:image,color_2:left_wrist_image,color_3:right_wrist_image \
  --resume \
  --resume-overlap 2 \
  --batch-size 50
```

## OpenPI

OpenPI 推荐目录：

```text
/data02/app/openpi
```

归一化前检查：

```bash
test -f /data03/data/datasets/lerobot/local/<task_name>/meta/info.json
test -f /data02/app/openpi/scripts/compute_norm_stats.py
```

手动归一化：

```bash
conda activate lerobot
cd /data03/data/datasets/openpi

export HF_LEROBOT_HOME=/data03/data/datasets/lerobot
export HF_HUB_OFFLINE=1
export OPENPI_H2_REPO_ID=local/<task_name>
export PYTHONPATH=/data02/app/openpi/src

/home/robot/miniconda3/envs/lerobot/bin/python \
  /data02/app/openpi/scripts/compute_norm_stats.py \
  --config-name pi05_h2_lerobot
```

模型部署命令由训练模板生成，`DEFAULT_PROMPT` 使用采集任务中的 instruction。

## H2 拨闸 API

API 脚本：

```text
api/h2_switch_flip_api.py
```

systemd 服务：

```text
h2-switch-flip-api.service
```

手动启动：

```bash
cd ~/eai-teleop-studio
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

## 常用排查

Web 服务：

```bash
ps -ef | grep '[t]eleop_web.server'
ss -lntp | grep 18088
tail -n 100 logs/system/teleop_$(date +%F).log
```

相机服务：

```bash
systemctl status teleimager-camera-capture.service --no-pager
journalctl -u teleimager-camera-capture.service -n 100 --no-pager
```

API 服务：

```bash
systemctl status h2-switch-flip-api.service --no-pager
tail -f logs/app/h2_switch_flip_api_17002.log
```

数据转换卡住：

```bash
pgrep -af 'convert_h2_to_lerobot|compute_norm_stats|teleop_hand_and_arm|ossutil'
free -h
```

检查是否仍有旧路径：

```bash
grep -RIn '/home/ubuntu\|eai_teleoperate_studio' config systemd teleop_web api tools | head
```

## 常见问题

### No module named teleop_web

通常是 systemd 的 `WorkingDirectory` 指向了错误目录。检查：

```bash
systemctl cat xr-teleop-web.service
ls /data02/app/eai-teleop-studio/teleop_web
```

### 页面能打开，但相机预览灰屏

确认设备页面的 WebRTC 服务 IP 是实际运行相机服务的机器，而不是固定填 Web 服务机器。自签 HTTPS 证书需要浏览器先信任一次。

### 配置修改后页面仍然旧

运行中的 Web 进程不会自动重读所有配置，重启：

```bash
sudo systemctl restart xr-teleop-web.service
```

### OSS 或转换记录很多后变慢

任务日志已经按 `logs/tasks/<task_name>/<date>/` 分目录存放。页面运行态记录会保留必要索引，不应再扫描整个日志目录。若手工归档旧任务，只移动 `logs/tasks/<task_name>/` 下的历史日期目录即可。
