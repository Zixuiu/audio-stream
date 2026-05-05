"""
测试不同音频设备哪个能正常播放声音
"""
import pyaudio
import numpy as np
import time

p = pyaudio.PyAudio()

# 要测试的设备列表（输出设备）
devices_to_test = [
    (4, "FxSound Audio Enhancer"),
    (6, "Realtek(R) Audio - 8ch"),
    (16, "Realtek(R) Audio - 2ch"),
    (19, "Headphones (Realtek HD Audio 2nd output)"),
    (20, "Speakers (Realtek HD Audio output)"),
]

print("=" * 60)
print("音频设备测试")
print("=" * 60)
print("\n将为每个设备播放 1 秒 440Hz 测试音")
print("听到声音的设备就是正确的\n")

for device_index, device_name in devices_to_test:
    try:
        print(f"\n测试设备 {device_index}: {device_name}")
        print("-" * 40)
        
        stream = p.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=44100,
            output=True,
            output_device_index=device_index,
        )
        
        # 生成 440 Hz 测试音
        duration = 1.0
        frequency = 440.0
        samples = np.sin(2 * np.pi * frequency * np.arange(int(44100 * duration)) / 44100)
        audio_data = samples.astype(np.float32).tobytes()
        
        print(f"  播放测试音...")
        stream.write(audio_data)
        stream.stop_stream()
        stream.close()
        
        response = input(f"  你听到声音了吗？(y/n): ").strip().lower()
        if response == 'y':
            print(f"  ✓ 找到正确设备！索引: {device_index}")
            print(f"\n  请在 server.py 中设置 device_index = {device_index}")
            break
        else:
            print(f"  ✗ 没有听到声音")
        
        time.sleep(0.5)
        
    except Exception as e:
        print(f"  ✗ 设备打开失败: {e}")

p.terminate()
print("\n测试完成！")
