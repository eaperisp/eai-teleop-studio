"""Lazy-loaded OpenCV and MediaPipe camera runtime."""

from __future__ import annotations

import math
import os
import time
from typing import Any

from hand_web.vision.models import HandDetection, VisionDependencyError


def _xyz(point: Any) -> tuple[float, float, float]:
    return float(point.x), float(point.y), float(getattr(point, "z", 0.0))


class MediaPipeRuntime:
    def __init__(self, config: dict[str, Any]) -> None:
        try:
            import cv2
            import mediapipe as mp
        except Exception as exc:
            raise VisionDependencyError(
                "视觉控制依赖未安装，请执行 pip install -r hand_web/requirements-vision.txt"
            ) from exc

        self.cv2 = cv2
        self.mp = mp
        self.side = str(config.get("side", "right")).lower()
        self.camera = int(config.get("camera", 0))
        self.width = int(config.get("width", 960))
        self.height = int(config.get("height", 540))
        backends = [cv2.CAP_ANY]
        if os.name == "nt":
            backends.extend([cv2.CAP_DSHOW, cv2.CAP_MSMF])
        self.backends = list(dict.fromkeys(backends))
        self.capture = self._open_capture()
        try:
            hands_api = mp.solutions.hands
            self._drawing = mp.solutions.drawing_utils
        except AttributeError as exc:
            self.capture.release()
            raise VisionDependencyError("当前 MediaPipe 版本不包含 Hands API，请安装 requirements-vision.txt 中指定的版本") from exc
        self._hands_api = hands_api
        self._hands = hands_api.Hands(
            static_image_mode=False,
            max_num_hands=2,
            model_complexity=1,
            min_detection_confidence=float(config.get("min_detection_confidence", 0.65)),
            min_tracking_confidence=float(config.get("min_tracking_confidence", 0.65)),
        )

    def read(self) -> tuple[Any, HandDetection | None]:
        ok, frame = self._read_frame()
        if not ok or frame is None:
            raise RuntimeError("摄像头画面读取失败")
        frame = self.cv2.flip(frame, 1)
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self._hands.process(rgb)
        detection = None
        landmarks_list = result.multi_hand_landmarks or []
        handedness_list = result.multi_handedness or []
        world_landmarks_list = result.multi_hand_world_landmarks or []
        for index, (landmarks, handedness) in enumerate(zip(landmarks_list, handedness_list)):
            if not handedness.classification:
                continue
            label = handedness.classification[0].label.lower()
            if label != self.side:
                continue
            points = landmarks.landmark
            detection = HandDetection(
                landmarks=points,
                side=label,
                gesture=self._gesture(points),
                render_data=landmarks,
                world_landmarks=(
                    world_landmarks_list[index].landmark
                    if index < len(world_landmarks_list) else None
                ),
            )
            break
        return frame, detection

    def _read_frame(self) -> tuple[bool, Any]:
        for _ in range(4):
            ok, frame = self.capture.read()
            if ok and frame is not None:
                return True, frame
            time.sleep(0.04)

        self.capture.release()
        time.sleep(0.15)
        try:
            self.capture = self._open_capture()
        except RuntimeError:
            return False, None

        for _ in range(4):
            ok, frame = self.capture.read()
            if ok and frame is not None:
                return True, frame
            time.sleep(0.04)
        return False, None

    def _open_capture(self) -> Any:
        for backend in self.backends:
            candidate = self.cv2.VideoCapture(self.camera, backend)
            if candidate.isOpened():
                candidate.set(self.cv2.CAP_PROP_FRAME_WIDTH, self.width)
                candidate.set(self.cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                candidate.set(self.cv2.CAP_PROP_BUFFERSIZE, 1)
                return candidate
            candidate.release()
        raise RuntimeError(f"无法打开摄像头索引 {self.camera}，请确认摄像头未被其他程序占用")

    def encode(self, frame: Any, detection: HandDetection | None, lines: list[str]) -> bytes:
        if detection is not None and detection.render_data is not None:
            self._drawing.draw_landmarks(frame, detection.render_data, self._hands_api.HAND_CONNECTIONS)
        for index, line in enumerate(lines):
            y = 28 + index * 25
            self.cv2.putText(frame, line, (14, y), self.cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 0, 0), 4, self.cv2.LINE_AA)
            self.cv2.putText(frame, line, (14, y), self.cv2.FONT_HERSHEY_SIMPLEX, 0.58, (53, 200, 255), 1, self.cv2.LINE_AA)
        ok, encoded = self.cv2.imencode(".jpg", frame, [self.cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            raise RuntimeError("摄像头画面编码失败")
        return encoded.tobytes()

    def close(self) -> None:
        try:
            self._hands.close()
        finally:
            self.capture.release()

    @staticmethod
    def _gesture(points: Any) -> str | None:
        extended = sum(points[tip].y < points[pip].y for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)))
        palm_width = max(math.dist(_xyz(points[5]), _xyz(points[17])), 1e-6)
        thumb_gap = math.dist(_xyz(points[4]), _xyz(points[17])) / palm_width
        if extended == 4 and thumb_gap >= 1.15:
            return "Open Hand"
        if extended == 0 and thumb_gap < 1.15:
            return "Fist"
        return None


__all__ = ["MediaPipeRuntime"]
