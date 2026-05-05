"""
手机音频 → 电脑播放 服务端（HTTPS + WSS）
核心优化：bytearray 环形缓冲区 + 大预缓冲 + 音量增益
用法: python server.py
手机浏览器打开 https://<电脑IP>:8765
"""

import asyncio
import json
import socket
import ssl
import threading
import time
import webbrowser
from pathlib import Path

import numpy as np
import pyaudio
import websockets

# ==================== 配置 ====================
HOST = "0.0.0.0"
PORT = 8765
WS_PORT = 8766
SAMPLE_RATE = 44100
CHANNELS = 1
FORMAT = pyaudio.paInt16
OUTPUT_FRAMES = 4096

# 音频增强
GAIN = 5.0
PREBUFFER_SECONDS = 0.3   # 预缓冲 300ms，彻底消除开头卡顿
MAX_BUFFER_SECONDS = 1.0  # 最大缓冲 1 秒，防止延迟过大

# ==================== 路径 ====================
BASE_DIR = Path(__file__).parent
CERT_FILE = BASE_DIR / "cert.pem"
KEY_FILE = BASE_DIR / "key.pem"

# ==================== 高性能环形缓冲区 ====================
class RingBuffer:
    """基于 bytearray 的环形缓冲区，比 deque 快很多"""

    def __init__(self, size):
        self.buf = bytearray(size)
        self.size = size
        self.write_pos = 0
        self.read_pos = 0
        self.count = 0
        self.lock = threading.Lock()

    def write(self, data):
        """写入数据，满了就丢弃最旧的"""
        with self.lock:
            n = len(data)
            # 如果数据比缓冲区还大，只保留最后 size 字节
            if n >= self.size:
                self.buf[-self.size:] = data[-self.size:]
                self.write_pos = 0
                self.read_pos = 0
                self.count = self.size
                return

            # 丢弃旧数据腾出空间
            space = self.size - self.count
            if n > space:
                discard = n - space
                self.read_pos = (self.read_pos + discard) % self.size
                self.count -= discard

            # 写入数据（可能分两段）
            first = min(n, self.size - self.write_pos)
            self.buf[self.write_pos:self.write_pos + first] = data[:first]
            if first < n:
                self.buf[:n - first] = data[first:]
            self.write_pos = (self.write_pos + n) % self.size
            self.count += n

    def read(self, n):
        """读取 n 字节，不够补零"""
        with self.lock:
            if self.count == 0:
                return b'\x00' * n

            actual = min(n, self.count)
            result = bytearray(actual)

            first = min(actual, self.size - self.read_pos)
            result[:first] = self.buf[self.read_pos:self.read_pos + first]
            if first < actual:
                result[first:] = self.buf[:actual - first]

            self.read_pos = (self.read_pos + actual) % self.size
            self.count -= actual

            if actual < n:
                result.extend(b'\x00' * (n - actual))

            return bytes(result)

    @property
    def available(self):
        with self.lock:
            return self.count


# ==================== 音频播放器 ====================
class AudioPlayer:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.lock = threading.Lock()
        self.buffer = RingBuffer(int(SAMPLE_RATE * CHANNELS * 2 * MAX_BUFFER_SECONDS))
        self.prebuffer_target = int(SAMPLE_RATE * CHANNELS * 2 * PREBUFFER_SECONDS)
        self.prebuffer_done = False
        self._open_stream()

    def _open_stream(self):
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=OUTPUT_FRAMES,
            stream_callback=self._callback,
        )

    def _callback(self, in_data, frame_count, time_info, status):
        bytes_needed = frame_count * CHANNELS * 2

        if not self.prebuffer_done:
            if self.buffer.available >= self.prebuffer_target:
                self.prebuffer_done = True
            else:
                return (b'\x00' * bytes_needed, pyaudio.paContinue)

        chunk = self.buffer.read(bytes_needed)
        return (chunk, pyaudio.paContinue)

    def feed(self, audio_data: bytes):
        """接收 PCM 数据 → 增益 → 写入缓冲区"""
        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float64)
        samples = np.clip(samples * GAIN, -32768, 32767)
        self.buffer.write(samples.astype(np.int16).tobytes())

    def reset(self):
        """新连接时重置预缓冲"""
        self.prebuffer_done = False

    def stop(self):
        with self.lock:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
            self.pa.terminate()


# ==================== 工具函数 ====================
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def create_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    return ctx


# ==================== WebSocket ====================
async def handle_client(websocket, player: AudioPlayer):
    client_addr = websocket.remote_address
    print(f"[+] 客户端已连接: {client_addr}")
    player.reset()

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                player.feed(message)
            elif isinstance(message, str):
                try:
                    data = json.loads(message)
                    cmd = data.get("type", "")
                    if cmd == "start":
                        player.reset()
                        print(f"[▶] 客户端开始传输音频")
                    elif cmd == "stop":
                        print(f"[⏸] 客户端停止传输音频")
                except json.JSONDecodeError:
                    pass
    except websockets.exceptions.ConnectionClosed:
        print(f"[-] 客户端已断开: {client_addr}")
    except Exception as e:
        print(f"[!] 客户端错误 ({client_addr}): {e}")


# ==================== HTTPS ====================
async def https_handler(reader, writer):
    try:
        request_line = (await reader.readline()).decode("utf-8", errors="ignore")
        if "GET" in request_line:
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            html_path = BASE_DIR / "index.html"
            content = html_path.read_bytes() if html_path.exists() else b"<h1>index.html not found</h1>"
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(content)}\r\n".encode()
                + b"\r\n" + content
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
    print(f"  音量增益:    {GAIN}x")
    print(f"  预缓冲:      {int(PREBUFFER_SECONDS*1000)}ms")
    print("=" * 55)
    print(f"\n  📱 手机浏览器打开: https://{local_ip}:{PORT}")
    print(f"  ⚠️  首次访问点「高级」→「继续访问」")
    print(f"  按 Ctrl+C 停止服务\n")

    https_server = await asyncio.start_server(https_handler, HOST, PORT, ssl=ssl_ctx)
    wss_server = await websockets.serve(lambda ws: handle_client(ws, player), HOST, WS_PORT, ssl=ssl_ctx)
    webbrowser.open(f"https://localhost:{PORT}")

    try:
        await asyncio.gather(https_server.serve_forever(), wss_server.serve_forever())
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
