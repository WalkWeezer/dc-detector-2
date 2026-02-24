#!/usr/bin/env python3
"""Диагностика Picamera2 на Raspberry Pi 5."""

import subprocess
import sys
import time

def header(text):
    print(f"\n{'='*50}")
    print(f"  {text}")
    print(f"{'='*50}")

def run_cmd(cmd, desc=""):
    if desc:
        print(f"\n--- {desc} ---")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip()
        if out:
            print(out)
        return r.returncode == 0, out
    except subprocess.TimeoutExpired:
        print("[TIMEOUT]")
        return False, ""

def main():
    header("1. Проверка системы")
    run_cmd("cat /proc/device-tree/model", "Модель платы")
    run_cmd("uname -a", "Ядро")
    run_cmd("vcgencmd version 2>/dev/null || echo 'vcgencmd недоступен'", "Firmware")

    header("2. Проверка подключения камеры")
    ok, out = run_cmd("libcamera-hello --list-cameras 2>&1", "Обнаруженные камеры (libcamera)")
    if "No cameras" in out or not ok:
        print("\n[!] Камера НЕ обнаружена через libcamera.")
        print("    Проверьте:")
        print("    - Шлейф вставлен правильно (контакты вниз, к плате)")
        print("    - Используется переходник 22pin->15pin для RPi5")
        print("    - В /boot/firmware/config.txt нет dtoverlay=vc4-fkms-v3d")
        print("    - Камера включена: sudo raspi-config -> Interface -> Camera")
        print("    - Попробуйте: sudo reboot")

        header("2a. Дополнительная диагностика")
        run_cmd("dmesg | grep -i -E 'camera|imx|ov5647|arducam|csi|unicam' | tail -20",
                "dmesg (camera/csi)")
        run_cmd("ls -la /dev/video* 2>/dev/null || echo 'Нет /dev/video* устройств'",
                "Video devices")
        run_cmd("cat /boot/firmware/config.txt | grep -v '^#' | grep -v '^$' | grep -i -E 'camera|dtoverlay|auto_detect'",
                "config.txt (camera-related)")
        return False

    print("\n[OK] Камера обнаружена!")

    header("3. Проверка Picamera2")
    try:
        from picamera2 import Picamera2
        print(f"Picamera2 версия: {Picamera2.__version__ if hasattr(Picamera2, '__version__') else 'installed'}")
    except ImportError:
        print("[!] Picamera2 не установлен.")
        print("    Установите: sudo apt install -y python3-picamera2")
        return False

    header("4. Инициализация камеры")
    try:
        picam2 = Picamera2()
        props = picam2.camera_properties
        print(f"Модель сенсора : {props.get('Model', '?')}")
        print(f"Размер пикселя : {props.get('UnitCellSize', '?')}")
        print(f"Разрешение     : {props.get('PixelArraySize', '?')}")
        sensor_modes = picam2.sensor_modes
        print(f"Режимов сенсора: {len(sensor_modes)}")
        for i, m in enumerate(sensor_modes):
            print(f"  [{i}] {m.get('size', '?')}  format={m.get('format', '?')}  fps={m.get('fps', '?')}")
    except Exception as e:
        print(f"[!] Ошибка инициализации: {e}")
        return False

    header("5. Захват тестового кадра")
    try:
        from picamera2 import Preview
        import numpy as np

        config = picam2.create_still_configuration(main={"size": (1920, 1080)})
        picam2.configure(config)
        picam2.start()
        time.sleep(2)  # автоэкспозиция

        frame = picam2.capture_array()
        print(f"Кадр получен: {frame.shape}, dtype={frame.dtype}")
        print(f"Мин/Макс/Среднее: {frame.min()}/{frame.max()}/{frame.mean():.1f}")

        if frame.max() == 0:
            print("[!] Кадр полностью чёрный — возможно проблема с сенсором")
        elif frame.min() == 255:
            print("[!] Кадр полностью белый — возможно проблема с экспозицией")
        else:
            print("[OK] Кадр выглядит нормально")

        # Сохраняем тестовый снимок
        from PIL import Image
        img = Image.fromarray(frame)
        test_path = "test_capture.jpg"
        img.save(test_path, quality=90)
        print(f"[OK] Снимок сохранён: {test_path}")

    except Exception as e:
        print(f"[!] Ошибка захвата: {e}")
        return False
    finally:
        try:
            picam2.stop()
            picam2.close()
        except:
            pass

    header("6. Тест видеопотока (3 сек)")
    try:
        picam2 = Picamera2()
        video_config = picam2.create_video_configuration(main={"size": (1280, 720), "format": "RGB888"})
        picam2.configure(video_config)
        picam2.start()
        time.sleep(1)

        frames = 0
        t0 = time.time()
        while time.time() - t0 < 3.0:
            picam2.capture_array()
            frames += 1

        elapsed = time.time() - t0
        fps = frames / elapsed
        print(f"Кадров: {frames} за {elapsed:.1f}с = {fps:.1f} FPS")

        if fps < 5:
            print("[!] FPS низкий — возможно USB-ограничение или нагрузка на CPU")
        else:
            print(f"[OK] Видеопоток работает ({fps:.0f} FPS)")

    except Exception as e:
        print(f"[!] Ошибка видеопотока: {e}")
        return False
    finally:
        try:
            picam2.stop()
            picam2.close()
        except:
            pass

    header("РЕЗУЛЬТАТ")
    print("[OK] Камера работает корректно!")
    return True


if __name__ == "__main__":
    header("Диагностика Picamera2 — Raspberry Pi 5")
    ok = main()
    sys.exit(0 if ok else 1)
