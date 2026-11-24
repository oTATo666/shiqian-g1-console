#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
视觉桥（vision bridge）—— 首次见面确认
=====================================

为什么单独一个进程：
    主控台便携版的 runtime 是 Python 3.14，没有 numpy/cv2；
    检测要跑 OpenCV，所以和传感器桥一样用 conda g1（Python 3.10）单跑，
    主控台只代理 /state 和画面。

数据来源：
    http://192.168.123.164:8081/stream  机载 MJPEG（g1cam.service 提供）
    取不到时退回 http://127.0.0.1:5011/video/frame.jpg（传感器桥转发的那一路）

检测策略（都跑在本机 CPU 上，不依赖外网）：
    face    ：YuNet（230KB 的 DNN 人脸检测），1~4 米正面/半侧脸最稳，判断"有人在看你"
    person  ：可选，放 models/yolov8n.onnx 才启用（人形框），没有就只靠人脸
    present ：两者任一命中并持续 PRESENT_ON 秒算"有人在场"，
              连续 PRESENT_OFF 秒都没有才判"走了"（滞回，避免闪断）

为什么不用 YOLO：
    YOLOv8n/YOLO11n 更准，但要先有 onnx 权重（这台机器下载 GitHub Release 被拦）。
    代码里留了位置：把 yolov8n.onnx 放到 models/ 下，自动叠一层人形检测（cv2.dnn），
    没有也能跑，只靠 YuNet 的人脸。

对外接口（默认 127.0.0.1:5013）：
    GET /state         {"present":bool,"faces":n,"persons":n,"fps":..,...}
    GET /snapshot.jpg  最新一帧（画了框）
    GET /stream.mjpg   带框的 MJPEG（页面里直接 <img>）
    POST /config       {"greet":true,...} 目前只改运行期开关
"""

import json
import os
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

HOST = os.environ.get("SHIQIAN_VISION_HOST", "127.0.0.1")
PORT = int(os.environ.get("SHIQIAN_VISION_PORT", "5013") or 5013)
STREAM_URL = os.environ.get("SHIQIAN_VISION_STREAM",
                            "http://192.168.123.164:8081/stream")
FALLBACK_URL = os.environ.get("SHIQIAN_VISION_FALLBACK",
                              "http://127.0.0.1:5011/video/frame.jpg")
MODEL_PATH = os.environ.get(
    "SHIQIAN_VISION_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "yolov8n.onnx"))

PRESENT_ON = float(os.environ.get("SHIQIAN_PRESENT_ON", "2.0"))
PRESENT_OFF = float(os.environ.get("SHIQIAN_PRESENT_OFF", "4.0"))
DETECT_EVERY = float(os.environ.get("SHIQIAN_DETECT_EVERY", "0.33"))

lock = threading.Lock()
_prev_gray = [None]
MOTION_ON = float(os.environ.get("SHIQIAN_MOTION_ON", "2.2"))
state = {
    "ok": False,
    "source": STREAM_URL,
    "fps_in": 0.0,
    "fps_det": 0.0,
    "last_frame_ms": None,
    "faces": [],
    "persons": [],
    "present": False,
    "present_since": None,
    "absent_since": time.time(),
    "first_meet": False,          # 本次运行第一次见到人
    "detector": "haar+hog",
    "error": "",
    "frames": 0,
    "motion": 0.0,          # 画面变化强度（0~255 的均值差）
    "moving": False,
}

latest_jpeg = b""       # 原始帧
latest_annot = b""      # 画了框的帧
cond = threading.Condition()

# ---------------- 检测器 ----------------
# OpenCV 5 已经没有 CascadeClassifier / HOGDescriptor 了，改用 YuNet：
# 一个小到 230KB 的人脸检测 DNN，CPU 单帧 ~10ms，1~4 米正面/半侧脸都稳。
# 有 yolov8n.onnx 时再叠一层"人形"检测（cv2.dnn，模型没下到就跳过）。
def _ascii_path(path):
    """OpenCV 在 Windows 上读不了中文路径的 onnx —— 复制一份到临时目录。"""
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        import shutil
        import tempfile
        dst = os.path.join(tempfile.gettempdir(), "g1_vision_" + os.path.basename(path))
        if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(path):
            shutil.copy2(path, dst)
        return dst


YUNET_PATH = os.environ.get(
    "SHIQIAN_YUNET_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "yunet.onnx"))

face_det = None
if os.path.exists(YUNET_PATH):
    try:
        face_det = cv2.FaceDetectorYN.create(_ascii_path(YUNET_PATH), "", (320, 320), 0.6, 0.3, 500)
    except Exception as e:  # noqa: BLE001
        state["error"] = f"YuNet 加载失败：{e}"

PERSON_PATH = os.environ.get(
    "SHIQIAN_PERSON_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "person_mediapipe.onnx"))

class PersonORT:
    """
    MediaPipe 行人检测的 onnxruntime 版。
    OpenCV 5 的新 DNN 引擎解析不了这个 onnx，所以用 ORT 跑，
    预处理/后处理照抄 opencv_zoo 的 mp_persondet.py（anchors 直接复用它的表）。
    """

    def __init__(self, path, score_thr=0.35, nms_thr=0.3, top_k=5000):
        import onnxruntime as ort
        from mp_persondet import MPPersonDet
        self.sess = ort.InferenceSession(_ascii_path(path), providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.anchors = MPPersonDet._load_anchors(None)
        self.size = np.array([224, 224])
        self.score_thr, self.nms_thr, self.top_k = score_thr, nms_thr, top_k

    def infer(self, image):
        h, w = image.shape[:2]
        img = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = (img - 0.5) * 2
        ratio = min(self.size[0] / img.shape[0], self.size[1] / img.shape[1])
        rh, rw = (np.array(img.shape[:2]) * ratio).astype(np.int32)
        img = cv2.resize(img, (rw, rh))
        pad_h, pad_w = self.size[0] - rh, self.size[1] - rw
        top, left = pad_h // 2, pad_w // 2
        img = cv2.copyMakeBorder(img, top, pad_h - top, left, pad_w - left,
                                 cv2.BORDER_CONSTANT, value=(0, 0, 0))
        blob = np.transpose(img, [2, 0, 1])[None].astype(np.float32)
        pad_bias = (np.array([left, top]) / ratio).astype(np.int32)
        outs = self.sess.run(None, {self.inp: blob})
        # ORT 的输出顺序和 cv2.dnn 相反：先 scores(1,N,1)，再 regressors(1,N,12)
        score_out, box_out = (outs[0], outs[1]) if outs[0].shape[-1] == 1 else (outs[1], outs[0])
        score = 1 / (1 + np.exp(-np.clip(score_out[0, :, 0], -30, 30)))
        box_delta = box_out[0, :, 0:4]
        cxy = box_delta[:, :2] / self.size
        wh = box_delta[:, 2:] / self.size
        scale = max(w, h)
        xy1 = (cxy - wh / 2 + self.anchors) * scale
        xy2 = (cxy + wh / 2 + self.anchors) * scale
        boxes = np.concatenate([xy1, xy2], axis=1) - [pad_bias[0], pad_bias[1], pad_bias[0], pad_bias[1]]
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), score.tolist(), self.score_thr, self.nms_thr, top_k=self.top_k)
        if len(keep) == 0:
            return []
        return [[float(v) for v in boxes[i]] + [float(score[i])] for i in np.array(keep).flatten()]


person_det = None
if os.path.exists(PERSON_PATH):
    try:
        person_det = PersonORT(PERSON_PATH)
    except Exception as e:  # noqa: BLE001
        state["error"] = f"行人模型加载失败：{type(e).__name__}"
        print(f"[vision] 行人模型不可用（{type(e).__name__}: {e}），只用人脸", flush=True)

yolo = None
if os.path.exists(MODEL_PATH):
    try:
        yolo = cv2.dnn.readNetFromONNX(_ascii_path(MODEL_PATH))
    except Exception as e:  # noqa: BLE001
        state["error"] = f"YOLO 加载失败：{e}"

names = []
if face_det is not None:
    names.append("yunet")
if person_det is not None:
    names.append("mediapipe-person")
if yolo is not None:
    names.append("yolo")
state["detector"] = "+".join(names) if names else "none"


def detect(frame):
    """返回 (faces, persons)，坐标是原图尺度。"""
    h, w = frame.shape[:2]
    faces, persons = [], []
    if face_det is not None:
        face_det.setInputSize((w, h))
        _, res = face_det.detect(frame)
        if res is not None:
            for row in res:
                x, y, fw, fh = row[:4]
                faces.append([int(x), int(y), int(fw), int(fh)])
    if person_det is not None:
        for row in person_det.infer(frame):
            x1, y1, x2, y2 = row[:4]
            if x2 - x1 < 20 or y2 - y1 < 40:
                continue
            persons.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])
    elif yolo is not None:
        persons = _yolo(frame)
    return faces, persons


def annotate(frame, faces, persons):
    for (x, y, w, h) in persons:
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 200, 120), 2)
        cv2.putText(frame, "person", (x + 4, max(14, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 120), 1, cv2.LINE_AA)
    for (x, y, w, h) in faces:
        cv2.rectangle(frame, (x, y), (x + w, y + h), (60, 120, 255), 2)
        cv2.putText(frame, "face", (x + 4, max(14, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 120, 255), 1, cv2.LINE_AA)
    return frame


# ---------------- 取流 ----------------
def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_stream():
    """一直读 MJPEG，把最新帧放进 latest_jpeg。"""
    global latest_jpeg
    buf = b""
    while True:
        try:
            resp = _opener().open(STREAM_URL, timeout=6)
            with lock:
                state["source"] = STREAM_URL
                state["error"] = ""
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        if len(buf) > (4 << 20):
                            buf = buf[-1024:]
                        break
                    jpg = buf[s:e + 2]
                    buf = buf[e + 2:]
                    if len(jpg) < 800:
                        continue
                    with lock:
                        latest_jpeg = jpg
                        state["frames"] += 1
                        state["last_frame_ms"] = int(time.time() * 1000)
                    with cond:
                        cond.notify_all()
        except Exception as e:  # noqa: BLE001
            with lock:
                state["error"] = f"{type(e).__name__}: 取流失败"
            time.sleep(2)


def read_single():
    """没有 MJPEG 时的兜底：定时抓单帧。"""
    while True:
        time.sleep(0.5)
        with lock:
            if state["frames"] and time.time() - (state["last_frame_ms"] or 0) / 1000 < 2:
                continue
        try:
            data = _opener().open(FALLBACK_URL, timeout=4).read()
            if data[:2] == b"\xff\xd8":
                with lock:
                    latest_jpeg = data
                    state["frames"] += 1
                    state["last_frame_ms"] = int(time.time() * 1000)
                    state["source"] = FALLBACK_URL
                with cond:
                    cond.notify_all()
        except Exception:
            pass


# ---------------- 检测主循环 ----------------
def detect_loop():
    global latest_annot
    last = 0.0
    fps_hist = deque(maxlen=20)
    while True:
        time.sleep(0.05)
        with lock:
            jpg = latest_jpeg
        if not jpg:
            time.sleep(0.2)
            continue
        now = time.time()
        if now - last < DETECT_EVERY:
            continue
        last = now
        frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue
        t0 = time.time()
        try:
            faces, persons = detect(frame)
        except Exception as e:  # noqa: BLE001
            with lock:
                state["error"] = f"检测失败：{type(e).__name__}: {e}"
            continue
        dt = time.time() - t0
        fps_hist.append(1.0 / dt if dt > 0 else 0)

        # 运动量：120x68 灰度差，人走近/挥手都会触发，不依赖模型
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (120, 68))
        motion = 0.0
        if _prev_gray[0] is not None:
            motion = float(np.abs(small.astype(np.int16) - _prev_gray[0].astype(np.int16)).mean())
            state["motion_ts"] = now if motion > MOTION_ON else state.get("motion_ts", 0)
        _prev_gray[0] = small
        moving = (now - state.get("motion_ts", 0)) < 2.5

        hit = bool(faces or persons or moving)
        with lock:
            state["faces"] = faces
            state["persons"] = persons
            state["motion"] = round(motion, 2)
            state["moving"] = moving
            state["fps_det"] = round(sum(fps_hist) / len(fps_hist), 1)
            if hit:
                state["absent_since"] = None
                if not state["present"]:
                    state["present"] = True
                    state["present_since"] = now
                    if not state["first_meet"]:
                        state["first_meet"] = True
            else:
                if state["present"]:
                    if state["absent_since"] is None:
                        state["absent_since"] = now
                    elif now - state["absent_since"] >= PRESENT_OFF:
                        state["present"] = False
                        state["present_since"] = None
                        state["absent_since"] = now
            state["ok"] = True
        latest_annot = cv2.imencode(".jpg", annotate(frame.copy(), faces, persons),
                                    [int(cv2.IMWRITE_JPEG_QUALITY), 80])[1].tobytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/state", "/health"):
            with lock:
                now = time.time()
                d = dict(state)
                d["present_for"] = round(now - d["present_since"], 1) if d["present_since"] else 0
                d["last_frame_age_ms"] = (int(time.time() * 1000) - d["last_frame_ms"]
                                          if d["last_frame_ms"] else None)
            return self._json({"ok": True, "data": d})
        if p == "/snapshot.jpg":
            with lock:
                data = latest_annot or latest_jpeg
            if not data:
                return self._json({"ok": False, "msg": "暂无画面"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/stream.mjpg":
            boundary = "vframe"
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    with lock:
                        data = latest_annot
                    if not data or data is last:
                        with cond:
                            cond.wait(timeout=1.5)
                        with lock:
                            data = latest_annot
                    if not data or data is last:
                        continue
                    last = data
                    self.wfile.write(f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                                     f"Content-Length: {len(data)}\r\n\r\n".encode()
                                     + data + b"\r\n")
            except Exception:
                return
        return self._json({"ok": False, "msg": "not found"}, 404)


def main():
    print(f"[vision] stream={STREAM_URL} detector={state['detector']}", flush=True)
    threading.Thread(target=read_stream, name="reader", daemon=True).start()
    threading.Thread(target=read_single, name="fallback", daemon=True).start()
    threading.Thread(target=detect_loop, name="detect", daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
