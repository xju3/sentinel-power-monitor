#!/usr/bin/env python3
"""
KV-AMP700mT 实时功耗监控服务
用法: python3 power_monitor_server.py
"""

import asyncio
import json
import struct
import time
import serial
import serial.tools.list_ports
import websockets
import sys
import os

# ── 配置 ──────────────────────────────────────────
# !! 检查并修改以下配置以匹配你的设备 !!
SERIAL_PORT   = None      # 串口号。设为 None 可自动检测 (推荐)
                          # Windows 示例: 'COM3'
                          # macOS / Linux 示例: '/dev/tty.usbmodem12345'
BAUD_RATE     = 9600
MODBUS_SLAVE_ID = 1       # Modbus 从机地址，请参考设备手册
INTERVAL_MS   = 200       # 采样间隔 ms (建议放宽到500ms，避免RS485拥塞)
VOLTAGE       = 3.3       # 系统供电电压 V
WS_HOST       = '0.0.0.0'
WS_PORT       = 8765
DEBUG_MODE    = os.getenv('DEBUG') == '1'  # 调试模式：模拟数据
# ──────────────────────────────────────────────────

def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack('<H', crc)

def read_current_ma(ser: serial.Serial) -> float | None:
    # Modbus RTU 查询帧: [从机地址, 功能码, 寄存器起始地址 (2B), 读取寄存器数量 (2B), CRC校验 (2B)]
    # 读取 2 个保持寄存器 (从地址 0x0000 开始)
    pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02]) # PDU: 功能码(0x03) + 数据
    adu = bytes([MODBUS_SLAVE_ID]) + pdu         # ADU: 从机地址 + PDU
    cmd = adu + crc16(adu)
    ser.reset_input_buffer()
    ser.reset_output_buffer() # 确保发送缓冲区没有残留脏数据
    ser.write(cmd)
    ser.flush()
    resp = ser.read(9)
    if len(resp) != 9:
        if len(resp) > 0:
            print(f"[警告] 数据长度异常, 收到 {len(resp)} 字节: {resp.hex()}")
        else:
            print("[警告] 未收到任何数据")
        return None
    raw = struct.unpack('>i', resp[3:7])[0]
    return raw * 10 / 1000   # 分辨率 10uA → mA

connected_clients: set = set()

async def broadcast(msg: str):
    if connected_clients:
        await asyncio.gather(*[c.send(msg) for c in connected_clients],
                             return_exceptions=True)

async def ws_handler(ws):
    connected_clients.add(ws)
    print(f"[WS] 客户端连接: {ws.remote_address}")
    try:
        await ws.wait_closed()
    finally:
        connected_clients.discard(ws)
        print(f"[WS] 客户端断开: {ws.remote_address}")

async def serial_reader():
    if DEBUG_MODE:
        print("[DEBUG] 调试模式启用 - 使用模拟数据")
        await debug_reader()
        return
    
    port_to_use = SERIAL_PORT
    if not port_to_use:
        print("[串口] 正在自动检测串口...")
        ports = serial.tools.list_ports.comports()
        # 优先选择包含 'usb' 或 'acm' 的设备，这是常见 USB 串口的标识
        candidates = [p for p in ports if 'usb' in p.device.lower() or 'acm' in p.device.lower()]
        
        if not candidates:
            print(f"[错误] 未找到可用的 USB 串口。检测到所有串口: {[p.device for p in ports]}")
            print(f"[提示] 请在脚本顶部手动配置 'SERIAL_PORT'。")
            return
        
        if len(candidates) > 1:
            print(f"[警告] 检测到多个可能的串口: {[p.device for p in candidates]}")
        
        port_to_use = candidates[0].device
        print(f"[串口] 自动选择端口: {port_to_use}")

    print(f"[串口] 尝试打开 {port_to_use} @ {BAUD_RATE}baud (从机地址: {MODBUS_SLAVE_ID})")
    try:
        ser = serial.Serial(
            port=port_to_use,
            baudrate=BAUD_RATE,
            bytesize=8, parity='N', stopbits=1,
            timeout=0.5
        )
    except serial.SerialException as e:
        print(f"[错误] 串口打开失败: {e}")
        print(f"[提示] 确认设备已连接，且端口 '{port_to_use}' 未被其他程序占用。")
        print(f"[提示] 使用调试模式: DEBUG=1 python3 power_monitor.py")
        return

    print("[串口] 等待设备启动 (2秒)...")
    await asyncio.sleep(2)
    ser.reset_input_buffer()

    print(f"[串口] 已连接，开始采样（{INTERVAL_MS}ms/次）")
    interval = INTERVAL_MS / 1000
    read_errors = 0

    while True:
        t_start = time.monotonic()
        try:
            current_ma = read_current_ma(ser)

            if current_ma is not None:
                power_mw = abs(current_ma) * VOLTAGE
                payload = json.dumps({
                    "ts":        round(time.time() * 1000),   # ms 时间戳
                    "current":   round(current_ma, 3),         # mA
                    "power":     round(power_mw, 3),           # mW
                    "voltage":   VOLTAGE,
                })
                await broadcast(payload)
                read_errors = 0
            else:
                read_errors += 1
                await asyncio.sleep(0.5) 
        except Exception as e:
            print(f"[错误] {e}")
            await asyncio.sleep(1)
            continue

        elapsed = time.monotonic() - t_start
        await asyncio.sleep(max(0, interval - elapsed))

async def debug_reader():
    """调试模式 - 生成模拟数据"""
    print("[DEBUG] 生成模拟功耗数据，建议用于测试 WebSocket 连接")
    interval = INTERVAL_MS / 1000
    import random
    phase = 0
    
    while True:
        t_start = time.monotonic()
        # 生成模拟数据：基础 50mA + 正弦波纹波
        phase = (phase + 0.1) % (2 * 3.14159)
        base_current = 50
        ripple = 20 * (1 + __import__('math').sin(phase))
        current_ma = base_current + ripple + random.gauss(0, 2)
        
        power_mw = abs(current_ma) * VOLTAGE
        payload = json.dumps({
            "ts":        round(time.time() * 1000),
            "current":   round(current_ma, 3),
            "power":     round(power_mw, 3),
            "voltage":   VOLTAGE,
        })
        await broadcast(payload)
        
        elapsed = time.monotonic() - t_start
        await asyncio.sleep(max(0, interval - elapsed))

async def main():
    print(f"\n{'='*60}")
    print(f"[服务] WebSocket 启动于 ws://localhost:{WS_PORT}")
    print(f"[服务] 用浏览器打开 http://localhost:8000")
    print(f"{'='*60}\n")
    
    try:
        async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
            await serial_reader()
    except KeyboardInterrupt:
        print("\n[停止] 服务已关闭")
    except Exception as e:
        print(f"\n[错误] {e}")
        sys.exit(1)

if __name__ == '__main__':
    if DEBUG_MODE:
        print("\n[启动] 调试模式")
    asyncio.run(main())
