"""
简单测试：直接播放测试音，不依赖WebSocket
用于确认音频设备是否正常工作
"""
import pyaudio
import numpy as np
import time

p = pyaudio.PyAudio()

print("=" * 50)
print("音频播放测试")
print("=" * 50)

# 使用默认设备
stream = p.open(
    format=pyaudio.paFloat32,
    channels=1,
    rate=44100,
    output=True,
)

print("\n正在播放测试音...")

# 生成 440Hz 正弦波（标准A音）
duration = 2.0  # 2秒
frequency = 440.0
samples = np.sin(2 * np.pi * frequency * np.arange(int(44100 * duration)) / 44100)
audio_data = samples.astype(np.float32).tobytes()

stream.write(audio_data)

print("播放完成！")
stream.stop_stream()
stream.close()
p.terminate()
