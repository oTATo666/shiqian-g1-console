# -*- coding: utf-8 -*-
"""Tracker presets: which BVH joints carry real tracker data.

Presets (user definition):
  P5  - chest + L/R thigh + L/R calf        (implemented now)
  P6  - TBD, waiting for sample file        (stub)
  P8  - like P6-family but L/R feet swapped for L/R arms (stub, waiting)
  P10 - full SlimeVR official layout        (stub, waiting)

Only joints listed in `tracked` are trusted for retargeting; everything
else is treated as derived/garbage and excluded from mapping.
"""

PRESETS = {
    "P5": {
        "label": "5点: 胸 + 左右大腿 + 左右小腿",
        "enabled": True,
        "tracked": {
            "chest": ["CHEST"],
            "left_thigh": ["LEFT_UPPER_LEG"],
            "left_calf": ["LEFT_LOWER_LEG"],
            "right_thigh": ["RIGHT_UPPER_LEG"],
            "right_calf": ["RIGHT_LOWER_LEG"],
        },
        # joints whose data is always junk under this preset -> zeroed in preview
        "untracked_groups": ["arm", "hand", "head_extra", "foot", "finger"],
    },
    "P6": {
        "label": "6点: (等样本文件)",
        "enabled": False,
        "tracked": {},
        "untracked_groups": [],
    },
    "P8": {
        "label": "8点: 左右脚换左右胳膊 (等样本文件)",
        "enabled": False,
        "tracked": {},
        "untracked_groups": [],
    },
    "P10": {
        "label": "10点: SlimeVR 官方全点位 (等样本文件)",
        "enabled": False,
        "tracked": {},
        "untracked_groups": [],
    },
}


# joint name -> group, used to gray out / zero untrusted joints in preview
_GROUP_RULES = [
    ("arm", ["SHOULDER", "UPPER_ARM", "LOWER_ARM", "ELBOW"]),
    ("hand", ["HAND"]),
    ("finger", ["THUMB", "INDEX", "MIDDLE", "RING", "LITTLE"]),
    ("foot", ["FOOT", "ANKLE", "TOE"]),
    ("head_extra", ["HEAD", "NECK"]),
    ("leg", ["UPPER_LEG", "LOWER_LEG", "KNEE"]),
    ("torso", ["HIP", "WAIST", "CHEST"]),
]


def joint_group(name):
    for grp, keys in _GROUP_RULES:
        if any(k in name for k in keys):
            return grp
    return "other"


def trusted_joints(preset_id, all_names):
    """Return set of joint names whose data is trusted under the preset."""
    p = PRESETS[preset_id]
    tracked = set()
    for names in p["tracked"].values():
        tracked.update(names)
    # structural joints needed for FK chain of tracked joints (kept as-is)
    structural = {"HIP", "WAIST", "UPPER_CHEST"}
    return tracked | structural
