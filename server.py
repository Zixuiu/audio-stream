"""
手机音频 → 电脑播放 服务端（HTTPS + WSS）
用法: python server.py
然后在手机浏览器中打开 https://<电脑IP>:8765
首次访问时浏览器会提示证书不安全，点击「高级」→「继续访问」即可
"""

import asyncio
import json
import os
import socket
import ssl
import threading
import webbrowser
from pathlib import Path

import pyaudio
import websockets

# ==================== 配置 ====================
HOST = "0.0.0.0"          # 监听所有网卡
PORT = 8765               # HTTPS 端口
WS_PORT = 8766            # WSS 端口
SAMPLE_RATE = 44100       # 采样率
CHANNELS = 1              # 单声道
FRAMES_PER_BUFFER = 4096  # 每次播放的帧数
FORMAT = pyaudio.paInt16  # 16位 PCM

# ==================== SSL 证书路径 ====================
BASE_DIR = Path(__file__).parent
CERT_FILE = BASE_DIR / "cert.pem"
KEY_FILE = BASE_DIR / "key.pem"


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
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ==================== 创建 SSL 上下文 ====================
def create_ssl_context():
    """创建 SSL 上下文（自签名证书）"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    return ctx


# ==================== WebSocket 处理 ====================
async def handle_client(websocket, player: AudioPlayer):
    """处理单个客户端的 WebSocket 连接"""
    client_addr = websocket.remote_address
    print(f"[+] 客户端已连接: {client_addr}")

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                player.play(message)
            elif isinstance(message, str):
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


# ==================== HTTPS 服务（提供网页） ====================
async def https_handler(reader, writer):
    """简单的 HTTPS 服务器，返回 index.html"""
    try:
        request_line = (await reader.readline()).decode("utf-8", errors="ignore")
        if "GET" in request_line:
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            html_path = BASE_DIR / "index.html"
            if html_path.exists():
                content = html_path.read_bytes()
            else:
                content = b"<h1>index.html not found</h1>"

            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(content)}\r\n".encode()
                + b"\r\n"
                + content
            )
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
    ssl_ctx = create_ssl_context()

    print("=" * 55)
    print("  🎵 手机音频 → 电脑播放 服务端 (HTTPS)")
    print("=" * 55)
    print(f"  本机 IP:   {local_ip}")
    print(f"  HTTPS 端口: {PORT}")
    print(f"  WSS 端口:   {WS_PORT}")
    print(f"  采样率:    {SAMPLE_RATE} Hz")
    print("=" * 55)
    print(f"\n  📱 手机浏览器打开: https://{local_ip}:{PORT}")
    print(f"  💻 电脑浏览器打开: https://localhost:{PORT}")
    print(f"\n  ⚠️  首次访问会提示证书不安全，点击「高级」→「继续访问」")
    print(f"  按 Ctrl+C 停止服务\n")

    # 启动 HTTPS 服务器（提供网页）
    https_server = await asyncio.start_server(
        https_handler, HOST, PORT, ssl=ssl_ctx
    )

    # 启动 WSS 服务器（音频传输）
    wss_server = await websockets.serve(
        lambda ws: handle_client(ws, player),
        HOST,
        WS_PORT,
        ssl=ssl_ctx,
    )

    # 自动打开浏览器
    webbrowser.open(f"https://localhost:{PORT}")

    try:
        await asyncio.gather(
            https_server.serve_forever(),
            wss_server.serve_forever(),
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
