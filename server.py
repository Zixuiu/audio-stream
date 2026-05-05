"""
手机音频 → 电脑播放 服务端
用法: python server.py
然后在手机浏览器中打开 http://<电脑IP>:8765
"""

import asyncio
import json
import socket
import struct
import sys
import threading
import time
import webbrowser
from pathlib import Path

import numpy as np
import pyaudio
import websockets

# ==================== 配置 ====================
HOST = "0.0.0.0"          # 监听所有网卡，手机可通过局域网访问
PORT = 8765               # 服务端口
SAMPLE_RATE = 44100       # 采样率
CHANNELS = 1              # 单声道
FRAMES_PER_BUFFER = 4096  # 每次播放的帧数
FORMAT = pyaudio.paInt16  # 16位 PCM

# ==================== 音频播放器 ====================
class AudioPlayer:
    """使用 PyAudio 播放实时音频流"""

    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.lock = threading.Lock()
        self._open_stream()

    def _open_stream(self):
        """打开音频输出流"""
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=FRAMES_PER_BUFFER,
        )

    def play(self, audio_data: bytes):
        """播放一段 PCM 音频数据"""
        with self.lock:
            try:
                self.stream.write(audio_data)
            except Exception:
                # 流出错时重新打开
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
                self._open_stream()
                self.stream.write(audio_data)

    def stop(self):
        """停止并关闭音频流"""
        with self.lock:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
            self.pa.terminate()


# ==================== 获取本机局域网 IP ====================
def get_local_ip() -> str:
    """获取本机局域网 IP 地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ==================== WebSocket 处理 ====================
async def handle_client(websocket, player: AudioPlayer):
    """处理单个客户端的 WebSocket 连接"""
    client_addr = websocket.remote_address
    print(f"[+] 客户端已连接: {client_addr}")

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                # 二进制数据 = PCM 音频
                player.play(message)
            elif isinstance(message, str):
                # JSON 文本消息 = 控制命令
                try:
                    data = json.loads(message)
                    cmd = data.get("type", "")

                    if cmd == "config":
                        print(f"[*] 客户端音频配置: {data}")
                    elif cmd == "start":
                        print(f"[▶] 客户端开始传输音频")
                    elif cmd == "stop":
                        print(f"[⏸] 客户端停止传输音频")
                except json.JSONDecodeError:
                    pass
    except websockets.exceptions.ConnectionClosed:
        print(f"[-] 客户端已断开: {client_addr}")
    except Exception as e:
        print(f"[!] 客户端错误 ({client_addr}): {e}")


# ==================== HTTP 服务（提供网页） ====================
async def http_handler(reader, writer):
    """简单的 HTTP 服务器，返回 index.html"""
    try:
        request_line = (await reader.readline()).decode("utf-8", errors="ignore")
        if "GET" in request_line:
            # 读取剩余请求头
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            html_path = Path(__file__).parent / "index.html"
            if html_path.exists():
                content = html_path.read_bytes()
            else:
                content = b"<h1>index.html not found</h1>"

            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Connection: close\r\n"
                f"Content-Length: {len(content)}\r\n"
                b"\r\n"
            ) + content
            writer.write(response)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        await writer.wait_closed()


# ==================== 主函数 ====================
async def main():
    local_ip = get_local_ip()
    player = AudioPlayer()

    print("=" * 55)
    print("  🎵 手机音频 → 电脑播放 服务端")
    print("=" * 55)
    print(f"  本机 IP:   {local_ip}")
    print(f"  监听端口:  {PORT}")
    print(f"  采样率:    {SAMPLE_RATE} Hz")
    print("=" * 55)
    print(f"\n  📱 手机浏览器打开: http://{local_ip}:{PORT}")
    print(f"  💻 电脑浏览器打开: http://localhost:{PORT}")
    print(f"\n  按 Ctrl+C 停止服务\n")

    # 启动 HTTP 服务器（提供网页）
    http_server = await asyncio.start_server(http_handler, HOST, PORT)

    # 启动 WebSocket 服务器（音频传输）
    ws_server = await websockets.serve(
        lambda ws: handle_client(ws, player),
        HOST,
        PORT + 1,  # WebSocket 用 PORT+1
    )

    # 自动打开浏览器
    webbrowser.open(f"http://localhost:{PORT}")

    try:
        await asyncio.gather(
            http_server.serve_forever(),
            ws_server.serve_forever(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        player.stop()
        print("\n[✓] 服务已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[✓] 正在关闭服务...")
