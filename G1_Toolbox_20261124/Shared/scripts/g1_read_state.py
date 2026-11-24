# -*- coding: utf-8 -*-
"""G1 read-only state check (no motion commands).

Subscribes to the robot's DDS state topics and prints a summary.
Nothing is published - this script never moves the robot.

Usage:
    python scripts/g1_read_state.py 以太网
"""
import subprocess
import sys
import time

from unitree_sdk2py.core import channel
from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_ as HgLowState
from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

latest = {"lowstate": None, "sport": None}


def iface_ipv4(iface_name):
    """Get the IPv4 address of a Windows adapter by its friendly name."""
    out = subprocess.run(
        ["ipconfig"], capture_output=True, text=True, encoding="gbk", errors="replace"
    ).stdout
    adapter = None
    for line in out.splitlines():
        s = line.strip()
        if ("adapter" in s.lower() or "适配器" in s) and s.endswith(":"):
            adapter = s.rstrip(":")
        elif adapter and iface_name in adapter and "IPv4" in s:
            return s.rsplit(":", 1)[-1].strip()
    return None


def init_dds(iface):
    """Init DDS. Falls back to address-based config when the adapter
    name is non-ASCII (cyclonedds-python encodes config as ASCII)."""
    try:
        iface.encode("ascii")
        ChannelFactoryInitialize(0, iface)
        return iface
    except UnicodeEncodeError:
        ip = iface_ipv4(iface)
        if not ip:
            raise RuntimeError(f"adapter '{iface}' has no IPv4 address (cable connected?)")
        # channel.py did `from .channel_config import ChannelConfigHasInterface`,
        # so patch the name in its own namespace, not in channel_config.
        channel.ChannelConfigHasInterface = channel.ChannelConfigHasInterface.replace(
            'name="$__IF_NAME__$"', f'address="{ip}"'
        )
        ChannelFactoryInitialize(0, iface)
        return f"{iface} (via {ip})"


def on_lowstate(msg):
    latest["lowstate"] = msg


def on_sport(msg):
    latest["sport"] = msg


def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else None
    if not iface:
        print("Usage: python scripts/g1_read_state.py <network_interface>")
        print('Example: python scripts/g1_read_state.py "以太网"')
        return 1

    print(f"Initializing DDS on interface: {iface}")
    used = init_dds(iface)
    print(f"DDS bound to: {used}")

    ChannelSubscriber("rt/lowstate", HgLowState).Init(on_lowstate, 10)
    ChannelSubscriber("rt/sportmodestate", SportModeState_).Init(on_sport, 10)

    print("Listening for 3 seconds (read-only, no commands sent)...\n")
    deadline = time.time() + 3.0
    while time.time() < deadline and latest["lowstate"] is None and latest["sport"] is None:
        time.sleep(0.1)
    time.sleep(0.5)  # collect a couple more frames

    ok = False

    ls = latest["lowstate"]
    if ls is not None:
        ok = True
        joints = [m.q for m in ls.motor_state[:29]]
        temps = []
        for m in ls.motor_state[:29]:
            t = getattr(m, "temperature", None)
            if t is not None:
                temps.append(max(t) if isinstance(t, (list, tuple)) else t)
        print("== LowState (rt/lowstate) ==")
        print(f"  mode_pr={ls.mode_pr}  mode_machine={ls.mode_machine}  tick={ls.tick}")
        print(f"  imu rpy(deg) : roll={ls.imu_state.rpy[0]*57.3:.1f} pitch={ls.imu_state.rpy[1]*57.3:.1f} yaw={ls.imu_state.rpy[2]*57.3:.1f}")
        print(f"  imu acc(m/s2): {[round(a, 2) for a in ls.imu_state.accelerometer]}")
        print(f"  joint q[0..4]: {[round(j, 3) for j in joints[:5]]}")
        if temps:
            print(f"  motor temp   : min={min(temps)} max={max(temps)} degC")
    else:
        print("  [NO DATA] rt/lowstate")

    sp = latest["sport"]
    if sp is not None:
        ok = True
        print("\n== SportModeState (rt/sportmodestate) ==")
        print(f"  position     : x={sp.position[0]:.2f} y={sp.position[1]:.2f} z={sp.position[2]:.2f}")
        print(f"  velocity     : vx={sp.velocity[0]:.2f} vy={sp.velocity[1]:.2f} wz={sp.yaw_speed:.2f}")
        print(f"  gait type    : {sp.gait_type}   body height: {sp.body_height:.2f}")
    else:
        print("  [NO DATA] rt/sportmodestate")

    print("\n== Result ==")
    if ok:
        print("  DDS link OK - robot state is being received. Ready to program!")
        return 0
    print("  No state received. Check: robot powered on? correct interface name? firewall?")
    return 2


if __name__ == "__main__":
    sys.exit(main())
