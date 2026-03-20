#!/usr/bin/env python3
"""
鼠标控制写字机 - GRBL ESP32
鼠标移动 -> XY轴移动
鼠标左键按下 -> 落笔 (Z轴下降)
鼠标左键抬起 -> 抬笔 (Z轴上升)
"""

import argparse
import serial
import time
import threading
import queue
import evdev
from evdev import ecodes

# 工作区域映射 (mm)
X_MIN, X_MAX = 0, 100
Y_MIN, Y_MAX = 0, 100

# 屏幕分辨率（用于坐标映射，越大鼠标需要移动越多，精度越高）
SCREEN_W, SCREEN_H = 1920, 1080

# Z轴高度
Z_UP   = 0.0
Z_DOWN = 5.0

# 移动速度 (mm/min)
FEED_RATE = 40000

# GRBL 串口接收缓冲区大小（字节），保守取 100
GRBL_RX_BUFFER = 100
# ==========================

ser = None
cmd_queue = queue.Queue()
ser_lock = threading.Lock()  # 串口访问锁
last_x, last_y = 0.0, 0.0
pen_down = False


def sender_thread():
    """
    GRBL 字符计数流控发送线程。
    跟踪已发送但未收到 ok 的字节数，不超过缓冲区上限就继续发。
    """
    pending = []   # [(cmd_str, byte_len), ...]
    in_flight = 0  # 已发出未确认的字节数

    while True:
        with ser_lock:
            # 尝试读一行回复（非阻塞）
            if ser.in_waiting:
                resp = ser.readline().decode(errors="ignore").strip()
                if resp.lower().startswith("ok") or resp.lower().startswith("error"):
                    if pending:
                        _, n = pending.pop(0)
                        in_flight -= n

            # 从队列取新命令，只要缓冲区还有空间就发
            while not cmd_queue.empty():
                cmd = cmd_queue.get_nowait()
                if cmd is None:
                    return  # 退出信号
                line = cmd.strip() + "\n"
                n = len(line.encode())
                if in_flight + n <= GRBL_RX_BUFFER:
                    ser.write(line.encode())
                    print(f"  >> {cmd}")
                    pending.append((cmd, n))
                    in_flight += n
                else:
                    if not cmd.startswith("G1 Z"):
                        pass
                    else:
                        cmd_queue.put(cmd)
                    break

        time.sleep(0.005)  # 5ms 轮询，避免空转


def find_mouse(path=None):
    """列出所有鼠标设备，path 指定时直接用"""
    candidates = []
    for d in evdev.list_devices():
        dev = evdev.InputDevice(d)
        cap = dev.capabilities()
        if ecodes.EV_REL in cap and ecodes.REL_X in cap[ecodes.EV_REL]:
            candidates.append(dev)

    if not candidates:
        return None

    print("可用鼠标设备:")
    for dev in candidates:
        print(f"  {dev.path} - {dev.name}")

    if path:
        for dev in candidates:
            if dev.path == path:
                print(f"使用: {dev.path} - {dev.name}")
                return dev
        print(f"未找到指定设备 {path}")
        return None

    # 默认取第一个
    print(f"使用: {candidates[0].path} - {candidates[0].name}（可用 --mouse 指定）")
    return candidates[0]


def mouse_thread(dev):
    """读取 evdev 事件，映射坐标并入队"""
    global last_x, last_y, pen_down
    abs_x = abs_y = 0  # 累计相对位移转绝对屏幕坐标

    for event in dev.read_loop():
        if event.type == ecodes.EV_REL:
            if event.code == ecodes.REL_X:
                abs_x = max(0, min(SCREEN_W, abs_x + event.value))
            elif event.code == ecodes.REL_Y:
                abs_y = max(0, min(SCREEN_H, abs_y + event.value))
            on_move(abs_x, abs_y)

        elif event.type == ecodes.EV_KEY:
            if event.code == ecodes.BTN_LEFT and event.value != 2:
                print(f"[按键] BTN_LEFT value={event.value}")
                on_click(event.value)


def on_move(x, y):
    global last_x, last_y

    gx = X_MIN + (x / SCREEN_W) * (X_MAX - X_MIN)
    gy = Y_MIN + (y / SCREEN_H) * (Y_MAX - Y_MIN)
    gx = max(X_MIN, min(X_MAX, gx))
    gy = max(Y_MIN, min(Y_MAX, gy))

    if gx == last_x and gy == last_y:
        return
    last_x, last_y = gx, gy

    # 清掉积压的 XY 移动，只保留 Z 轴指令
    tmp = []
    while not cmd_queue.empty():
        try:
            item = cmd_queue.get_nowait()
            if item and item.startswith("G1 Z"):
                tmp.append(item)
        except queue.Empty:
            break
    for item in tmp:
        cmd_queue.put(item)

    cmd_queue.put(f"G1 X{gx:.2f} Y{gy:.2f} F{FEED_RATE}")
    print(f"[移动] screen=({x},{y}) -> gcode=({gx:.2f},{gy:.2f})")


def on_click(value: int):
    global pen_down
    # value=1: 按下->落笔, value=0: 抬起->抬笔
    if value == 1 and not pen_down:
        pen_down = True
        print("[落笔]")
        with ser_lock:
            send_direct(f"G1 Z{Z_DOWN} F{FEED_RATE}")
            send_direct("G4 P0")
    elif value == 0 and pen_down:
        pen_down = False
        print("[抬笔]")
        with ser_lock:
            send_direct(f"G1 Z{Z_UP} F{FEED_RATE}")
            send_direct("G4 P0")


def send_direct(cmd: str):
    """初始化阶段直接发送并等待回复"""
    line = cmd.strip() + "\n"
    ser.write(line.encode())
    resp = ser.readline().decode(errors="ignore").strip()
    print(f"  >> {cmd}  <- {resp}")
    return resp


def init_grbl():
    print("连接 GRBL...")
    ser.flushInput()

    startup = ""
    deadline = time.time() + 1
    while time.time() < deadline:
        if ser.in_waiting:
            startup += ser.readline().decode(errors="ignore")
    print("GRBL 启动信息:", startup.strip())

    send_direct("\r\n\r\n")
    time.sleep(0.5)
    ser.flushInput()

    send_direct("$X")
    send_direct("G21")
    send_direct("G90")
    send_direct(f"G1 Z{Z_UP} F{FEED_RATE}")
    send_direct(f"G1 X0 Y0 F{FEED_RATE}")
    print("初始化完成，开始监听鼠标...")


def main():
    global ser
    parser = argparse.ArgumentParser(description="鼠标控制 GRBL 写字机")
    parser.add_argument("-p", "--port", required=True, help="串口设备，如 /dev/ttyUSB2")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="波特率 (默认: 115200)")
    parser.add_argument("-m", "--mouse", default=None, help="鼠标设备路径，如 /dev/input/event8（不指定则用第一个）")
    args = parser.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.1)
    init_grbl()

    t = threading.Thread(target=sender_thread, daemon=True)
    t.start()

    dev = find_mouse(args.mouse)
    if dev is None:
        print("未找到鼠标设备，请确认已加入 input 组：sudo usermod -aG input $USER")
        return

    print("移动鼠标控制XY，左键落笔/抬笔，Ctrl+C 退出")
    mt = threading.Thread(target=mouse_thread, args=(dev,), daemon=True)
    mt.start()

    try:
        mt.join()
    except KeyboardInterrupt:
        print("\n退出，抬笔归零...")
        cmd_queue.put(f"G1 Z{Z_UP} F{FEED_RATE}")
        cmd_queue.put(f"G1 X0 Y0 F{FEED_RATE}")
        cmd_queue.put(None)
        t.join(timeout=5)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
