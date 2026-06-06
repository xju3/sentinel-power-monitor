#!/usr/bin/env python3
"""
快速启动脚本 - WebSocket 功耗监控服务
支持调试模式（无硬件）和实时模式（有硬件）
"""

import sys
import os
import subprocess


def start_server():
    """启动 WebSocket 服务器"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    server_script = os.path.join(script_dir, 'power_monitor.py')
    
    print("\n" + "="*60)
    print("⚡ KV-AMP700mT 功耗监控服务")
    print("="*60)
    print("\n启动选项:")
    print("  [1] 实时监控 (需要硬件连接)")
    print("  [2] 调试模式 (模拟数据，测试 WebSocket)")
    print("  [3] 退出")
    
    choice = input("\n请选择 [1-3]: ").strip()
    
    if choice == '1':
        print("\n[启动] 实时监控模式...")
        print("[提示] 确保 KV-AMP700mT 已通过 USB 连接\n")
        # 使用 subprocess.run 替代 os.system，更加健壮
        subprocess.run([sys.executable, server_script])
    elif choice == '2':
        print("\n[启动] 调试模式（模拟数据）...\n")
        env = os.environ.copy()
        env['DEBUG'] = '1'
        # 使用 subprocess.run 并传递 env 字典，实现跨平台
        subprocess.run([sys.executable, server_script], env=env)
    elif choice == '3':
        print("[退出]\n")
        sys.exit(0)
    else:
        print("[错误] 无效选择\n")
        start_server()

if __name__ == '__main__':
    try:
        start_server()
    except KeyboardInterrupt:
        print("\n\n[停止] 用户中断")
        sys.exit(0)
