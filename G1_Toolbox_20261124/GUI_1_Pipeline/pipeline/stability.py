# -*- coding: utf-8 -*-
"""动作可行性审查：判断一段关节轨迹"机器人站着做会不会倒"。

为什么需要它
------------
我们导出的是**开环关节角**：机器人只用 PD 位置伺服去跟，没有任何
"我要倒了→改落脚点"的闭环。所以能不能站住，取决于动作本身是否让
重心一直落在脚掌支撑面内。官方跳舞能稳住是因为它有 RL 策略做闭环，
这里做的是**事前体检**：算得出来就放行，算不过就缩幅/降速/拦截。

算法（保守近似）
----------------
1. 用 g1_model.preview_fk 把每帧关节角变成骨架点位
2. 按分段质量估重心 CoM（总质量约 33 kg，见 SEG_MASS）
3. 把着地脚（脚底 z < CONTACT_Z）的脚掌矩形拼成支撑多边形
4. 算 CoM 水平投影到多边形边界的**有符号距离** margin：
   > 0 在支撑面内，< 0 已经出界（现实里就是正在倒）
5. 再叠加两个动态指标：支撑脚水平滑移、关节角速度

判定
----
stable     margin 最小 > 0.03 m 且出界比例 < 2%
marginal   偶尔擦边（margin > -0.02），能试但要慢放
infeasible 明显站不住 → 建议缩幅/降速，或直接拦下
"""
import numpy as np

from . import g1_model as G

# 脚掌尺寸（G1 标准版实测约 0.25 x 0.10 m，这里取保守值）
FOOT_LEN = 0.22
FOOT_WID = 0.09
CONTACT_Z = 0.025          # 脚底低于这个高度算着地
SAFE_MARGIN = 0.030        # 稳定阈值（m）
MARGINAL_MARGIN = -0.020   # 低于这个就判定不可行

# 分段质量（kg），按 G1 约 33 kg 分配；只用于估算 CoM，不追求精确
SEG_MASS = {
    "pelvis": 6.5, "waist": 1.0, "torso": 8.0, "head": 1.5,
    "l_hip": 2.0, "l_knee": 2.4, "l_ankle": 0.6, "l_foot": 0.4,
    "r_hip": 2.0, "r_knee": 2.4, "r_ankle": 0.6, "r_foot": 0.4,
    "l_shoulder": 1.2, "l_elbow": 1.0, "l_wrist": 0.4,
    "r_shoulder": 1.2, "r_elbow": 1.0, "r_wrist": 0.4,
}
TOTAL_MASS = sum(SEG_MASS.values())


def _foot_corners(center, yaw=0.0):
    """脚掌四角（世界 xy），脚中心 + 朝向角。"""
    hl, hw = FOOT_LEN / 2.0, FOOT_WID / 2.0
    local = np.array([[hl, hw], [hl, -hw], [-hl, -hw], [-hl, hw]])
    c, s = np.cos(yaw), np.sin(yaw)
    R = np.array([[c, -s], [s, c]])
    return (local @ R.T) + np.array([center[0], center[1]])


def _hull(points):
    """二维凸包（Andrew monotone chain），返回逆时针顶点，够用且不依赖 scipy。"""
    pts = sorted(set(map(tuple, np.round(points, 9))))
    if len(pts) <= 2:
        return np.array(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1])


def _margin_to_polygon(px, py, poly):
    """点到凸多边形的有符号距离：内部为正，外部为负（到最近边）。"""
    if len(poly) < 3:
        return -1.0
    inside = True
    best = 1e9
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        seg = np.hypot(ex, ey)
        if seg < 1e-9:
            continue
        # 叉积判内外（凸包逆时针，内部叉积 >= 0）
        cross = ex * (py - ay) - ey * (px - ax)
        if cross < 0:
            inside = False
        dist = abs(cross) / seg
        best = min(best, dist)
    return best if inside else -best


def com_series(q, fps):
    """逐帧估重心（世界坐标）与脚底点。"""
    n = len(q)
    coms = np.zeros((n, 3))
    feet = {"l": np.zeros((n, 3)), "r": np.zeros((n, 3))}
    for f in range(n):
        p = G.preview_fk(q[f])
        acc = np.zeros(3)
        wsum = 0.0
        for name, m in SEG_MASS.items():
            if name in p:
                acc += m * p[name]
                wsum += m
        coms[f] = acc / max(wsum, 1e-9)
        feet["l"][f] = p["l_foot"]
        feet["r"][f] = p["r_foot"]
    return coms, feet


def review(q, fps, sample=2):
    """审查整段轨迹。返回 dict（含 verdict / 指标 / 逐帧 margin）。"""
    q = np.asarray(q, dtype=float)
    if len(q) < 3:
        return {"verdict": "unknown", "msg": "帧数太少，无法审查"}
    coms, feet = com_series(q, fps)
    n = len(q)
    idx = list(range(0, n, max(1, sample)))
    margins = []
    contact_frames = 0
    air_samples = 0
    single_samples = 0
    slips = []
    for f in idx:
        poly_pts = []
        for side in ("l", "r"):
            if feet[side][f, 2] < CONTACT_Z:
                poly_pts.append(_foot_corners(feet[side][f]))
        if len(poly_pts) == 1:
            single_samples += 1
        if poly_pts:
            contact_frames += 1
            poly = _hull(np.vstack(poly_pts))
            margins.append(_margin_to_polygon(coms[f, 0], coms[f, 1], poly))
        else:
            air_samples += 1           # 腾空帧单独统计，不混进 margin
        # 滑移：相邻样本里着地脚的水平位移
        g = idx[idx.index(f) + 1] if idx.index(f) + 1 < len(idx) else None
        if g is not None:
            for side in ("l", "r"):
                if feet[side][f, 2] < CONTACT_Z and feet[side][g, 2] < CONTACT_Z:
                    d = np.hypot(*(feet[side][g, :2] - feet[side][f, :2]))
                    slips.append(d / ((g - f) / fps))

    margins = np.array(margins) if margins else np.array([0.0])
    dq = np.abs(np.diff(q, axis=0)) * fps if len(q) > 1 else np.zeros((1, q.shape[1]))
    vel_max = float(dq.max())
    vel_p95 = float(np.percentile(dq, 95))
    outside = float((margins < 0).mean())
    m_min = float(margins.min())
    slip_max = float(max(slips)) if slips else 0.0
    air_ratio = float(air_samples / max(1, len(idx)))
    single_ratio = float(single_samples / max(1, len(idx)))
    dynamic_ratio = air_ratio + single_ratio

    # 判定顺序很重要：
    #  · 腾空/单脚占比高的步态，静态支撑判据本来就不适用，必须靠 RL 策略，
    #    这种情况不能叫"不可行"，要叫"需要动态平衡"。
    #  · 全程双脚着地还出界，才是真的站不住。
    if dynamic_ratio >= 0.15:
        verdict = "dynamic"
        text = (f"单脚支撑 {single_ratio*100:.0f}% + 腾空 {air_ratio*100:.0f}%："
                "这是步态类动作，开环回放必然会倒，只能靠 RL 策略或锁腿")
    elif m_min > SAFE_MARGIN and outside < 0.02:
        verdict, text = "stable", "重心始终在脚掌支撑面内，可以放行"
    elif m_min > MARGINAL_MARGIN:
        verdict, text = "marginal", "偶尔擦边：建议 0.5x 慢放并有人扶"
    else:
        verdict, text = "infeasible", "全程着地但重心明显出界，开环回放会倒"

    return {
        "verdict": verdict, "msg": text,
        "margin_min": round(m_min, 4),
        "outside_ratio": round(outside, 3),
        "air_ratio": round(air_ratio, 3),
        "single_ratio": round(single_ratio, 3),
        "dynamic_ratio": round(dynamic_ratio, 3),
        "slip_max": round(slip_max, 3),
        "vel_max": round(vel_max, 2),
        "vel_p95": round(vel_p95, 2),
        "frames": int(n), "fps": float(fps),
        "margins": margins.tolist(),
        "sample": int(sample),
    }


def make_feasible(q, fps, min_alpha=0.25, steps=8):
    """自动可行化：把动作朝静止姿态收缩，找"最大保留幅度且能站稳"的版本。

    返回 (q_new, info)。找不到就 info["ok"]=False，调用方应拦截。
    """
    q = np.asarray(q, dtype=float)
    base = G.Q_DEFAULT[None, :]
    best = None
    for k in range(steps):
        alpha = 1.0 - (1.0 - min_alpha) * k / max(1, steps - 1)
        q2 = base + alpha * (q - base)
        rep = review(q2, fps)
        if best is None or rep["margin_min"] > best[1]["margin_min"]:
            best = (alpha, rep, q2)
        if rep["verdict"] == "stable":
            return q2, {"ok": True, "alpha": round(alpha, 2), "report": rep,
                        "msg": f"缩幅到原来的 {alpha*100:.0f}%，重心回到支撑面内"}
    alpha, rep, q2 = best
    return q2, {"ok": False, "alpha": round(alpha, 2), "report": rep,
                "msg": f"即使缩到 {alpha*100:.0f}% 仍站不住（margin {rep['margin_min']} m），建议改动作"}

# ======================================================================
# 开环安全版：把"腿在动"的动作改造成"腿站住、只动上半身"
# ----------------------------------------------------------------------
#  开环回放能不能不摔，取决于重心是否一直在脚掌支撑面内。
#  步态类动作（走路/跳）必然单脚或腾空，开环一定倒。
#  最省事、又最接近原意的改法：把腿部 12 个关节锁到站姿，
#  腰和双臂的动作原样保留 —— 观众看到的还是"这段动作"，但机器人站得住。
# ======================================================================
LEG_IDX = list(range(12))          # 髋/膝/踝 x2
UPPER_IDX = list(range(12, 29))    # 腰 + 双臂


def _leg_stance(q):
    """取一个双脚着地的参考站姿：优先用整段里 margin 最好的一帧，否则用 Q_DEFAULT。"""
    try:
        coms, feet = com_series(q, 100.0)
    except Exception:  # noqa: BLE001
        return G.Q_DEFAULT.copy()
    best, best_m = None, -9.9
    for f in range(0, len(q), max(1, len(q) // 40)):
        if feet["l"][f, 2] < CONTACT_Z and feet["r"][f, 2] < CONTACT_Z:
            poly = _hull(np.vstack([_foot_corners(feet["l"][f]), _foot_corners(feet["r"][f])]))
            m = _margin_to_polygon(coms[f, 0], coms[f, 1], poly)
            if m > best_m:
                best_m, best = m, q[f].copy()
    return best if best is not None else G.Q_DEFAULT.copy()


def make_openloop_safe(q, fps, ramp=0.45):
    """锁腿版：腿平滑过渡到站姿，腰/臂保留。

    ramp: 过渡时长（秒）。越长越平滑，但原动作的"腿"越早消失。
    返回 (q_new, info)
    """
    q = np.asarray(q, dtype=float)
    n = len(q)
    stance = _leg_stance(q)
    legs = q[:, LEG_IDX].copy()
    target = stance[LEG_IDX]

    # 逐帧风险：单脚/腾空，或重心出界
    coms, feet = com_series(q, fps)
    risky = np.zeros(n, dtype=bool)
    for f in range(n):
        l_ok = feet["l"][f, 2] < CONTACT_Z
        r_ok = feet["r"][f, 2] < CONTACT_Z
        if not (l_ok and r_ok):
            risky[f] = True
            continue
        poly = _hull(np.vstack([_foot_corners(feet["l"][f]), _foot_corners(feet["r"][f])]))
        risky[f] = _margin_to_polygon(coms[f, 0], coms[f, 1], poly) < 0.01

    # 风险帧膨胀 ramp 秒，避免腿部突变
    pad = max(1, int(ramp * fps))
    mask = np.zeros(n, dtype=float)
    idx = np.where(risky)[0]
    for f in idx:
        mask[max(0, f - pad):min(n, f + pad + 1)] = 1.0
    # 平滑权重（0=原动作，1=锁腿）
    k = max(3, int(0.25 * fps))
    kernel = np.hanning(k)
    kernel /= kernel.sum()
    w = np.convolve(mask, kernel, mode="same")
    w = np.clip(w / max(w.max(), 1e-6) * 1.6, 0.0, 1.0)
    w = np.maximum(w, mask)          # 风险帧至少全锁

    out = q.copy()
    out[:, LEG_IDX] = legs * (1 - w[:, None]) + target[None, :] * w[:, None]

    rep = review(out, fps)
    # 腿锁住后若边距仍然很紧，再把腰部三关节朝站姿收一点（手臂不动）
    if rep["margin_min"] < SAFE_MARGIN:
        best_rep, best_q = rep, out
        for alpha in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3):
            cand = out.copy()
            cand[:, 12:15] = stance[12:15] + alpha * (out[:, 12:15] - stance[12:15])
            r = review(cand, fps)
            if r["margin_min"] > best_rep["margin_min"]:
                best_rep, best_q = r, cand
            if r["margin_min"] >= SAFE_MARGIN:
                break
        rep, out = best_rep, best_q

    return out, {
        "ok": rep["verdict"] in ("stable", "marginal"),
        "risky_ratio": round(float(risky.mean()), 3),
        "report": rep,
        "msg": (f"锁腿 {risky.mean()*100:.0f}% 的帧，判定 {rep['verdict'].upper()}，"
                f"最小边距 {rep['margin_min']*100:+.1f} cm"),
    }

# ======================================================================
# 时间轴分段：把一段长动作切成 0.5 秒的小格，逐格判"这段能不能开环"
# ======================================================================
def frame_metrics(q, fps):
    """逐帧算：支撑状态 + 重心边距。分段和审查共用，避免重复 FK。"""
    coms, feet = com_series(q, fps)
    n = len(q)
    margin = np.full(n, np.nan)
    single = np.zeros(n, dtype=bool)
    air = np.zeros(n, dtype=bool)
    for f in range(n):
        pts = []
        for side in ("l", "r"):
            if feet[side][f, 2] < CONTACT_Z:
                pts.append(_foot_corners(feet[side][f]))
        if not pts:
            air[f] = True
            continue
        if len(pts) == 1:
            single[f] = True
        poly = _hull(np.vstack(pts))
        margin[f] = _margin_to_polygon(coms[f, 0], coms[f, 1], poly)
    return {"margin": margin, "single": single, "air": air, "coms": coms, "feet": feet}


def segment_report(q, fps, window=0.5):
    """按 window 秒切片，返回 [{t0,t1,level,margin_min,verdict}]。

    level: safe(绿) / warn(黄) / risky(红)
    """
    q = np.asarray(q, dtype=float)
    n = len(q)
    m = frame_metrics(q, fps)
    step = max(1, int(round(window * fps)))
    out = []
    for start in range(0, n, step):
        end = min(n, start + step)
        seg_m = m["margin"][start:end]
        valid = ~np.isnan(seg_m)
        air_r = float(m["air"][start:end].mean())
        single_r = float(m["single"][start:end].mean())
        mmin = float(np.nanmin(seg_m)) if valid.any() else -1.0
        if air_r > 0.3:
            level, verdict = "risky", "腾空"
        elif single_r > 0.3:
            level, verdict = "risky", "单脚"
        elif (not valid.any()) or mmin < 0:
            level, verdict = "risky", "重心出界"
        elif mmin < SAFE_MARGIN:
            level, verdict = "warn", "擦边"
        else:
            level, verdict = "safe", "可开环"
        out.append({"t0": round(start / fps, 2), "t1": round(end / fps, 2),
                    "a": start, "b": end, "level": level, "verdict": verdict,
                    "margin_min": round(mmin, 4),
                    "single": round(single_r, 2), "air": round(air_r, 2)})
    return out


def safe_ranges(segs, min_sec=0.6, fps=100.0, allow_warn=False):
    """把连续的 safe（可选 warn）片段合并成区间 [(start_frame, end_frame), ...]。"""
    ok_levels = {"safe", "warn"} if allow_warn else {"safe"}
    ranges, cur = [], None
    for s in segs:
        if s["level"] in ok_levels:
            cur = (s["a"], s["b"]) if cur is None else (cur[0], s["b"])
        else:
            if cur and (cur[1] - cur[0]) / fps >= min_sec:
                ranges.append(cur)
            cur = None
    if cur and (cur[1] - cur[0]) / fps >= min_sec:
        ranges.append(cur)
    return ranges

# ======================================================================
# 分段自适应降速：只把"太快"的片段拉慢，其余保持原速
# ----------------------------------------------------------------------
#  有些动作其实站得住，只是关节速度超过 PD 跟得上限（实测 >1.5 rad/s 就容易
#  冲过头）。整段降速太浪费，按 0.25 秒窗口算峰值速度，只拉慢超标窗口。
# ======================================================================
def speed_series(q, fps):
    dq = np.abs(np.diff(q, axis=0)) * fps
    if len(dq) == 0:
        return np.zeros(1)
    return np.concatenate([dq.max(axis=1), dq[-1:].max(axis=1)])


def retime_by_speed(q, fps, target_vel=1.2, max_stretch=3.0, window=0.25):
    """按峰值关节速度分段拉伸时间轴。返回 (q_new, info)。

    target_vel: 允许的最大关节角速度（rad/s）
    max_stretch: 单段最多拉慢几倍
    """
    q = np.asarray(q, dtype=float)
    n = len(q)
    sp = speed_series(q, fps)
    step = max(2, int(round(window * fps)))
    pieces, stretched, worst = [], 0, 0.0
    for a in range(0, n, step):
        b = min(n, a + step)
        v = float(sp[a:b].max())
        worst = max(worst, v)
        k = 1.0
        if v > target_vel:
            k = min(max_stretch, v / target_vel)
            stretched += 1
        L = max(2, int(round((b - a) * k)))
        src = np.linspace(a, max(a, b - 1), L)
        idx = np.arange(n)
        piece = np.stack([np.interp(src, idx, q[:, j]) for j in range(q.shape[1])], axis=1)
        pieces.append(piece)
    out = np.concatenate(pieces, axis=0) if pieces else q
    rep = review(out, fps)
    return out, {
        "ok": rep["verdict"] in ("stable", "marginal") or rep["verdict"] == "dynamic",
        "stretched_windows": stretched,
        "worst_vel": round(worst, 2),
        "old_seconds": round(n / fps, 2),
        "new_seconds": round(len(out) / fps, 2),
        "report": rep,
        "msg": (f"{stretched} 个窗口超速（峰值 {worst:.2f} rad/s）已拉慢，"
                f"时长 {n/fps:.1f}s → {len(out)/fps:.1f}s"),
    }
