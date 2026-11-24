# -*- coding: utf-8 -*-
"""DDS init helper for Windows.

Two problems this solves:
1. cyclonedds-python encodes its XML config as ASCII, so non-ASCII adapter
   friendly names (e.g. 以太网) raise UnicodeEncodeError.  We fall back to
   binding by IPv4 address instead of by name.
2. The adapter that faces the robot is not always called the same thing on
   another machine.  resolve_iface() finds it by looking for the adapter that
   owns a 192.168.123.x address, so the pipeline is portable.
"""
import subprocess

from unitree_sdk2py.core import channel
from unitree_sdk2py.core.channel import ChannelFactoryInitialize

ROBOT_SUBNET = "192.168.123."


def _ipconfig_lines():
    out = subprocess.run(
        ["ipconfig"], capture_output=True, text=True, encoding="gbk", errors="replace"
    ).stdout
    return out.splitlines()


def _adapter_blocks():
    """Yield (adapter_alias, [ipv4 addresses]) for every ipconfig section."""
    alias, ips = None, []
    for line in _ipconfig_lines():
        s = line.strip()
        if ("adapter" in s.lower() or "适配器" in s) and s.endswith(":"):
            if alias:
                yield alias, ips
            head = s.rstrip(":").strip()
            # "以太网适配器 以太网" / "Ethernet adapter Ethernet" -> trailing name
            parts = head.split()
            alias = parts[-1] if parts else head
            ips = []
        elif alias and "IPv4" in s:
            ip = s.rsplit(":", 1)[-1].strip()
            if ip:
                ips.append(ip)
    if alias:
        yield alias, ips


def iface_ipv4(iface_name):
    for alias, ips in _adapter_blocks():
        if iface_name in alias and ips:
            return ips[0]
    return None


def resolve_iface(preferred=None, subnet=ROBOT_SUBNET):
    """Return (alias, ipv4) of the adapter connected to the robot subnet."""
    blocks = list(_adapter_blocks())
    if preferred:
        for alias, ips in blocks:
            if preferred in alias and ips:
                return alias, ips[0]
    for alias, ips in blocks:
        for ip in ips:
            if ip.startswith(subnet):
                return alias, ip
    return None, None


def init_dds(iface, ip=None):
    """Bind DDS to `iface`, falling back to address binding for non-ASCII names."""
    if ip is None:
        ip = iface_ipv4(iface)
    try:
        iface.encode("ascii")
        ChannelFactoryInitialize(0, iface)
        return iface
    except UnicodeEncodeError:
        if not ip:
            raise RuntimeError(
                f"adapter '{iface}' has no IPv4 address (cable connected?)"
            )
        channel.ChannelConfigHasInterface = channel.ChannelConfigHasInterface.replace(
            'name="$__IF_NAME__$"', f'address="{ip}"'
        )
        ChannelFactoryInitialize(0, iface)
        return f"{iface} (via {ip})"


def init_dds_auto(preferred=None):
    """Find the robot-facing adapter automatically, then init DDS on it."""
    alias, ip = resolve_iface(preferred)
    if not alias:
        raise RuntimeError(
            "no adapter has a 192.168.123.x address - run tools\\一键连接机器人.bat first"
        )
    return init_dds(alias, ip)
