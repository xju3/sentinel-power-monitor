#!/usr/bin/env python3
"""
KV-AMP700mT 实时功耗监控服务
用法: python3 backend/server.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import struct
import time
import websockets
import sys
import os
from collections import deque

# ── 配置 ──────────────────────────────────────────
# !! 检查并修改以下配置以匹配你的设备 !!
SERIAL_PORT   = None      # 串口号。设为 None 可自动检测 (推荐)
                          # Windows 示例: 'COM3'
                          # macOS / Linux 示例: '/dev/tty.usbmodem12345'
BAUD_RATE     = 9600
MODBUS_SLAVE_ID = 1       # Modbus 从机地址，请参考设备手册
INTERVAL_MS   = 200       # 采样间隔 ms (建议放宽到500ms，避免RS485拥塞)
VOLTAGE       = 3.3       # 系统供电电压 V
CURRENT_OFFSET = 0.0      # 电流校准基准(mA)。用于减去开发板(LED、串口芯片)的固定静态功耗或传感器零偏
WS_HOST       = '0.0.0.0'
WS_PORT       = int(os.getenv('POWER_WS_PORT', '8765'))
DEBUG_MODE    = os.getenv('DEBUG') == '1'  # 调试模式：模拟数据
EFFICIENCY    = 0.85
BATTERY_MAH   = 19000
CURRENT_CYCLE_SECONDS = 60
TARGET_CYCLE_SECONDS = 3600
ACTIVE_CURRENT_THRESHOLD = 1.0
DEVICE_ID     = os.getenv('POWER_DEVICE_ID', '')
ARCHIVE_DIR   = os.getenv('POWER_ARCHIVE_DIR', 'data')
ARCHIVE_ENABLED = os.getenv('POWER_ARCHIVE', '1') != '0'
ARCHIVE_QUEUE_SIZE = 5000
ARCHIVE_BATCH_SIZE = 100
ARCHIVE_FLUSH_SECONDS = 2.0
ARCHIVE_LIFE_SECONDS = 5.0
# ──────────────────────────────────────────────────

def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack('<H', crc)

def read_current_ma(ser) -> float | None:
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
monitoring_active = False

class PowerStats:
    def __init__(self):
        self.device_id = DEVICE_ID
        self.battery_mah = BATTERY_MAH
        self.current_cycle_seconds = CURRENT_CYCLE_SECONDS
        self.target_cycle_seconds = TARGET_CYCLE_SECONDS
        self.reset_all()

    def reset_all(self):
        self.sample_count = 0
        self.sum_abs_current = 0.0
        self.peak_current = 0.0
        self.valley_current = None
        self.total_energy_mwh = 0.0
        self.moving_window = deque(maxlen=50)
        self.reset_cycle_stats()

    def reset_cycle_stats(self):
        self.cycle_samples = []
        self.cycle_number = 0
        self.completed_cycle_count = 0
        self.sum_cycle_current = 0.0
        self.sum_cycle_energy = 0.0
        self.active_period_start_ts = None
        self.active_period_count = 0
        self.sum_active_period_time = 0.0

    def reset_peak(self):
        self.peak_current = 0.0

    def update_settings(
        self,
        device_id: str | None = None,
        battery_mah: float | None = None,
        current_cycle_seconds: float | None = None,
        target_cycle_seconds: float | None = None,
    ):
        if device_id is not None:
            self.device_id = device_id.strip()
        if battery_mah is not None and battery_mah > 0:
            self.battery_mah = battery_mah
        if target_cycle_seconds is not None and target_cycle_seconds > 0:
            self.target_cycle_seconds = target_cycle_seconds
        if (
            current_cycle_seconds is not None
            and current_cycle_seconds > 0
            and current_cycle_seconds != self.current_cycle_seconds
        ):
            self.current_cycle_seconds = current_cycle_seconds
            self.reset_cycle_stats()

    def build_sample(self, current_ma: float, ts_ms: int | None = None) -> dict:
        ts_ms = ts_ms if ts_ms is not None else round(time.time() * 1000)
        
        # 扣除静态偏移底噪，避免出现负值
        adjusted_current = max(0.0, current_ma - CURRENT_OFFSET)
        power_mw = adjusted_current * VOLTAGE
        sample = {
            "ts": ts_ms,
            "device_id": self.device_id,
            "current": round(adjusted_current, 3),
            "power": round(power_mw, 3),
            "voltage": VOLTAGE,
        }
        sample["stats"] = self.update(sample)
        return sample

    def update(self, sample: dict) -> dict:
        abs_current = abs(sample["current"])
        power_mw = abs(sample["power"])

        self.sample_count += 1
        self.sum_abs_current += abs_current
        self.peak_current = max(self.peak_current, abs_current)
        self.valley_current = (
            abs_current
            if self.valley_current is None
            else min(self.valley_current, abs_current)
        )
        self.total_energy_mwh += power_mw * (INTERVAL_MS / 3600000)
        self.moving_window.append(sample["current"])

        self._update_active_period(sample)
        self._update_cycle(sample)

        return self.snapshot()

    def snapshot(self) -> dict:
        avg_current = self.sum_abs_current / self.sample_count if self.sample_count else None
        avg_power = avg_current * VOLTAGE if avg_current is not None else None
        avg_cycle_current = (
            self.sum_cycle_current / self.completed_cycle_count
            if self.completed_cycle_count
            else None
        )
        avg_cycle_energy = (
            self.sum_cycle_energy / self.completed_cycle_count
            if self.completed_cycle_count
            else None
        )
        avg_active_time = (
            self.sum_active_period_time / self.active_period_count
            if self.active_period_count
            else None
        )
        support_count = None
        total_hours = None
        if avg_cycle_energy and avg_cycle_energy > 0:
            battery_energy = self.battery_mah * VOLTAGE * EFFICIENCY
            support_count = int(battery_energy // avg_cycle_energy)
            total_hours = support_count * self.target_cycle_seconds / 3600

        cycle_point_count = self.cycle_point_count
        return {
            "sample_count": self.sample_count,
            "sample_duration_s": round(self.sample_count * INTERVAL_MS / 1000),
            "battery_mah": self.battery_mah,
            "current_cycle_seconds": self.current_cycle_seconds,
            "avg_current_ma": avg_current,
            "avg_power_mw": avg_power,
            "peak_current_ma": self.peak_current if self.peak_current > 0 else None,
            "valley_current_ma": self.valley_current,
            "total_energy_mwh": self.total_energy_mwh,
            "moving_avg_current_ma": (
                sum(self.moving_window) / len(self.moving_window)
                if self.moving_window
                else None
            ),
            "chart_y_max": max(10, int((self.peak_current + 9.999) // 10) * 10),
            "cycle_point_count": cycle_point_count,
            "cycle_number": self.cycle_number,
            "cycle_progress_points": len(self.cycle_samples),
            "absolute_sample_count": self.cycle_number * cycle_point_count + len(self.cycle_samples),
            "completed_cycle_count": self.completed_cycle_count,
            "avg_cycle_current_ma": avg_cycle_current,
            "avg_cycle_energy_mwh": avg_cycle_energy,
            "active_period_count": self.active_period_count,
            "avg_active_period_s": avg_active_time,
            "active_threshold_ma": ACTIVE_CURRENT_THRESHOLD,
            "support_count": support_count,
            "estimated_life_hours": total_hours,
            "target_cycle_seconds": self.target_cycle_seconds,
        }

    @property
    def cycle_point_count(self) -> int:
        return max(1, round(self.current_cycle_seconds * 1000 / INTERVAL_MS))

    def _update_active_period(self, sample: dict):
        is_active = abs(sample["current"]) > ACTIVE_CURRENT_THRESHOLD
        sample_ts = sample["ts"]

        if is_active and self.active_period_start_ts is None:
            self.active_period_start_ts = sample_ts

        if not is_active and self.active_period_start_ts is not None:
            active_time = max(0, (sample_ts - self.active_period_start_ts) / 1000)
            self.active_period_count += 1
            self.sum_active_period_time += active_time
            self.active_period_start_ts = None

    def _update_cycle(self, sample: dict):
        self.cycle_samples.append(sample)
        if len(self.cycle_samples) < self.cycle_point_count:
            return

        completed_cycle = self.cycle_samples[:self.cycle_point_count]
        self.cycle_samples = self.cycle_samples[self.cycle_point_count:]
        cycle_avg_current = sum(abs(item["current"]) for item in completed_cycle) / len(completed_cycle)
        cycle_energy = sum(
            abs(item["power"]) * (INTERVAL_MS / 3600000)
            for item in completed_cycle
        )

        self.cycle_number += 1
        self.completed_cycle_count += 1
        self.sum_cycle_current += cycle_avg_current
        self.sum_cycle_energy += cycle_energy

STATS = PowerStats()

def start_monitoring(
    device_id: str,
    battery_mah: float | None = None,
    current_cycle_seconds: float | None = None,
    target_cycle_seconds: float | None = None,
):
    global monitoring_active
    STATS.update_settings(
        device_id=device_id,
        battery_mah=battery_mah,
        current_cycle_seconds=current_cycle_seconds,
        target_cycle_seconds=target_cycle_seconds,
    )
    STATS.reset_all()
    monitoring_active = True

def stop_monitoring():
    global monitoring_active
    monitoring_active = False

class PowerArchive:
    def __init__(self, archive_dir: str):
        self.archive_dir = archive_dir
        self.conn: sqlite3.Connection | None = None
        self.db_path: str | None = None
        self.last_life_snapshot: dict[str, tuple[int, int]] = {}

    def close(self):
        if self.conn:
            self.conn.commit()
            self.conn.close()
            self.conn = None
            self.db_path = None

    def write_samples(self, samples: list[dict]):
        if not samples:
            return

        batches: dict[str, list[dict]] = {}
        for sample in samples:
            batches.setdefault(self._db_path_for_ts(sample["ts"]), []).append(sample)

        for db_path, db_samples in batches.items():
            self._connect(db_path)
            assert self.conn is not None
            self.conn.executemany(
                """
                INSERT INTO samples (device_id, ts_ms, current_ma, power_mw, voltage_v)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        sample.get("device_id", ""),
                        sample["ts"],
                        sample["current"],
                        sample["power"],
                        sample["voltage"],
                    )
                    for sample in db_samples
                ],
            )
            life_snapshots = [
                sample
                for sample in db_samples
                if self._should_write_life_snapshot(sample)
            ]
            self._write_life_snapshots(life_snapshots)
            self.conn.commit()

    def _should_write_life_snapshot(self, sample: dict) -> bool:
        stats = sample.get("stats")
        if not stats:
            return False

        device_id = sample.get("device_id", "")
        ts_ms = sample["ts"]
        completed_cycle_count = stats.get("completed_cycle_count") or 0
        last_ts, last_cycle_count = self.last_life_snapshot.get(device_id, (0, -1))
        elapsed_ms = ts_ms - last_ts
        if elapsed_ms >= ARCHIVE_LIFE_SECONDS * 1000 or completed_cycle_count != last_cycle_count:
            self.last_life_snapshot[device_id] = (ts_ms, completed_cycle_count)
            return True
        return False

    def _write_life_snapshots(self, samples: list[dict]):
        if not samples:
            return

        assert self.conn is not None
        self.conn.executemany(
            """
            INSERT INTO device_life_snapshots (
                device_id, ts_ms, battery_mah, current_cycle_s, target_cycle_s,
                sample_count, sample_duration_s, avg_current_ma, avg_power_mw,
                total_energy_mwh, completed_cycle_count, avg_cycle_current_ma,
                avg_cycle_energy_mwh, active_period_count, avg_active_period_s,
                support_count, estimated_life_hours
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                _life_snapshot_row(sample)
                for sample in samples
            ],
        )

    def _db_path_for_ts(self, ts_ms: int) -> str:
        local_date = dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        return os.path.join(self.archive_dir, f"power_{local_date}.db")

    def _connect(self, db_path: str):
        if self.conn and self.db_path == db_path:
            return

        self.close()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.db_path = db_path
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL DEFAULT '',
                ts_ms INTEGER NOT NULL,
                current_ma REAL NOT NULL,
                power_mw REAL NOT NULL,
                voltage_v REAL NOT NULL
            )
            """
        )
        self._ensure_column("samples", "device_id", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_device_ts ON samples(device_id, ts_ms)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts_ms ON samples(ts_ms)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_life_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL DEFAULT '',
                ts_ms INTEGER NOT NULL,
                battery_mah REAL NOT NULL,
                current_cycle_s REAL NOT NULL,
                target_cycle_s REAL NOT NULL,
                sample_count INTEGER NOT NULL,
                sample_duration_s REAL NOT NULL,
                avg_current_ma REAL,
                avg_power_mw REAL,
                total_energy_mwh REAL NOT NULL,
                completed_cycle_count INTEGER NOT NULL,
                avg_cycle_current_ma REAL,
                avg_cycle_energy_mwh REAL,
                active_period_count INTEGER NOT NULL,
                avg_active_period_s REAL,
                support_count INTEGER,
                estimated_life_hours REAL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_life_device_ts ON device_life_snapshots(device_id, ts_ms)"
        )
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str):
        assert self.conn is not None
        columns = {
            row[1]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def _life_snapshot_row(sample: dict) -> tuple:
    stats = sample["stats"]
    return (
        sample.get("device_id", ""),
        sample["ts"],
        stats.get("battery_mah") or BATTERY_MAH,
        stats.get("current_cycle_seconds") or CURRENT_CYCLE_SECONDS,
        stats.get("target_cycle_seconds") or TARGET_CYCLE_SECONDS,
        stats.get("sample_count") or 0,
        stats.get("sample_duration_s") or 0,
        stats.get("avg_current_ma"),
        stats.get("avg_power_mw"),
        stats.get("total_energy_mwh") or 0,
        stats.get("completed_cycle_count") or 0,
        stats.get("avg_cycle_current_ma"),
        stats.get("avg_cycle_energy_mwh"),
        stats.get("active_period_count") or 0,
        stats.get("avg_active_period_s"),
        stats.get("support_count"),
        stats.get("estimated_life_hours"),
    )

def query_latest_life_snapshots(archive_dir: str, device_id: str = "") -> list[dict]:
    latest_by_device: dict[str, dict] = {}
    if not os.path.isdir(archive_dir):
        return []

    for name in sorted(os.listdir(archive_dir)):
        if not name.endswith(".db"):
            continue
        db_path = os.path.join(archive_dir, name)
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            if not _sqlite_table_exists(conn, "device_life_snapshots"):
                conn.close()
                continue
            if device_id:
                rows = conn.execute(
                    """
                    SELECT * FROM device_life_snapshots
                    WHERE device_id = ?
                    ORDER BY ts_ms DESC
                    LIMIT 1
                    """,
                    (device_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT d.*
                    FROM device_life_snapshots d
                    JOIN (
                        SELECT device_id, MAX(ts_ms) AS max_ts
                        FROM device_life_snapshots
                        GROUP BY device_id
                    ) latest
                    ON d.device_id = latest.device_id AND d.ts_ms = latest.max_ts
                    """
                ).fetchall()
            conn.close()
        except sqlite3.Error as e:
            print(f"[查询] 跳过 {db_path}: {e}")
            continue

        for row in rows:
            record = dict(row)
            key = record.get("device_id", "")
            prev = latest_by_device.get(key)
            if prev is None or record["ts_ms"] > prev["ts_ms"]:
                latest_by_device[key] = record

    return sorted(
        latest_by_device.values(),
        key=lambda record: record["ts_ms"],
        reverse=True,
    )

def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None

async def archive_writer(queue: asyncio.Queue):
    archive = PowerArchive(ARCHIVE_DIR)
    batch = []
    print(f"[存档] SQLite 已启用，目录: {os.path.abspath(ARCHIVE_DIR)}")
    try:
        while True:
            try:
                sample = await asyncio.wait_for(
                    queue.get(), timeout=ARCHIVE_FLUSH_SECONDS
                )
                batch.append(sample)
                while len(batch) < ARCHIVE_BATCH_SIZE:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                pass

            if batch:
                try:
                    archive.write_samples(batch)
                except Exception as e:
                    print(f"[存档] 写入失败: {e}")
                finally:
                    batch.clear()
    except asyncio.CancelledError:
        if batch:
            try:
                archive.write_samples(batch)
            except Exception as e:
                print(f"[存档] 停止前写入失败: {e}")
        archive.close()
        raise

def enqueue_archive_sample(queue: asyncio.Queue | None, sample: dict):
    if queue is None:
        return

    try:
        queue.put_nowait(sample)
    except asyncio.QueueFull:
        print("[存档] 队列已满，当前样本未写入")

async def broadcast(msg: str):
    if connected_clients:
        await asyncio.gather(*[c.send(msg) for c in connected_clients],
                             return_exceptions=True)

async def handle_client_message(ws, msg: str):
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        print(f"[WS] 忽略无效消息: {msg}")
        return

    msg_type = data.get("type")
    if msg_type == "settings":
        STATS.update_settings(
            device_id=_to_device_id(data.get("device_id")),
            battery_mah=_to_float(data.get("battery_mAh")),
            current_cycle_seconds=_to_float(data.get("current_cycle_s")),
            target_cycle_seconds=_to_float(data.get("target_cycle_s")),
        )
    elif msg_type == "start_monitor":
        device_id = _to_device_id(data.get("device_id"))
        if not device_id:
            await ws.send(json.dumps({
                "type": "control_error",
                "message": "设备编号不能为空",
            }))
            return
        start_monitoring(
            device_id=device_id,
            battery_mah=_to_float(data.get("battery_mAh")),
            current_cycle_seconds=_to_float(data.get("current_cycle_s")),
            target_cycle_seconds=_to_float(data.get("target_cycle_s")),
        )
        await ws.send(json.dumps({
            "type": "monitor_started",
            "device_id": STATS.device_id,
        }))
    elif msg_type == "stop_monitor":
        stop_monitoring()
        await ws.send(json.dumps({
            "type": "monitor_stopped",
        }))
    elif msg_type == "reset_all":
        STATS.reset_all()
    elif msg_type == "reset_peak":
        STATS.reset_peak()
    elif msg_type == "query_life":
        device_id = _to_device_id(data.get("device_id"))
        if not device_id:
            await ws.send(json.dumps({
                "type": "control_error",
                "message": "查询设备编号不能为空",
            }))
            return
        records = query_latest_life_snapshots(
            ARCHIVE_DIR,
            device_id,
        )
        await ws.send(json.dumps({
            "type": "life_query_result",
            "records": records,
        }))

def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _to_device_id(value):
    return value.strip() if isinstance(value, str) else ""

async def ws_handler(ws):
    connected_clients.add(ws)
    print(f"[WS] 客户端连接: {ws.remote_address}")
    try:
        async for msg in ws:
            await handle_client_message(ws, msg)
    finally:
        connected_clients.discard(ws)
        print(f"[WS] 客户端断开: {ws.remote_address}")

async def serial_reader(archive_queue: asyncio.Queue | None):
    if DEBUG_MODE:
        print("[DEBUG] 调试模式启用 - 使用模拟数据")
        await debug_reader(archive_queue)
        return

    import serial
    import serial.tools.list_ports
    ser = None
    while True:
        if not monitoring_active or not STATS.device_id:
            if ser:
                ser.close()
                ser = None
                print("[串口] 监测已停止，串口已关闭")
            await asyncio.sleep(0.2)
            continue

        if ser is None:
            ser = await open_serial_port(serial)
            if ser is None:
                await asyncio.sleep(1)
                continue

        t_start = time.monotonic()
        try:
            current_ma = read_current_ma(ser)

            if current_ma is not None:
                sample = STATS.build_sample(current_ma)
                payload = json.dumps(sample)
                enqueue_archive_sample(archive_queue, sample)
                await broadcast(payload)
                read_errors = 0
            else:
                await asyncio.sleep(0.5) 
        except Exception as e:
            print(f"[错误] {e}")
            await asyncio.sleep(1)
            continue

        elapsed = time.monotonic() - t_start
        await asyncio.sleep(max(0, INTERVAL_MS / 1000 - elapsed))

async def open_serial_port(serial):
    port_to_use = SERIAL_PORT
    if not port_to_use:
        print("[串口] 正在自动检测串口...")
        ports = serial.tools.list_ports.comports()
        candidates = [p for p in ports if 'usb' in p.device.lower() or 'acm' in p.device.lower()]

        if not candidates:
            print(f"[错误] 未找到可用的 USB 串口。检测到所有串口: {[p.device for p in ports]}")
            print(f"[提示] 请在脚本顶部手动配置 'SERIAL_PORT'。")
            return None

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
        print(f"[提示] 使用调试模式: DEBUG=1 python3 backend/server.py")
        return None

    print("[串口] 等待设备启动 (2秒)...")
    await asyncio.sleep(2)
    ser.reset_input_buffer()
    print(f"[串口] 已连接，开始采样（设备: {STATS.device_id}, {INTERVAL_MS}ms/次）")
    return ser

async def debug_reader(archive_queue: asyncio.Queue | None):
    """调试模式 - 生成模拟数据"""
    print("[DEBUG] 生成模拟功耗数据，建议用于测试 WebSocket 连接")
    interval = INTERVAL_MS / 1000
    import random
    phase = 0
    
    while True:
        if not monitoring_active or not STATS.device_id:
            await asyncio.sleep(0.2)
            continue

        t_start = time.monotonic()
        # 生成模拟数据：基础 50mA + 正弦波纹波
        phase = (phase + 0.1) % (2 * 3.14159)
        base_current = 50
        ripple = 20 * (1 + __import__('math').sin(phase))
        current_ma = base_current + ripple + random.gauss(0, 2)
        
        sample = STATS.build_sample(current_ma)
        payload = json.dumps(sample)
        enqueue_archive_sample(archive_queue, sample)
        await broadcast(payload)
        
        elapsed = time.monotonic() - t_start
        await asyncio.sleep(max(0, interval - elapsed))

async def main():
    print(f"\n{'='*60}")
    print(f"[服务] WebSocket 启动于 ws://localhost:{WS_PORT}")
    print(f"[服务] Web 页面: cd web && npm run dev")
    print(f"{'='*60}\n")
    
    archive_queue = None
    archive_task = None
    if ARCHIVE_ENABLED:
        archive_queue = asyncio.Queue(maxsize=ARCHIVE_QUEUE_SIZE)
        archive_task = asyncio.create_task(archive_writer(archive_queue))
    else:
        print("[存档] 已禁用（POWER_ARCHIVE=0）")

    try:
        async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
            await serial_reader(archive_queue)
    except KeyboardInterrupt:
        print("\n[停止] 服务已关闭")
    except Exception as e:
        print(f"\n[错误] {e}")
        sys.exit(1)
    finally:
        if archive_task:
            archive_task.cancel()
            try:
                await archive_task
            except asyncio.CancelledError:
                pass

def run():
    if DEBUG_MODE:
        print("\n[启动] 调试模式")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[停止] 服务已关闭")


if __name__ == '__main__':
    run()
