# -*- coding: utf-8 -*-
"""Embodied light-shadow sculpture: project robot pose onto a screen plane.

Model: a point light (projector) at L, a vertical screen plane at x = screen_x
(robot faces -x? no: robot faces +x, screen in FRONT of robot at screen_x > 0,
light source between robot and screen or behind robot).

Physical setup assumed:
  - screen plane: x = screen_x (vertical, normal = -x direction facing robot)
  - light: point source at light_pos (behind robot, x < 0, so robot's shadow
    is cast forward onto the screen)
  - robot at origin

For each limb (capsule approximated as thick segment), project endpoints from
the light onto the screen plane. The projected "shadow" is drawn as thick 2D
lines; the silhouette outline is the convex hull of projected joint points
(thickened). Also returns silhouette area (m^2 on screen) as a metric.
"""
import numpy as np

from . import g1_model as G

# limb radius per node pair (m) — makes the stick figure a body
LIMB_RADIUS = {
    ("pelvis", "waist"): 0.16, ("waist", "torso"): 0.16, ("torso", "head"): 0.11,
    ("l_hip", "l_knee"): 0.09, ("l_knee", "l_ankle"): 0.07,
    ("r_hip", "r_knee"): 0.09, ("r_knee", "r_ankle"): 0.07,
    ("l_shoulder", "l_elbow"): 0.06, ("l_elbow", "l_wrist"): 0.05,
    ("r_shoulder", "r_elbow"): 0.06, ("r_elbow", "r_wrist"): 0.05,
    ("l_ankle", "l_foot"): 0.06, ("r_ankle", "r_foot"): 0.06,
}


def project_point(p, light, screen_x):
    """Project 3D point p from point light onto plane x=screen_x."""
    d = p - light
    if d[0] <= 1e-6:
        return None                      # point not in front of light
    t = (screen_x - light[0]) / d[0]
    hit = light + t * d
    return hit[1], hit[2]                # (y, z) on screen


def shadow_frame(gpos, light, screen_x):
    """Project all segments. Returns dict:
    segments2d: [((y1,z1),(y2,z2),width)]
    hull2d: (K,2) silhouette outline points
    area: convex hull area (screen m^2)
    """
    light = np.asarray(light, float)
    segs = []
    pts = []
    for a, b in G.G1_SEGMENTS:
        if a not in gpos or b not in gpos:
            continue
        pa, pb = project_point(gpos[a], light, screen_x), project_point(gpos[b], light, screen_x)
        if pa is None or pb is None:
            continue
        r = LIMB_RADIUS.get((a, b), 0.06)
        # shadow width magnified by projection ratio
        mag = (screen_x - light[0]) / max(0.3, (gpos[a][0] + gpos[b][0]) / 2 - light[0])
        segs.append((pa, pb, 2 * r * abs(mag)))
        pts.append(pa)
        pts.append(pb)
    pts = np.array(pts) if pts else np.zeros((0, 2))
    hull = _convex_hull(pts) if len(pts) >= 3 else pts
    area = _poly_area(hull) if len(hull) >= 3 else 0.0
    return {"segments": segs, "hull": hull, "area": area}


def _convex_hull(pts):
    """Andrew monotone chain, pts (N,2)."""
    pts = sorted(set(map(tuple, pts)))
    if len(pts) < 3:
        return np.array(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1])


def _poly_area(poly):
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))
