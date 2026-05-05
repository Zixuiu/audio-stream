"""
音频输出设备检测和测试
"""
import pyaudio
import numpy as np

p = pyaudio.PyAudio()

print("=" * 50)
print("音频输出设备列表")
print("=" * 50)

for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    if info['maxOutputChannels'] > 0:
        print(f"设备 {i}: {info['name']}")
        print(f"  输出通道数: {info['maxOutputChannels']}")
        print(f"  采样率: {info['defaultSampleRate']}")
        print()

try:
    default = p.get_default_output_device_info()
    print(f"默认输出设备: {default['name']} (设备 {default['index']})")
except Exception as e:
    print(f"获取默认输出设备失败: {e}")

print()
print("=" * 50)
print("正在测试音频输出...")
print("=" * 50)

try:
    stream = p.open(
        format=pyaudio.paFloat32,
        channels=1,
        rate=44100,
        output=True,
    )
    
    # 生成 440 Hz 的正弦波（标准A音）
    duration = 1.0  # 1秒
    frequency = 440.0
    samples = np.sin(2 * np.pi * frequency * np.arange(int(44100 * duration)) / 44100)
    audio_data = samples.astype(np.float32).tobytes()
    
    print(f"正在播放 {frequency}Hz 测试音 1 秒...")
    stream.write(audio_data)
    stream.stop_stream()
    stream.close()
    print("测试音播放完成！如果听到声音，说明音频输出设备正常。")
except Exception as e:
    print(f"测试音播放失败: {e}")

p.terminate()
