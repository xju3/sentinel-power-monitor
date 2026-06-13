# ESP32-S3 功耗监控 - 启动指南

## 目录结构

```text
backend/  后端 WebSocket、采集、计算、SQLite 存档
web/      前端页面，使用 Node 管理静态服务
docs/     设备资料
```

根目录不再放 Python 入口文件；后端和前端分别从各自目录启动。

## 启动后端

有硬件：

```bash
python3 backend/server.py
```

调试模式：

```bash
DEBUG=1 python3 backend/server.py
```

后端默认启动 WebSocket：

```text
ws://localhost:8765
```

## 启动前端

```bash
cd web
npm run dev
```

然后打开：

```text
http://localhost:8000
```

## 使用流程

1. 在页面选择“监测某个设备功耗”。
2. 输入设备编号。
3. 点击“开始监测”。
4. 如需查询历史续航状态，选择“查询某个设备功耗数据”，输入设备编号后查询。

后端只有收到非空设备编号和“开始监测”命令后才会采样、计算和写入数据库。

## 依赖

后端：

```bash
pip install pyserial websockets
```

前端只使用 Node 内置 HTTP 服务，不需要安装第三方包。
