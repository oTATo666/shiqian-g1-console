# -*- coding: utf-8 -*-
"""G1 SDK environment self-check.

Checks:
1. Core dependencies import (cyclonedds / numpy / opencv / unitree_sdk2py)
2. G1 modules (loco / arm / audio) import
3. Lists local network interfaces (for DDS connection to the robot)
"""
import importlib.metadata as md
import socket
import sys


def check(name, fn):
    try:
        fn()
        print(f"  [OK]   {name}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {name}: {e}")
        return False


def main():
    ok = True
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})\n")

    print("== Dependencies ==")
    for pkg in ["cyclonedds", "numpy", "opencv-python", "unitree_sdk2py"]:
        ok &= check(f"{pkg} {md.version(pkg)}", lambda p=pkg: __import__(p.split("-")[0].replace("opencv", "cv2")))

    print("\n== G1 SDK modules ==")
    ok &= check("core.channel (DDS channel)", lambda: __import__("unitree_sdk2py.core.channel", fromlist=["ChannelFactoryInitialize"]))
    ok &= check("g1.loco (motion control)", lambda: __import__("unitree_sdk2py.g1.loco.g1_loco_client", fromlist=["LocoClient"]))
    ok &= check("g1.arm (arm actions)", lambda: __import__("unitree_sdk2py.g1.arm.g1_arm_action_client", fromlist=["G1ArmActionClient"]))
    ok &= check("g1.audio (voice)", lambda: __import__("unitree_sdk2py.g1.audio.g1_audio_client", fromlist=["AudioClient"]))

    print("\n== Network interfaces (use one of these adapter names to connect to G1) ==")
    try:
        import subprocess

        out = subprocess.run(
            ["ipconfig"], capture_output=True, text=True, encoding="gbk", errors="replace"
        ).stdout
        adapter = None
        for line in out.splitlines():
            s = line.strip()
            if ("adapter" in s.lower() or "适配器" in s) and s.endswith(":"):
                adapter = s.rstrip(":")
                for sep in ("adapter", "适配器"):
                    if sep in adapter:
                        adapter = adapter.split(sep, 1)[-1].strip() or adapter
                print(f"  - {adapter}")
            elif adapter and ("IPv4" in s):
                print(f"      {s.rsplit(':', 1)[-1].strip()}")
    except Exception as e:  # noqa: BLE001
        print(f"  (cannot list interfaces: {e}; run 'ipconfig' to see adapter names)")

    print("\n== Result ==")
    print("  Environment READY" if ok else "  Something FAILED - see [FAIL] lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
