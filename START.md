# ESP32-S3 功耗监控 - 启动指南

## 方案 A：有硬件（推荐）

### 1. 连接硬件
- 使用 USB 连接 KV-AMP700mT 功耗计到电脑
- 确认设备在 `/dev/cu.usbmodem*` 或 `COM*`

### 2. 启动 Python 服务器
```bash
python3 power_monitor.py
```

预期输出：
```
============================================================
[服务] WebSocket 启动于 ws://localhost:8765
[服务] 用浏览器打开 http://localhost:8000
============================================================

[串口] 打开 /dev/cu.usbmodem5B7A0302631 @ 9600baud
[串口] 已连接，开始采样（200ms/次）
```

### 3. 打开浏览器
- 访问：`http://localhost:8000/index.html`（需搭建 HTTP 服务器）
- 或直接打开文件：`file:///path/to/index.html`

---

## 方案 B：调试模式（无硬件）

测试 WebSocket 连接而无需实际硬件：

```bash
DEBUG=1 python3 power_monitor.py
```

预期输出：
```
[启动] 调试模式
============================================================
[服务] WebSocket 启动于 ws://localhost:8765
[服务] 用浏览器打开 http://localhost:8000
============================================================

[DEBUG] 调试模式启用 - 使用模拟数据
[DEBUG] 生成模拟功耗数据，建议用于测试 WebSocket 连接
```

然后打开浏览器访问 `index.html`，应该看到模拟的电流曲线。

---

## 问题排查

### 问题：无法连接 WebSocket
**错误信息**：`❌ 连接失败 · 检查服务器`

**解决步骤**：
1. 确认 Python 服务器是否在运行
   ```bash
   lsof -i :8765  # macOS/Linux
   netstat -ano | findstr :8765  # Windows
   ```

2. 如果端口被占用，可修改 `power_monitor.py` 中的 `WS_PORT`

3. 查看 Python 进程的错误日志

### 问题：串口打开失败
**错误信息**：`[错误] 串口打开失败`

**解决方案**：
- 使用 `DEBUG=1` 模式进行测试
- 检查设备连接和权限
- 确认正确的串口号（修改 `SERIAL_PORT`）

### 问题：收不到数据
1. 查看浏览器控制台（F12 → Console）
2. 确认 WebSocket 状态（应显示"已连接"）
3. 检查 Python 服务器是否在广播数据

---

## 快速启动脚本

### macOS/Linux
```bash
#!/bin/bash
cd "$(dirname "$0")"
python3 power_monitor.py
```

### Windows (PowerShell)
```powershell
cd $PSScriptRoot
python power_monitor.py
```

---

## 技术参数

| 参数 | 值 |
|------|-----|
| WebSocket 地址 | `ws://localhost:8765` |
| 采样间隔 | 200ms |
| 串口波特率 | 9600 |
| 设备电压 | 3.3V |
| 数据格式 | JSON |

## 依赖包

```bash
pip install pyserial websockets
```
