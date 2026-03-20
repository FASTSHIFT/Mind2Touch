#!/usr/bin/env python3
"""
PNG/JPG 图片转 GCODE（光栅扫描填充）
像素越暗 -> 落笔绘制，像素越亮 -> 抬笔跳过
支持双向扫描（蛇形走位）减少空行程
"""

import argparse
import math
from PIL import Image

# ========== 配置 ==========
Z_UP = 0.0
Z_DOWN = 5.0
FEED_RATE = 20000

# 亮度阈值：低于此值落笔（0=黑, 255=白）
BRIGHTNESS_THRESHOLD = 200
# ==========================


def image_to_gcode(img_path, bed_w, bed_h, margin, resolution, threshold, invert):
    """
    将图片转为 GCODE 扫描线
    resolution: mm/像素
    """
    img = Image.open(img_path).convert("L")  # 转灰度

    if invert:
        from PIL import ImageOps
        img = ImageOps.invert(img)

    # 计算缩放：图片适配工作区
    usable_w = bed_w - 2 * margin
    usable_h = bed_h - 2 * margin

    # 按 resolution 计算目标像素数
    target_w = int(usable_w / resolution)
    target_h = int(usable_h / resolution)

    # 保持宽高比
    ratio = min(target_w / img.width, target_h / img.height)
    new_w = max(1, int(img.width * ratio))
    new_h = max(1, int(img.height * ratio))

    img = img.resize((new_w, new_h), Image.LANCZOS)
    pixels = img.load()

    # 居中偏移
    off_x = margin + (usable_w - new_w * resolution) / 2
    off_y = margin + (usable_h - new_h * resolution) / 2

    print(f"  图片: {img.width}x{img.height} 像素, 分辨率: {resolution}mm/px")
    print(f"  实际尺寸: {img.width * resolution:.1f} x {img.height * resolution:.1f} mm")

    lines = ["G21", "G90", f"G1 Z{Z_UP} F{FEED_RATE}"]
    pen_is_down = False
    total_draw = 0

    for row in range(new_h):
        y = off_y + row * resolution

        # 蛇形：偶数行从左到右，奇数行从右到左
        if row % 2 == 0:
            cols = range(new_w)
        else:
            cols = range(new_w - 1, -1, -1)

        row_has_dark = False
        for col in cols:
            brightness = pixels[col, row]
            if brightness < threshold:
                row_has_dark = True
                break

        if not row_has_dark:
            continue  # 全白行跳过

        # 扫描这一行
        for col in cols:
            x = off_x + col * resolution
            brightness = pixels[col, row]
            should_draw = brightness < threshold

            if should_draw and not pen_is_down:
                # 抬笔移动到起点，落笔
                lines.append(f"G1 X{x:.3f} Y{y:.3f} F{FEED_RATE}")
                lines.append(f"G1 Z{Z_DOWN} F{FEED_RATE}")
                pen_is_down = True
                total_draw += 1
            elif not should_draw and pen_is_down:
                # 抬笔
                lines.append(f"G1 Z{Z_UP} F{FEED_RATE}")
                pen_is_down = False
            elif should_draw and pen_is_down:
                lines.append(f"G1 X{x:.3f} Y{y:.3f} F{FEED_RATE}")

        # 行末抬笔
        if pen_is_down:
            lines.append(f"G1 Z{Z_UP} F{FEED_RATE}")
            pen_is_down = False

    lines.append(f"G1 Z{Z_UP} F{FEED_RATE}")
    lines.append(f"G1 X0 Y0 F{FEED_RATE}")

    print(f"  绘制段数: {total_draw}")
    return lines


def send_to_grbl(gcode_lines, port, baud):
    import serial
    import time

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
    parser = argparse.ArgumentParser(description="PNG/JPG 图片转 GCODE 写字机")
    parser.add_argument("image", help="图片文件路径")
    parser.add_argument("-p", "--port", help="串口设备（不指定则只输出 GCODE）")
    parser.add_argument("-b", "--baud", type=int, default=115200)
    parser.add_argument("-o", "--output", help="输出 GCODE 文件路径")
    parser.add_argument("--bed-w", type=float, default=100, help="工作区宽度 mm (默认 100)")
    parser.add_argument("--bed-h", type=float, default=100, help="工作区高度 mm (默认 100)")
    parser.add_argument("--margin", type=float, default=5, help="边距 mm (默认 5)")
    parser.add_argument("--resolution", type=float, default=0.5, help="分辨率 mm/像素 (默认 0.5，越小越精细)")
    parser.add_argument("--threshold", type=int, default=BRIGHTNESS_THRESHOLD, help="亮度阈值 0-255 (默认 200，低于此值落笔)")
    parser.add_argument("--invert", action="store_true", help="反色（白底黑字变黑底白字）")
    parser.add_argument("--preview", action="store_true", help="预览 GCODE 路径")
    args = parser.parse_args()

    print(f"处理图片: {args.image}")
    gcode = image_to_gcode(
        args.image, args.bed_w, args.bed_h, args.margin,
        args.resolution, args.threshold, args.invert
    )
    print(f"  生成 {len(gcode)} 行 GCODE")

    if args.output:
        with open(args.output, "w") as f:
            f.write("\n".join(gcode))
        print(f"  已保存: {args.output}")

    if args.preview:
        from gcode_preview import preview_gcode
        preview_gcode(gcode, title=args.image, bed_w=args.bed_w, bed_h=args.bed_h, z_down=Z_DOWN)

    if args.port:
        print(f"发送到 {args.port}...")
        send_to_grbl(gcode, args.port, args.baud)
    elif not args.output:
        print("\n".join(gcode))


if __name__ == "__main__":
    main()
