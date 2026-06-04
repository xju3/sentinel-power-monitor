#!/usr/bin/env python3
"""
简单 HTTP 服务器 - 用于在浏览器中打开 index.html
"""

import http.server
import socketserver
import os
import sys
from pathlib import Path

PORT = 8000
SCRIPT_DIR = Path(__file__).parent

class MyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        """自定义日志输出"""
        print(f"[HTTP] {format % args}")

def start_http_server():
    """启动 HTTP 服务器"""
    os.chdir(SCRIPT_DIR)
    
    try:
        with socketserver.TCPServer(("", PORT), MyHTTPRequestHandler) as httpd:
            print(f"\n{'='*60}")
            print(f"[HTTP] 服务器启动于 http://localhost:{PORT}")
            print(f"[HTTP] 打开浏览器: http://localhost:{PORT}/index.html")
            print(f"{'='*60}\n")
            
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n[停止] HTTP 服务器已关闭")
    except OSError as e:
        if e.errno == 48:  # Address already in use
            print(f"\n[错误] 端口 {PORT} 已被占用")
            print(f"[提示] 尝试使用其他端口或杀死占用进程")
        else:
            print(f"\n[错误] {e}")
        sys.exit(1)

if __name__ == '__main__':
    start_http_server()
