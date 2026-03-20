#!/usr/bin/env python3
"""
SVG 转 GCODE - 矢量描边，支持线简化和时间估算
"""

import argparse
import math
from svgpathtools import svg2paths2, Path

Z_UP = 0.0
Z_DOWN = 5.0
FEED_RATE = 20000
SAMPLE_INTERVAL = 0.5


def path_to_points(path, interval):
    if path.length() == 0:
        return []
    n = max(2, int(math.ceil(path.length() / interval)))
    return [(path.point(i / n).real, path.point(i / n).imag) for i in range(n + 1)]


def split_path_at_moves(path):
    if len(path) == 0:
        return [path]
    subpaths = []
    current = []
    for seg in path:
        if current and abs(seg.start - current[-1].end) > 0.1:
            subpaths.append(Path(*current))
            current = []
        current.append(seg)
    if current:
        subpaths.append(Path(*current))
    return subpaths


def simplify_points(pts, tolerance):
    """Douglas-Peucker 线简化"""
    if len(pts) <= 2 or tolerance <= 0:
        return pts

    # 找离首尾连线最远的点
    start, end = pts[0], pts[-1]
    max_dist = 0
    max_idx = 0
    dx, dy = end[0] - start[0], end[1] - start[1]
    line_len = math.sqrt(dx*dx + dy*dy)

    for i in range(1, len(pts) - 1):
        if line_len < 1e-10:
            dist = math.sqrt((pts[i][0]-start[0])**2 + (pts[i][1]-start[1])**2)
        else:
            dist = abs(dy*pts[i][0] - dx*pts[i][1] + end[0]*start[1] - end[1]*start[0]) / line_len
        if dist > max_dist:
            max_dist = dist
            max_idx = i

    if max_dist > tolerance:
        left = simplify_points(pts[:max_idx+1], tolerance)
        right = simplify_points(pts[max_idx:], tolerance)
        return left[:-1] + right
    else:
        return [start, end]


def fit_to_bed(all_paths, bed_w, bed_h, margin):
    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')
    for pts in all_paths:
        for x, y in pts:
            min_x, min_y = min(min_x, x), min(min_y, y)
            max_x, max_y = max(max_x, x), max(max_y, y)
    svg_w, svg_h = max_x - min_x, max_y - min_y
    if svg_w == 0 or svg_h == 0:
        return all_paths, 1.0
    usable_w = bed_w - 2 * margin
    usable_h = bed_h - 2 * margin
    scale = min(usable_w / svg_w, usable_h / svg_h)
    off_x = margin + (usable_w - svg_w * scale) / 2
    off_y = margin + (usable_h - svg_h * scale) / 2
    return [[((x - min_x) * scale + off_x, (y - min_y) * scale + off_y)
             for x, y in pts] for pts in all_paths], scale


def generate_gcode(all_points):
    lines = ["G21", "G90", f"G1 Z{Z_UP} F{FEED_RATE}"]
    for pts in all_points:
        if len(pts) < 2:
            continue
        x0, y0 = pts[0]
        lines.append(f"G1 Z{Z_UP} F{FEED_RATE}")
        lines.append(f"G1 X{x0:.3f} Y{y0:.3f} F{FEED_RATE}")
        lines.append(f"G1 Z{Z_DOWN} F{FEED_RATE}")
        for x, y in pts[1:]:
            lines.append(f"G1 X{x:.3f} Y{y:.3f} F{FEED_RATE}")
        lines.append(f"G1 Z{Z_UP} F{FEED_RATE}")
    lines.append(f"G1 X0 Y0 F{FEED_RATE}")
    return lines


def estimate_time(gcode_lines):
    """估算绘制时间（基于距离和速度）"""
    x, y, z = 0.0, 0.0, 0.0
    total_draw = 0.0   # 落笔距离 mm
    total_travel = 0.0  # 抬笔距离 mm
    total_z = 0.0       # Z 轴距离 mm
    pen_down = False
    move_count = 0

    for line in gcode_lines:
        parts = line.strip().split()
        if not parts or parts[0] not in ("G0", "G1"):
            continue
        params = {}
        for p in parts[1:]:
            if len(p) > 1 and p[0] in "XYZF":
                try:
                    params[p[0]] = float(p[1:])
                except ValueError:
                    pass

        new_x = params.get("X", x)
        new_y = params.get("Y", y)
        new_z = params.get("Z", z)

        if "Z" in params:
            total_z += abs(new_z - z)
            pen_down = abs(new_z - Z_DOWN) < 0.01
            z = new_z

        dx, dy = new_x - x, new_y - y
        dist = math.sqrt(dx*dx + dy*dy)
        if dist > 0:
            if pen_down:
                total_draw += dist
            else:
                total_travel += dist
            move_count += 1
        x, y = new_x, new_y

    # 时间 = 距离 / 速度，速度单位 mm/min
    draw_time = total_draw / FEED_RATE  # min
    travel_time = total_travel / FEED_RATE
    z_time = total_z / FEED_RATE
    total_time = draw_time + travel_time + z_time

    # 加上加减速开销估算（每次移动约 0.02s 开销）
    accel_overhead = move_count * 0.02 / 60  # min

    total_min = total_time + accel_overhead

    print(f"  === 时间估算 ===")
    print(f"  落笔距离: {total_draw:.1f} mm")
    print(f"  空行程:   {total_travel:.1f} mm")
    print(f"  Z轴距离:  {total_z:.1f} mm")
    print(f"  移动次数: {move_count}")
    print(f"  预计时间: {int(total_min)}分{int((total_min%1)*60)}秒")


def send_to_grbl(gcode_lines, port, baud):
    import serial, time
    ser = serial.Serial(port, baud, timeout=2)
    time.sleep(2)
    ser.flushInput()
    def send(cmd):
        ser.write((cmd + "\n").encode())
        resp = ser.readline().decode(errors="ignore").strip()
        print(f"  >> {cmd}  <- {resp}")
    send("$X")
    total = len(gcode_lines)
    for i, line in enumerate(gcode_lines):
        send(line)
        if i % 100 == 0:
            print(f"  进度: {i}/{total}")
    print(f"  完成: {total}/{total}")
    ser.close()


def main():
    parser = argparse.ArgumentParser(description="SVG 转 GCODE（矢量描边）")
    parser.add_argument("svg", help="SVG 文件路径")
    parser.add_argument("-p", "--port", help="串口设备")
    parser.add_argument("-b", "--baud", type=int, default=115200)
    parser.add_argument("-o", "--output", help="输出 GCODE 文件")
    parser.add_argument("--bed-w", type=float, default=100)
    parser.add_argument("--bed-h", type=float, default=100)
    parser.add_argument("--margin", type=float, default=5)
    parser.add_argument("--interval", type=float, default=SAMPLE_INTERVAL, help="曲线采样间距 mm")
    parser.add_argument("--simplify", type=float, default=0, help="线简化容差 mm (0=不简化，推荐 0.1~0.5)")
    parser.add_argument("--preview", action="store_true", help="预览")
    args = parser.parse_args()

    print(f"解析 SVG: {args.svg}")
    paths, _, _ = svg2paths2(args.svg)
    print(f"  找到 {len(paths)} 条原始路径")

    all_subpaths = []
    for p in paths:
        all_subpaths.extend(split_path_at_moves(p))
    print(f"  拆分后 {len(all_subpaths)} 条子路径")

    all_points = [path_to_points(p, args.interval) for p in all_subpaths]
    all_points = [pts for pts in all_points if pts]
    total_before = sum(len(p) for p in all_points)
    print(f"  采样后 {len(all_points)} 条线段, {total_before} 个点")

    # 线简化
    if args.simplify > 0:
        all_points = [simplify_points(pts, args.simplify) for pts in all_points]
        all_points = [pts for pts in all_points if len(pts) >= 2]
        total_after = sum(len(p) for p in all_points)
        ratio = (1 - total_after / total_before) * 100 if total_before > 0 else 0
        print(f"  简化后 {len(all_points)} 条线段, {total_after} 个点 (减少 {ratio:.1f}%)")

    all_points, scale = fit_to_bed(all_points, args.bed_w, args.bed_h, args.margin)
    print(f"  缩放比例: {scale:.4f}")

    gcode = generate_gcode(all_points)
    print(f"  生成 {len(gcode)} 行 GCODE")

    estimate_time(gcode)

    if args.output:
        with open(args.output, "w") as f:
            f.write("\n".join(gcode))
        print(f"  已保存: {args.output}")

    if args.preview:
        from gcode_preview import preview_gcode
        preview_gcode(gcode, title=args.svg, bed_w=args.bed_w, bed_h=args.bed_h, z_down=Z_DOWN)

    if args.port:
        send_to_grbl(gcode, args.port, args.baud)
    elif not args.output and not args.preview:
        print("\n".join(gcode))


if __name__ == "__main__":
    main()
