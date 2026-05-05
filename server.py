"""
手机音频 → 电脑播放 服务端（HTTPS + WSS）
用法: python server.py
然后在手机浏览器中打开 https://<电脑IP>:8765
首次访问时浏览器会提示证书不安全，点击「高级」→「继续访问」即可
"""

import asyncio
import collections
import json
import socket
import ssl
import struct
import threading
import time
import webbrowser
from pathlib import Path

import numpy as np
import pyaudio
import websockets

# ==================== 配置 ====================
HOST = "0.0.0.0"
PORT = 8765               # HTTPS 端口
WS_PORT = 8766            # WSS 端口
SAMPLE_RATE = 44100
CHANNELS = 1
FORMAT = pyaudio.paInt16
FRAMES_PER_BUFFER = 2048  # 较小的播放缓冲区，配合环形缓冲区使用

# 音频增强配置
GAIN = 5.0                # 音量增益倍数（解决声音小的问题）
RING_BUFFER_SIZE = 65536  # 环形缓冲区大小（字节），约 0.37 秒，解决卡顿
TARGET_BUFFER_MS = 150    # 目标预缓冲毫秒数，平衡延迟和流畅度

# ==================== SSL 证书路径 ====================
BASE_DIR = Path(__file__).parent
CERT_FILE = BASE_DIR / "cert.pem"
KEY_FILE = BASE_DIR / "key.pem"


# ==================== 带增益的音频播放器 ====================
class AudioPlayer:
    """使用 PyAudio + 环形缓冲区 + 增益 播放实时音频流"""

    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.lock = threading.Lock()
        self.ring_buffer = collections.deque()  # 存放 PCM bytes 块
        self.buffer_event = threading.Event()    # 有新数据时通知
        self.total_bytes = 0
        self.prebuffer_done = False
        self._open_stream()

    def _open_stream(self):
        """打开音频输出流（使用回调模式，避免阻塞）"""
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=self._callback,
        )

    def _callback(self, in_data, frame_count, time_info, status):
        """PyAudio 回调：从环形缓冲区取数据播放"""
        bytes_needed = frame_count * CHANNELS * 2  # 16bit = 2 bytes
        chunk = b""

        with self.lock:
            while len(chunk) < bytes_needed and self.ring_buffer:
                piece = self.ring_buffer.popleft()
                chunk += piece

        # 不够的部分补零（静音），避免爆音
        if len(chunk) < bytes_needed:
            chunk += b"\x00" * (bytes_needed - len(chunk))

        return (chunk, pyaudio.paContinue)

    def feed(self, audio_data: bytes):
        """将音频数据送入环形缓冲区（带增益处理）"""
        # 将 bytes 转为 int16 numpy 数组
        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float64)

        # 应用增益
        samples = samples * GAIN

        # 软限幅，防止爆音
        samples = np.clip(samples, -32768, 32767)

        # 转回 int16 bytes
        boosted = samples.astype(np.int16).tobytes()

        # 计算当前缓冲区大小
        with self.lock:
            current_size = sum(len(b) for b in self.ring_buffer)
            self.ring_buffer.append(boosted)
            self.total_bytes += len(boosted)
            new_size = current_size + len(boosted)

        # 预缓冲：积累足够数据后再开始播放，避免开头卡顿
        target_bytes = int(SAMPLE_RATE * CHANNELS * 2 * TARGET_BUFFER_MS / 1000)
        if not self.prebuffer_done:
            if new_size >= target_bytes:
                self.prebuffer_done = True
                print(f"[▶] 预缓冲完成 ({TARGET_BUFFER_MS}ms)，开始播放")
        else:
            # 缓冲区过大时丢弃旧数据，防止延迟累积
            max_bytes = RING_BUFFER_SIZE * 2
            if new_size > max_bytes:
                with self.lock:
                    while sum(len(b) for b in self.ring_buffer) > RING_BUFFER_SIZE:
                        self.ring_buffer.popleft()

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
                player.feed(message)
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
    print(f"  本机 IP:     {local_ip}")
    print(f"  HTTPS 端口:  {PORT}")
    print(f"  WSS 端口:    {WS_PORT}")
    print(f"  采样率:      {SAMPLE_RATE} Hz")
    print(f"  音量增益:    {GAIN}x")
    print(f"  预缓冲:      {TARGET_BUFFER_MS}ms")
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
