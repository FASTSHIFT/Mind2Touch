#!/usr/bin/env python3
"""GCODE 路径预览（matplotlib）"""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches


def preview_gcode(gcode_lines, title="GCODE Preview", bed_w=100, bed_h=100, z_down=5.0):
    fig, ax = plt.subplots(1, 1, figsize=(8, 8))
    ax.set_xlim(-2, bed_w + 2)
    ax.set_ylim(-2, bed_h + 2)
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")

    rect = mpatches.Rectangle((0, 0), bed_w, bed_h, fill=False, edgecolor="gray", linestyle="--")
    ax.add_patch(rect)

    # 先解析出所有落笔线段
    segments = []  # [[(x,y), ...], ...]
    x, y, z = 0.0, 0.0, 0.0
    pen_down = False
    current_seg = []

    for line in gcode_lines:
        line = line.strip()
        if not line or line.startswith(";"):
            continue

        parts = line.split()
        cmd = parts[0] if parts else ""
        params = {}
        for p in parts[1:]:
            if len(p) > 1 and p[0] in "XYZF":
                try:
                    params[p[0]] = float(p[1:])
                except ValueError:
                    pass

        if cmd not in ("G0", "G1"):
            continue

        new_z = params.get("Z", z)
        new_x = params.get("X", x)
        new_y = params.get("Y", y)

        if "Z" in params and new_z != z:
            was_down = pen_down
            pen_down = abs(new_z - z_down) < 0.01
            z = new_z

            if was_down and not pen_down:
                # 抬笔：保存当前段
                if len(current_seg) > 1:
                    segments.append(current_seg)
                current_seg = []
            elif not was_down and pen_down:
                # 落笔：从当前位置开始新段
                current_seg = [(x, y)]

        if "X" in params or "Y" in params:
            if pen_down:
                current_seg.append((new_x, new_y))
            x, y = new_x, new_y

    if pen_down and len(current_seg) > 1:
        segments.append(current_seg)

    # 绘制
    for seg in segments:
        xs = [p[0] for p in seg]
        ys = [p[1] for p in seg]
        ax.plot(xs, ys, "b-", linewidth=0.4)

    ax.invert_yaxis()
    plt.tight_layout()
    plt.show()
