# -*- coding: utf-8 -*-
"""环境探针：只在 Isaac venv 里跑，输出一行 JSON 供 trainctl.doctor 解析。"""
import json

r = {}
try:
    import torch
    r["torch"] = torch.__version__
    r["cuda"] = bool(torch.cuda.is_available())
    r["gpu"] = torch.cuda.get_device_name(0) if r["cuda"] else ""
except Exception as e:  # noqa: BLE001
    r["torch"] = f"ERR {type(e).__name__}: {e}"
    r["cuda"] = False
    r["gpu"] = ""

try:
    import isaaclab
    r["isaaclab"] = getattr(isaaclab, "__version__", "?")
except Exception as e:  # noqa: BLE001
    r["isaaclab"] = f"ERR {type(e).__name__}"

print(json.dumps(r, ensure_ascii=False))
