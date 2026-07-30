# H2 Orbbec 相机选择器与黑白画面排障

## 固定配置规则

H2 上的 Orbbec 相机不要用固定 `/dev/videoX` 或 `physical_path` 作为长期配置，因为 SDK 调用深度流、USB 重新枚举或重启后这些值会漂移。

`teleop/teleimager/cam_config_server.yaml` 中每路相机固定使用：

```yaml
direct_video_id: true
serial_number: CPxxxxxxxxxx
usb_interface: '1.4'
video_index: 0
video_id: null
physical_path: null
```

含义：

- `serial_number` 定位物理相机。
- `usb_interface` 定位同一物理相机下的一组 UVC 接口。
- `video_index` 定位该接口下的具体视频节点。
- `video_id` 和 `physical_path` 必须保持 `null`，避免 USB 重新枚举后继续指向旧节点。

## 当前 RGB selector

```text
head_camera        CP0BB53000FS  usb_interface=1.4  video_index=0
torso_camera       CPCBC530002E  usb_interface=1.4  video_index=0
left_wrist_camera  CP0F463000HS  usb_interface=1.4  video_index=0
right_wrist_camera CP06563000E6  usb_interface=1.4  video_index=0
```

## 黑白/红外散斑的原因

如果 WebRTC 画面是黑白或满屏红外散斑，通常是打开了 Orbbec 的 `GREY`、`Z16`、`BA81` 等深度/红外节点，而不是 RGB 节点。

示例：

```text
CP0BB53000FS + interface=1.0 + index=2 -> GREY
CP0BB53000FS + interface=1.4 + index=0 -> YUYV/MJPG RGB
```

## 后续排障步骤

1. 查看服务实际占用的节点：

```bash
fuser -v /dev/video* 2>&1
```

2. 查看相机节点格式：

```bash
v4l2-ctl -d /dev/video22 --list-formats-ext | head -60
```

RGB 节点应看到 `YUYV` 或 `MJPG`。如果看到 `GREY`、`Z16`、`BA81`，说明选错节点。

3. 查看每个 video 节点的稳定 selector：

```bash
python3 - <<'PY'
from pathlib import Path

def read(path):
    try:
        return Path(path).read_text(errors="ignore").strip()
    except OSError:
        return ""

def serial(device):
    p = Path(device)
    for _ in range(10):
        s = read(p / "serial")
        if s:
            return s
        if p.parent == p:
            break
        p = p.parent
    return ""

for node in sorted(Path("/sys/class/video4linux").glob("video*"), key=lambda p: int(p.name[5:])):
    dev = (node / "device").resolve()
    interface = dev.name.split(":")[-1] if ":" in dev.name else ""
    print(f"/dev/{node.name} index={read(node/'index')} interface={interface} serial={serial(dev)} path={dev}")
PY
```

4. 修改 `cam_config_server.yaml` 中对应相机的 `serial_number + usb_interface + video_index`，保持：

```yaml
video_id: null
physical_path: null
```

5. 重启服务：

```bash
sudo systemctl restart teleimager-camera-capture.service
sudo systemctl status teleimager-camera-capture.service --no-pager
```

6. 刷新浏览器 WebRTC 页面。旧页面可能还连接着旧流。

## SDK/ROS 占用问题

如果某台 Orbbec 从 `/sys/class/video4linux` 消失，或者 `lsusb -t` 里对应接口变成 `Driver=usbfs`，通常是 Orbbec SDK/ROS 节点 claim 了 USB 设备。此时 teleimager/OpenCV 不能同时读取同一台相机。

处理方式：

```bash
sudo pkill -f orbbec_camera_node
sudo pkill -f 'roslaunch orbbec_camera'
sudo systemctl restart teleimager-camera-capture.service
```

如果仍不恢复，需要重新插拔或 USB reset 对应 Orbbec。
