#!/usr/bin/env python3
"""
KV-AMP700mT 实时功耗监控服务
ESP32-S3 @ 3.3V
用法: python3 power_monitor_server.py
"""

import asyncio
import json
import struct
import time
import serial
import websockets

# ── 配置 ──────────────────────────────────────────
SERIAL_PORT   = '/dev/cu.usbmodem5B7A0302631'
BAUD_RATE     = 9600
INTERVAL_MS   = 200       # 采样间隔 ms
VOLTAGE       = 3.3       # ESP32-S3 供电电压 V
WS_HOST       = '0.0.0.0'
WS_PORT       = 8765
# ──────────────────────────────────────────────────

MODBUS_CMD = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x02])

def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack('<H', crc)

def read_current_ma(ser: serial.Serial) -> float | None:
    cmd = MODBUS_CMD + crc16(MODBUS_CMD)
    ser.reset_input_buffer()
    ser.write(cmd)
    resp = ser.read(9)
    if len(resp) != 9:
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
    print(f"[串口] 打开 {SERIAL_PORT} @ {BAUD_RATE}baud")
    try:
        ser = serial.Serial(
            port=SERIAL_PORT,
            baudrate=BAUD_RATE,
            bytesize=8, parity='N', stopbits=1,
            timeout=0.5
        )
    except serial.SerialException as e:
        print(f"[错误] 串口打开失败: {e}")
        return

    print(f"[串口] 已连接，开始采样（{INTERVAL_MS}ms/次）")
    interval = INTERVAL_MS / 1000

    while True:
        t_start = time.monotonic()
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
        else:
            print("[警告] 读取超时或响应异常")

        elapsed = time.monotonic() - t_start
        await asyncio.sleep(max(0, interval - elapsed))

async def main():
    print(f"[服务] WebSocket 启动于 ws://localhost:{WS_PORT}")
    print(f"[服务] 用浏览器打开 power_monitor.html 即可查看曲线")
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        await serial_reader()

if __name__ == '__main__':
    asyncio.run(main())
