"""
手机音频 → 电脑播放 服务端（HTTPS + WSS）
优化版本：使用阻塞模式音频播放 + 自动选择最佳音频设备 + 更大的预缓冲
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

# 音频增强
GAIN = 20.0  # 增大增益
PREBUFFER_SECONDS = 0.5   # 预缓冲 500ms
MAX_BUFFER_SECONDS = 2.0  # 最大缓冲 2 秒

# ==================== 路径 ====================
BASE_DIR = Path(__file__).parent
CERT_FILE = BASE_DIR / "cert.pem"
KEY_FILE = BASE_DIR / "key.pem"


# ==================== 高性能环形缓冲区 ====================
class RingBuffer:
    """基于 bytearray 的环形缓冲区"""

    def __init__(self, size):
        self.buf = bytearray(size)
        self.size = size
        self.write_pos = 0
        self.read_pos = 0
        self.count = 0
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            n = len(data)
            if n >= self.size:
                self.buf[-self.size:] = data[-self.size:]
                self.write_pos = 0
                self.read_pos = 0
                self.count = self.size
                return

            space = self.size - self.count
            if n > space:
                discard = n - space
                self.read_pos = (self.read_pos + discard) % self.size
                self.count -= discard

            first = min(n, self.size - self.write_pos)
            self.buf[self.write_pos:self.write_pos + first] = data[:first]
            if first < n:
                self.buf[:n - first] = data[first:]
            self.write_pos = (self.write_pos + n) % self.size
            self.count += n

    def read(self, n):
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

    def clear(self):
        with self.lock:
            self.write_pos = 0
            self.read_pos = 0
            self.count = 0


# ==================== 音频设备检测 ====================
def get_best_output_device(p):
    """使用系统默认音频输出设备"""
    try:
        best_device = p.get_default_output_device_info()
        print(f"\n使用默认音频设备: {best_device['name']} (索引 {best_device['index']})")
        return best_device['index']
    except Exception as e:
        print(f"\n无法获取默认设备: {e}")
        return None


# ==================== 音频播放器（阻塞模式） ====================
class AudioPlayer:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.lock = threading.Lock()
        self.buffer = RingBuffer(int(SAMPLE_RATE * CHANNELS * 2 * MAX_BUFFER_SECONDS))
        self.prebuffer_target = int(SAMPLE_RATE * CHANNELS * 2 * PREBUFFER_SECONDS)
        self.playing = True
        self.total_frames_received = 0
        self.total_bytes_sent = 0
        
        # 检测并选择最佳音频设备
        device_index = get_best_output_device(self.pa)
        self._open_stream(device_index)
        
        # 启动播放线程
        self.play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self.play_thread.start()

    def _open_stream(self, device_index=None):
        kwargs = {
            'format': FORMAT,
            'channels': CHANNELS,
            'rate': SAMPLE_RATE,
            'output': True,
            'frames_per_buffer': 1024,  # 更小的缓冲区降低延迟
        }
        
        if device_index is not None:
            kwargs['output_device_index'] = device_index
            print(f"使用指定设备 (索引 {device_index})")
        else:
            print("使用默认音频设备")
        
        self.stream = self.pa.open(**kwargs)
        print(f"音频流已打开: 采样率={SAMPLE_RATE}, 通道={CHANNELS}")

    def _play_loop(self):
        """阻塞模式播放循环"""
        bytes_per_frame = 1024 * CHANNELS * 2
        
        while self.playing:
            if self.buffer.available >= self.prebuffer_target:
                chunk = self.buffer.read(bytes_per_frame)
                try:
                    self.stream.write(chunk)
                    self.total_bytes_sent += len(chunk)
                except Exception as e:
                    print(f"[!] 播放错误: {e}")
            else:
                # 缓冲区不足，等待一下
                time.sleep(0.01)

    def feed(self, audio_data: bytes):
        """接收 PCM 数据 → 增益 → 写入缓冲区"""
        self.total_frames_received += 1
        
        # 转换为 float 进行增益处理
        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float64)
        
        # 应用增益
        samples = np.clip(samples * GAIN, -32768, 32767)
        
        # 写回缓冲区
        self.buffer.write(samples.astype(np.int16).tobytes())

    def reset(self):
        """新连接时重置预缓冲"""
        self.buffer.clear()
        print("[↻] 缓冲区已重置")

    def stop(self):
        self.playing = False
        time.sleep(0.1)
        with self.lock:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except Exception:
                    pass
            self.pa.terminate()

    def get_stats(self):
        return {
            'buffer_available': self.buffer.available,
            'prebuffer_target': self.prebuffer_target,
            'frames_received': self.total_frames_received,
            'bytes_sent': self.total_bytes_sent,
        }


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
    
    bytes_received = 0

    try:
        async for message in websocket:
            if isinstance(message, bytes):
                player.feed(message)
                bytes_received += len(message)
                if bytes_received % (44100 * 2) < 1000:  # 每约1秒打印一次
                    print(f"[📊] 已接收: {bytes_received // 1024}KB")
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
                b"Cache-Control: no-cache\r\n"
                + f"Content-Length: {len(content)}\r\n".encode()
                + b"\r\n" + content
            )
            writer.write(response)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    except Exception as e:
        print(f"[!] HTTPS handler error: {e}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ==================== 状态监控 ====================
async def monitor_player(player: AudioPlayer):
    """定期打印播放器状态"""
    while True:
        await asyncio.sleep(5)
        stats = player.get_stats()
        if stats['frames_received'] > 0:
            buffer_percent = (stats['buffer_available'] / stats['prebuffer_target']) * 100 if stats['prebuffer_target'] > 0 else 0
            print(f"[📊] 状态: 缓冲区={buffer_percent:.0f}%, 接收帧={stats['frames_received']}, 发送字节={stats['bytes_sent'] // 1024}KB")


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
    
    # 启动状态监控
    monitor_task = asyncio.create_task(monitor_player(player))
    
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
