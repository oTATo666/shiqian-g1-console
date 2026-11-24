"""
mock_robot_server.py —— G1 机器人 / 光影控制端的「模拟接收器」· v5
=================================================================

【它是干什么的？】
真机上，这个位置应该是 G1 机器人的控制程序（接收动作信号、播放预设 Pose）。
但联调阶段我们手上不一定有机器人，所以用这个模拟服务代替：
它会把收到的动作打印在屏幕上，并记住最近 10 条，方便检查"到底发没发出去"。

【接口（与真实控制端保持一致）】
    POST /trigger_action/{action_id}   触发动作，action_id 必须是 A-Z
    GET  /health                       健康检查（主程序启动自检会调它）
    GET  /state                        查看最近收到的动作（调试用）
    GET  /poses                        查看当前支持的姿态列表

【怎么启动】
    双击 `2_点我启动系统_Windows.bat`（会自动拉起它）
    或者单独运行：python mock_robot_server.py

【以后接真机怎么改？】
    把真实控制端也做成同样的 POST /trigger_action/{action_id} 接口，
    然后主程序安全控制台里把"动作触发接口"改成真机地址即可，其它代码一行都不用动。
"""

import os
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="G1 & Light Sculpture Action Receiver (Mock)")

# 最近收到的动作（内存里留最近 10 条，重启就清空，够调试用）
RECENT = []
# 已处理过的 request_id：防止网络重试导致同一个动作播两遍（幂等）
SEEN_IDS = set()
MAX_SEEN = 500

POSE_NAMES = {
    "A": "仰望星空", "B": "挥手致意", "C": "比心", "D": "抱拳",
    "E": "叉腰", "F": "张开双臂", "G": "低头沉思", "H": "旋转半圈",
}


@app.post("/trigger_action/{action_id}")
async def trigger_action(action_id: str, request: Request):
    """接收动作信号：A-Z 之外的一律拒绝（和真实控制端的校验保持一致）"""
    aid = (action_id or "").strip().upper()
    if len(aid) != 1 or not ("A" <= aid <= "Z"):
        return JSONResponse({"status": "error", "reason": "action_id 必须是 A-Z 单个字母"},
                            status_code=400)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # 幂等：同一个 request_id 只执行一次
    request_id = str(payload.get("request_id", "")).strip()
    if request_id and request_id in SEEN_IDS:
        return {"status": "ok", "duplicated": True, "executed_pose": aid}
    if request_id:
        if len(SEEN_IDS) > MAX_SEEN:
            SEEN_IDS.clear()
        SEEN_IDS.add(request_id)

    name = payload.get("action_name") or POSE_NAMES.get(aid, f"Pose-{aid}")
    record = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "action_id": aid,
        "action_name": name,
        "request_id": request_id[:8],
    }
    RECENT.append(record)
    del RECENT[:-10]

    print(f"\n✨ [G1 光影控制端] 收到动作信号 -> 播放预设 Pose [{aid}] {name}")
    print(f"   记忆点已随信号送达（带不可见水印，共 {len(payload.get('memory', ''))} 字符）")
    return {"status": "ok", "executed_pose": aid, "action_name": name}


@app.get("/health")
async def health():
    """主程序启动自检会调这个接口，200 就代表控制端在线"""
    return {"status": "ok", "service": "g1-mock-receiver", "received": len(RECENT)}


@app.get("/state")
async def state():
    """查看最近收到的动作，用来确认程序到底发没发出来"""
    return {"recent": RECENT, "poses": POSE_NAMES}


@app.get("/poses")
async def poses():
    return {"poses": POSE_NAMES,
            "note": "真机可用姿态以实际录制的 A-Z 为准；这里只是模拟器展示用"}


@app.get("/")
async def root():
    return {"service": "G1 光影动作模拟接收端", "tip": "POST /trigger_action/F 试试看"}


if __name__ == "__main__":
    port = int(os.environ.get("G1_MOCK_PORT", 8080))
    print(f"🚀 [后台服务] G1 光影动作模拟控制端已启动 http://localhost:{port}")
    print("   接口：POST /trigger_action/{A-Z}   GET /health   GET /state")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
