"""
Pi 3B Simulator — Multi-Preprocessor Demo
==========================================
Показывает все лёгкие препроцессоры одновременно в сетке панелей.

Каждая панель:
  • Препроцессор работает на НИЗКОМ разрешении (быстро)
  • Находит ROI → вырезает из ОРИГИНАЛЬНОГО разрешения
  • YOLO получает только вырезки высокого разрешения
  • На панели: чёрный фон + только то, что модель видит + боксы

Usage:
    python tools/pi3b_sim.py --source 0
    python tools/pi3b_sim.py --demo
    python tools/pi3b_sim.py --source video.mp4 --no-throttle

Controls: [T] throttle  [Q] выход

Requirements: pip install opencv-python numpy psutil
Optional:     pip install ultralytics
"""

import argparse
import time
import threading
import queue as _queue
import sys
import random
from dataclasses import dataclass, field
from collections import deque
from typing import List, Tuple, Callable

import cv2
import numpy as np
import psutil

# ──────────────────────────────────────────────────────────────────────────────
# Константы
# ──────────────────────────────────────────────────────────────────────────────

# Разрешение для работы препроцессора (маленькое = дёшево)
PREPROC_W, PREPROC_H = 320, 240   # MOG2 работает на этом разрешении
MAX_CROP_PX = 320                  # ROI-кроп ресайзится до MAX_CROP_PX перед YOLO

# Размер каждой панели в GUI
PANEL_W, PANEL_H = 480, 360

# Pi 3B throttle
PI3B_CPU_FREQ  = 1.4
PC_CPU_FREQ    = 3.8
PI3B_IPC       = 0.35
SLOWDOWN       = (PC_CPU_FREQ / PI3B_CPU_FREQ) * (1.0 / PI3B_IPC)  # ≈ 7.7×
PI3B_RAM_MB    = 1024

# Задержка FakeYOLO (YOLOv8n на Pi3B при 640×640 ≈ 280ms)
YOLO_REF_MS    = 280
YOLO_REF_PX    = 640 * 640


def throttle(start: float, factor: float = SLOWDOWN) -> None:
    extra = (time.perf_counter() - start) * (factor - 1.0)
    if extra > 0:
        time.sleep(extra)


# ──────────────────────────────────────────────────────────────────────────────
# ROI
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ROI:
    x: int; y: int; w: int; h: int

    @property
    def area(self) -> int:
        return self.w * self.h

    def scale(self, sx: float, sy: float) -> "ROI":
        return ROI(int(self.x * sx), int(self.y * sy),
                   int(self.w * sx), int(self.h * sy))

    def clamp(self, W: int, H: int) -> "ROI":
        x = max(0, min(self.x, W - 1))
        y = max(0, min(self.y, H - 1))
        w = max(1, min(self.w, W - x))
        h = max(1, min(self.h, H - y))
        return ROI(x, y, w, h)

    def crop(self, frame: np.ndarray) -> np.ndarray:
        return frame[self.y:self.y + self.h, self.x:self.x + self.w]


def merge_rois(rois: List[ROI], gap: int = 8) -> List[ROI]:
    """Объединяет ROI которые перекрываются или близко стоят."""
    if not rois:
        return []
    changed = True
    boxes = list(rois)
    while changed:
        changed = False
        result, used = [], [False] * len(boxes)
        for i in range(len(boxes)):
            if used[i]:
                continue
            a = boxes[i]
            grp = [a]
            for j in range(i + 1, len(boxes)):
                if used[j]:
                    continue
                b = boxes[j]
                # расширяем a на gap для проверки близости
                if not (a.x + a.w + gap < b.x or b.x + b.w + gap < a.x or
                        a.y + a.h + gap < b.y or b.y + b.h + gap < a.y):
                    grp.append(b)
                    used[j] = True
                    changed = True
            if len(grp) == 1:
                result.append(a)
            else:
                x1 = min(r.x for r in grp)
                y1 = min(r.y for r in grp)
                x2 = max(r.x + r.w for r in grp)
                y2 = max(r.y + r.h for r in grp)
                result.append(ROI(x1, y1, x2 - x1, y2 - y1))
        boxes = result
    return boxes


# opencv-contrib сaliency module (pip install opencv-contrib-python)
try:
    cv2.saliency.StaticSaliencySpectralResidual_create()
    _HAS_SALIENCY = True
except AttributeError:
    _HAS_SALIENCY = False


# ──────────────────────────────────────────────────────────────────────────────
# Лёгкие препроцессоры (работают на PREPROC_W×PREPROC_H)
# ──────────────────────────────────────────────────────────────────────────────

class FrameDiff:
    """Разность кадров. Самый дешёвый способ найти движение."""
    name  = "Frame Diff"
    color = (80, 220, 255)   # цвет заголовка панели

    def __init__(self, threshold: int = 20, min_area: int = 150, pad: int = 6):
        self._prev   = None
        self.thr     = threshold
        self.min_area = min_area
        self.pad     = pad

    def extract(self, small: np.ndarray) -> List[ROI]:
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self._prev is None:
            self._prev = gray
            return []
        diff = cv2.absdiff(self._prev, gray)
        self._prev = gray
        _, mask = cv2.threshold(diff, self.thr, 255, cv2.THRESH_BINARY)
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.dilate(mask, k, iterations=3)
        return _mask_to_rois(mask, self.min_area, self.pad)


class MOG2Sub:
    """
    Вычитание фона MOG2 с детектором движения камеры.

    Если камера движется (весь кадр меняется) — MOG2 выдаёт ROI = 100%.
    Детектор сравнивает среднюю яркость diff по всему кадру.
    Если глобальный diff > cam_motion_thr → камера движется → MOG2 паузируется,
    возвращается [] (пропускаем инференс), счётчик адаптации сбрасывается.
    """
    name  = "MOG2 BgSub"
    color = (100, 255, 180)

    def __init__(self, threshold: int = 40, min_area: int = 200, pad: int = 8,
                 cam_motion_thr: float = 18.0):
        """
        cam_motion_thr — средний absdiff по всему кадру при котором считаем
                         что камера движется (не объект). Подбирается под
                         скорость камеры. 18 = умеренное движение.
        """
        self.mog = cv2.createBackgroundSubtractorMOG2(
            history=100, varThreshold=threshold, detectShadows=False)
        self.min_area      = min_area
        self.pad           = pad
        self.cam_thr       = cam_motion_thr
        self._prev_gray    = None
        self._last_mask    = None   # для визуализации
        self.status        = "learning background..."
        self._frame_n      = 0
        self._cam_moving   = False

    def _is_camera_moving(self, gray: np.ndarray) -> bool:
        """True если глобальный сдвиг кадра говорит о движении камеры."""
        if self._prev_gray is None:
            self._prev_gray = gray
            return False
        diff = cv2.absdiff(gray, self._prev_gray)
        self._prev_gray = gray
        mean_diff = float(diff.mean())
        return mean_diff > self.cam_thr

    def get_mask_viz(self, frame: np.ndarray) -> np.ndarray:
        """Цветная визуализация маски MOG2 поверх кадра."""
        if self._last_mask is None:
            return frame.copy()
        h, w = frame.shape[:2]
        mask = cv2.resize(self._last_mask, (w, h))
        viz = frame.copy()
        # зелёный оверлей на активные пиксели маски
        green = np.zeros_like(frame)
        green[:, :, 1] = mask
        viz = cv2.addWeighted(viz, 0.5, green, 0.6, 0)
        if self._cam_moving:
            cv2.putText(viz, "CAMERA MOVING — paused",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 60, 255), 2)
        return viz

    def extract(self, small: np.ndarray) -> List[ROI]:
        self._frame_n += 1
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        # Детектор движения камеры
        self._cam_moving = self._is_camera_moving(gray)
        if self._cam_moving:
            self.status = f"cam moving — paused (frame {self._frame_n})"
            self._last_mask = np.zeros(small.shape[:2], np.uint8)
            return []

        mask = self.mog.apply(small)
        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.dilate(mask, k, iterations=2)
        self._last_mask = mask

        rois = _mask_to_rois(mask, self.min_area, self.pad)
        n    = len(rois)
        ratio = sum(r.area for r in rois) / (small.shape[0]*small.shape[1]) if rois else 0
        if self._frame_n < 30:
            self.status = f"learning bg... ({self._frame_n}/150)"
        else:
            self.status = f"tracking  ROIs={n}  coverage={ratio*100:.0f}%"
        return rois


class GMCMog2:
    """
    Global Motion Compensation + MOG2.

    Алгоритм:
      1. Lucas-Kanade отслеживает угловые точки между prev и curr
      2. RANSAC вычисляет гомографию движения камеры (H: curr→prev)
      3. Текущий кадр стабилизируется: warpPerspective(small, H_inv)
      4. MOG2 работает на стабилизированной последовательности —
         для него камера «стоит», видны только объекты что движутся иначе

    Находит объекты при движущейся камере (пан/тилт).
    На однотонном фоне (небо, стена) — LK не находит точек → fallback без GMC.
    """
    name  = "GMC + MOG2"
    color = (255, 200, 80)

    def __init__(self, threshold: int = 40, min_area: int = 200,
                 pad: int = 8, max_features: int = 150):
        self.mog = cv2.createBackgroundSubtractorMOG2(
            history=100, varThreshold=threshold, detectShadows=False)
        self.min_area     = min_area
        self.pad          = pad
        self.max_features = max_features
        self._prev_gray   = None
        self._last_mask   = None
        self.status       = "learning background..."
        self._frame_n     = 0

    def get_mask_viz(self, frame: np.ndarray) -> np.ndarray:
        if self._last_mask is None:
            return frame.copy()
        h, w = frame.shape[:2]
        mask = cv2.resize(self._last_mask, (w, h))
        viz  = frame.copy()
        green = np.zeros_like(frame)
        green[:, :, 1] = mask
        return cv2.addWeighted(viz, 0.5, green, 0.6, 0)

    def extract(self, small: np.ndarray) -> List[ROI]:
        self._frame_n += 1
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        stabilized = small   # fallback без компенсации
        gmc_ok     = False

        if self._prev_gray is not None:
            corners = cv2.goodFeaturesToTrack(
                self._prev_gray, self.max_features, 0.01, 7, blockSize=7)

            if corners is not None and len(corners) >= 8:
                nxt, st, _ = cv2.calcOpticalFlowPyrLK(
                    self._prev_gray, gray, corners, None,
                    winSize=(15, 15), maxLevel=2)
                gp = corners[st.ravel() == 1]
                gc = nxt[st.ravel() == 1]

                if len(gp) >= 6:
                    # H_inv: curr → prev (стабилизируем curr к prev)
                    H_inv, _ = cv2.findHomography(gc, gp, cv2.RANSAC, 3.0)
                    if H_inv is not None:
                        h, w = small.shape[:2]
                        stabilized = cv2.warpPerspective(small, H_inv, (w, h))
                        gmc_ok = True

        self._prev_gray = gray

        mask = self.mog.apply(stabilized)
        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.dilate(mask, k, iterations=2)
        self._last_mask = mask

        rois  = _mask_to_rois(mask, self.min_area, self.pad)
        n     = len(rois)
        ratio = sum(r.area for r in rois) / (small.shape[0] * small.shape[1]) if rois else 0
        gmc_s = "GMC✓" if gmc_ok else "GMC✗ fallback"
        self.status = (f"learning ({self._frame_n}/100)"
                       if self._frame_n < 30
                       else f"{gmc_s}  ROIs={n}  cov={ratio*100:.0f}%")
        return rois


class SaliencyPrep:
    """
    Spectral Residual Saliency (FFT-based).

    Не зависит от движения — находит «визуально необычные» зоны.
    Работает для статичных объектов и при движущейся камере.

    Требует: pip install opencv-contrib-python
    Fallback: Laplacian-based saliency если contrib недоступен.
    """
    name        = "Saliency " + ("SpRes" if _HAS_SALIENCY else "Laplacian")
    color       = (80, 180, 255)
    MAX_COVERAGE = 0.65

    def __init__(self, min_area: int = 200, pad: int = 8, top_pct: float = 85.0):
        self._sal     = (cv2.saliency.StaticSaliencySpectralResidual_create()
                         if _HAS_SALIENCY else None)
        self.min_area = min_area
        self.pad      = pad
        self.top_pct  = top_pct
        self._last_map: np.ndarray | None = None
        self.status   = "spectral residual" if _HAS_SALIENCY else "laplacian fallback"

    def get_mask_viz(self, frame: np.ndarray) -> np.ndarray:
        if self._last_map is None:
            return frame.copy()
        h, w    = frame.shape[:2]
        smap    = cv2.resize(self._last_map, (w, h))
        colored = cv2.applyColorMap(smap, cv2.COLORMAP_HOT)
        return cv2.addWeighted(frame, 0.4, colored, 0.6, 0)

    def extract(self, small: np.ndarray) -> List[ROI]:
        if self._sal is not None:
            ok, smap = self._sal.computeSaliency(small)
            if not ok:
                return []
            smap = (smap * 255).astype(np.uint8)
        else:
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            lap  = cv2.Laplacian(gray, cv2.CV_32F)
            smap = cv2.normalize(np.abs(lap), None, 0, 255,
                                 cv2.NORM_MINMAX, cv2.CV_8U)

        smap = cv2.GaussianBlur(smap, (5, 5), 0)
        self._last_map = smap

        thr = max(float(np.percentile(smap, self.top_pct)), 10)
        _, mask = cv2.threshold(smap, thr, 255, cv2.THRESH_BINARY)
        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

        rois = _mask_to_rois(mask, self.min_area, self.pad)
        rois.sort(key=lambda r: r.area, reverse=True)
        rois = rois[:8]

        n     = len(rois)
        ratio = sum(r.area for r in rois) / (small.shape[0] * small.shape[1]) if rois else 0
        self.status = f"ROIs={n}  cov={ratio*100:.0f}%"
        return _filter_coverage(rois, small.shape, self.MAX_COVERAGE)


class CombinedPrep:
    """
    GMC+MOG2 ∪ Saliency — объединяет оба препроцессора.

    GMC+MOG2  → находит объекты что движутся иначе чем камера
    Saliency  → находит визуально выделяющиеся статичные объекты
    Union ROI → YOLO получает максимальное покрытие при минимальных пропусках
    """
    name  = "Combined (GMC+Sal)"
    color = (255, 255, 100)

    def __init__(self, gmc: "GMCMog2", sal: "SaliencyPrep"):
        self._gmc = gmc
        self._sal = sal
        self.status = ""

    def get_mask_viz(self, frame: np.ndarray) -> np.ndarray:
        viz = frame.copy()
        if self._gmc._last_mask is not None:
            h, w  = frame.shape[:2]
            mask  = cv2.resize(self._gmc._last_mask, (w, h))
            green = np.zeros_like(viz)
            green[:, :, 1] = mask
            viz = cv2.addWeighted(viz, 0.6, green, 0.4, 0)
        if self._sal._last_map is not None:
            h, w    = frame.shape[:2]
            smap    = cv2.resize(self._sal._last_map, (w, h))
            colored = cv2.applyColorMap(smap, cv2.COLORMAP_HOT)
            viz = cv2.addWeighted(viz, 0.7, colored, 0.3, 0)
        return viz

    def extract(self, small: np.ndarray) -> List[ROI]:
        rois_g = self._gmc.extract(small)
        rois_s = self._sal.extract(small)
        merged = merge_rois(rois_g + rois_s, gap=10)
        self.status = f"GMC={len(rois_g)} Sal={len(rois_s)} → {len(merged)}"
        return merged


class HSVFilter:
    """
    Адаптивный цветовой фильтр HSV.
    'dark'  — объекты значительно темнее медианы сцены (относительный порог).
    'warm'  — красно-оранжевые пятна с высокой насыщенностью.
    Не срабатывает если найденная область > MAX_COVERAGE кадра.
    """
    color = (255, 180, 80)
    MAX_COVERAGE = 0.75   # если ROI > 75% кадра — считаем фильтр неэффективным

    def __init__(self, preset: str = "dark", min_area: int = 200,
                 pad: int = 8, dark_rel: float = 0.55):
        """
        dark_rel — пиксель считается «тёмным» если его V < median_V * dark_rel.
                   Снизить → строже (меньше ROI). Поднять → мягче.
        """
        self.preset   = preset
        self.min_area = min_area
        self.pad      = pad
        self.dark_rel = dark_rel
        self.name     = {"dark": "HSV Dark (adaptive)", "warm": "HSV Warm"}[preset]

    def extract(self, small: np.ndarray) -> List[ROI]:
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        h_ch, s_ch, v_ch = cv2.split(hsv)

        if self.preset == "dark":
            # Относительный порог: темнее median * dark_rel
            median_v = float(np.median(v_ch))
            thr_v    = max(20, int(median_v * self.dark_rel))
            mask     = cv2.inRange(v_ch, 0, thr_v)
        else:  # warm — абсолютный, т.к. ищем конкретный цвет
            mask = cv2.inRange(hsv, np.array([0, 80, 80]),  np.array([22, 255, 255]))
            mask |= cv2.inRange(hsv, np.array([158, 80, 80]), np.array([180, 255, 255]))
            # фильтруем малонасыщенные пиксели (серые стены не попадают)
            mask &= cv2.inRange(s_ch, 80, 255)

        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        mask = cv2.dilate(mask, k, iterations=2)

        rois = _mask_to_rois(mask, self.min_area, self.pad)
        return _filter_coverage(rois, small.shape, self.MAX_COVERAGE)


class EdgeBlocks:
    """
    Блочная плотность рёбер Canny.
    Адаптивный порог: выбирает только блоки из топ-N% по плотности рёбер.
    Не даёт выбрать весь кадр — всегда строго лучшие зоны.
    """
    name  = "Edge Blocks"
    color = (180, 130, 255)
    MAX_COVERAGE = 0.50

    def __init__(self, block: int = 16, top_percentile: float = 85.0,
                 min_blocks: int = 2, min_area: int = 80, t1: int = 25, t2: int = 70):
        """
        top_percentile — порог: берём блоки с плотностью > percentile(all_densities, top_percentile).
                         85 → только топ 15% самых насыщенных краями блоков.
                         Поднять → строже. Снизить → мягче.
        min_blocks     — не выбирать ничего если блоков найдено меньше.
        """
        self.block    = block
        self.top_pct  = top_percentile
        self.min_blk  = min_blocks
        self.min_area = min_area
        self.t1, self.t2 = t1, t2

    def extract(self, small: np.ndarray) -> List[ROI]:
        gray  = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), self.t1, self.t2)
        h, w  = edges.shape
        b     = self.block

        # Считаем плотность для каждого блока
        densities = []
        coords    = []
        for by in range(0, h, b):
            for bx in range(0, w, b):
                d = edges[by:by+b, bx:bx+b].mean() / 255.0
                densities.append(d)
                coords.append((bx, by))

        if not densities:
            return []

        # Адаптивный порог = percentile по реальным значениям кадра
        thr = float(np.percentile(densities, self.top_pct))

        # Отбираем блоки выше порога (но только если есть реальные края)
        selected = [(bx, by) for (bx, by), d in zip(coords, densities)
                    if d > thr and d > 0.02]   # 0.02 — минимум "хоть что-то есть"

        if len(selected) < self.min_blk:
            return []

        mask = np.zeros((h, w), np.uint8)
        for bx, by in selected:
            mask[by:by+b, bx:bx+b] = 255

        # Без дилатации — блоки уже достаточно крупные сами по себе
        rois = _mask_to_rois(mask, self.min_area, 4)
        # Ограничиваем количество зон: берём топ-6 по площади
        rois.sort(key=lambda r: r.area, reverse=True)
        return rois[:6]


class AdaptiveHSVPreprocessor:
    """
    Самообучающийся препроцессор: YOLO → HSV.

    Фаза 1 (калибровка, каждые CALIB_EVERY кадров):
        Запускает YOLO на крошечном кадре (160×120).
        Из найденных bbox семплирует HSV-пиксели объектов.
        Строит диапазон [mean - K*std, mean + K*std] по каждому каналу.

    Фаза 2 (фильтрация, все остальные кадры):
        Использует выученный HSV-диапазон как быстрый фильтр.
        YOLO получает только вырезки из оригинального разрешения.

    Результат: вместо 280ms на полный кадр — 1ms HSV-маска + YOLO только на ROI.
    """
    name  = "HSV Adaptive (YOLO→HSV)"
    color = (200, 80, 255)
    MAX_COVERAGE = 0.80
    CALIB_W, CALIB_H = 160, 120  # разрешение для калибровочного YOLO прогона
    K_STD = 2.2                   # диапазон: mean ± K*std (шире = меньше miss)

    def __init__(self, yolo_fn: Callable, calib_every: int = 40):
        self._yolo       = yolo_fn
        self._calib_every = calib_every
        self._ranges     = None     # tuple(lo np.array, hi np.array) или None
        self._frame_n    = 0
        self.status      = "ожидание первой калибровки..."
        self._last_labels: List[str] = []

    def _calibrate(self, small: np.ndarray) -> None:
        """Запускает YOLO на small-кадре, извлекает HSV из найденных объектов."""
        # Используем small напрямую — он уже 320×240, достаточно мал для скорости
        dets = self._yolo(small)
        if not dets:
            self.status = "калибровка: объектов не найдено (жду...)"
            if self._frame_n <= 5:
                print(f"[AdaptiveHSV] frame {self._frame_n}: YOLO на 320x240 — ничего не найдено")
            return

        # d["x1..y2"] уже в координатах small — используем напрямую
        H, W = small.shape[:2]
        hsv_small = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)

        # Сэмплируем только центральные 50% каждого bbox:
        # убирает фон и соседние объекты по краям рамки
        pixels: List[np.ndarray] = []
        for d in dets:
            x1, y1 = max(0, int(d["x1"])), max(0, int(d["y1"]))
            x2, y2 = min(W, int(d["x2"])), min(H, int(d["y2"]))
            bw, bh = x2 - x1, y2 - y1
            if bw < 8 or bh < 8:
                continue
            # центральные 50% по каждой оси
            cx1 = x1 + bw // 4;  cx2 = x2 - bw // 4
            cy1 = y1 + bh // 4;  cy2 = y2 - bh // 4
            patch = hsv_small[cy1:cy2, cx1:cx2]
            if patch.size > 0:
                pixels.append(patch.reshape(-1, 3))

        if not pixels:
            return

        all_px = np.vstack(pixels).astype(np.float32)
        lo = np.zeros(3, np.uint8)
        hi = np.zeros(3, np.uint8)
        limits = [180, 255, 255]

        # Перцентильный диапазон устойчивее к выбросам, чем mean±std
        # 10й–90й перцентиль — берём "ядро" цвета объекта
        for ch in range(3):
            vals = all_px[:, ch]
            lo[ch] = max(0,          int(np.percentile(vals, 10)))
            hi[ch] = min(limits[ch], int(np.percentile(vals, 90)))

        # Проверка: если диапазон слишком широкий — объект разноцветный,
        # HSV-фильтр бесполезен для него
        h_span = int(hi[0]) - int(lo[0])
        s_span = int(hi[1]) - int(lo[1])
        if h_span > 100 and s_span > 150:
            labels = list({d.get("label", "?") for d in dets})
            self.status = (f"[{', '.join(labels)}] цвет слишком разнородный "
                           f"(H-span={h_span}, S-span={s_span}) — HSV неприменим")
            print(f"[AdaptiveHSV] диапазон слишком широк: H:{lo[0]}-{hi[0]} "
                  f"S:{lo[1]}-{hi[1]} — калибровка сброшена")
            self._ranges = None
            return

        # Минимальная ширина чтобы не пропустить мелкие вариации цвета
        for ch, min_w in enumerate([10, 30, 40]):
            span = int(hi[ch]) - int(lo[ch])
            if span < min_w:
                center = (int(lo[ch]) + int(hi[ch])) // 2
                lo[ch] = max(0,          center - min_w // 2)
                hi[ch] = min(limits[ch], center + min_w // 2)

        self._ranges      = (lo, hi)
        self._last_labels = list({d.get("label", "?") for d in dets})
        self.status = (f"[{', '.join(self._last_labels)}]  "
                       f"H:{lo[0]}-{hi[0]}  S:{lo[1]}-{hi[1]}  V:{lo[2]}-{hi[2]}")
        print(f"[AdaptiveHSV] calibrated: {self.status}")

    def extract(self, small: np.ndarray) -> List[ROI]:
        self._frame_n += 1

        # Пока не откалиброваны — пробуем каждый кадр.
        # После первой успешной калибровки — повторяем каждые calib_every кадров.
        if self._ranges is None or self._frame_n % self._calib_every == 0:
            self._calibrate(small)

        if self._ranges is None:
            return []

        lo, hi = self._ranges
        hsv  = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)

        # Если Hue-диапазон переходит через 0/180 — два inRange
        if lo[0] > hi[0]:
            m1 = cv2.inRange(hsv, np.array([0,     lo[1], lo[2]]), np.array([hi[0], hi[1], hi[2]]))
            m2 = cv2.inRange(hsv, np.array([lo[0], lo[1], lo[2]]), np.array([180,   hi[1], hi[2]]))
            mask = cv2.bitwise_or(m1, m2)
        else:
            mask = cv2.inRange(hsv, lo, hi)

        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        mask = cv2.dilate(mask, k, iterations=2)

        rois = _mask_to_rois(mask, 200, 8)
        return _filter_coverage(rois, small.shape, self.MAX_COVERAGE)


def _filter_coverage(rois: List[ROI], shape: tuple, max_ratio: float) -> List[ROI]:
    """Если суммарное покрытие ROI > max_ratio кадра — возвращаем пустой список.
    Это значит препроцессор не нашёл ничего конкретного — весь кадр 'интересный'."""
    if not rois:
        return []
    total_px = shape[0] * shape[1]
    roi_px   = sum(r.area for r in rois)
    if roi_px / total_px > max_ratio:
        return []
    return rois


def _mask_to_rois(mask: np.ndarray, min_area: int, pad: int) -> List[ROI]:
    h, w = mask.shape[:2]
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rois = []
    for cnt in contours:
        if cv2.contourArea(cnt) < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        roi = ROI(max(0, x-pad), max(0, y-pad),
                  min(w - max(0, x-pad), bw + 2*pad),
                  min(h - max(0, y-pad), bh + 2*pad))
        rois.append(roi)
    return merge_rois(rois, gap=pad*2)


# ──────────────────────────────────────────────────────────────────────────────
# YOLO (реальный или имитация)
# ──────────────────────────────────────────────────────────────────────────────

class FakeYOLO:
    """Имитирует задержку YOLOv8n пропорционально площади входа."""
    def __call__(self, img: np.ndarray) -> List[dict]:
        h, w = img.shape[:2]
        delay = (YOLO_REF_MS / 1000) * (h * w / YOLO_REF_PX) * random.uniform(0.9, 1.1)
        time.sleep(delay)
        dets = []
        if random.random() < 0.25:
            for _ in range(random.randint(1, 2)):
                bx = random.randint(0, max(1, w - 40))
                by = random.randint(0, max(1, h - 40))
                bw = random.randint(25, min(w - bx, 100))
                bh = random.randint(25, min(h - by, 100))
                dets.append({"x1": bx, "y1": by, "x2": bx+bw, "y2": by+bh,
                             "conf": random.uniform(0.45, 0.95), "label": "obj"})
        return dets


def make_yolo(use_real: bool = False) -> Callable:
    if not use_real:
        print("FakeYOLO: симуляция тайминга Pi3B без загрузки модели (~5 МБ RAM).")
        print("  Добавь --real-yolo для реальной детекции (потребует ~200+ МБ).")
        return FakeYOLO()

    try:
        import torch
        torch.set_grad_enabled(False)          # не хранить градиенты — экономим ~30%
        torch.set_num_threads(2)               # не захватывать все ядра
        from ultralytics import YOLO as _Y
        model = _Y("yolov8n.pt")
        model.fuse()                           # merge BN-слои → меньше операций и RAM
        print("YOLOv8n: реальная модель (torch.no_grad, fused)")
        def _predict(img: np.ndarray) -> List[dict]:
            res = model(img, verbose=False)[0]
            out = []
            for b in res.boxes:
                x1, y1, x2, y2 = map(int, b.xyxy[0].tolist())
                out.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                            "conf": float(b.conf[0]),
                            "label": model.names.get(int(b.cls[0]), "?")})
            return out
        return _predict
    except ImportError:
        print("ultralytics не установлен — FakeYOLO")
        return FakeYOLO()


# ──────────────────────────────────────────────────────────────────────────────
# Async YOLO Worker — инференс в отдельном потоке
# ──────────────────────────────────────────────────────────────────────────────

class YOLOWorker:
    """
    YOLO работает в отдельном потоке. Главный цикл не блокируется.

    submit() кладёт пачку кропов в очередь (maxsize=1 — старые отбрасываются).
    get_latest() возвращает последние детекции мгновенно, без ожидания.

    Результат: главный цикл идёт со скоростью MOG2 (~30+ FPS),
    YOLO работает в своём темпе (~5–15 FPS), боксы обновляются асинхронно.
    """

    def __init__(self, yolo_fn: Callable):
        self._yolo    = yolo_fn
        self._q: _queue.Queue = _queue.Queue(maxsize=1)
        self._dets:   List[dict] = []
        self._inf_ms: float = 0.0
        self._yolo_fps_times: deque = deque(maxlen=30)
        self._lock    = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, crops_data: list, do_throttle: bool) -> None:
        """crops_data: list of (crop_img, roi, scale_x, scale_y)."""
        # выбрасываем устаревший кадр если поток занят
        try:
            self._q.get_nowait()
        except _queue.Empty:
            pass
        try:
            self._q.put_nowait((crops_data, do_throttle))
        except _queue.Full:
            pass

    def get_latest(self) -> Tuple[List[dict], float, float]:
        """Возвращает (dets, inf_ms, yolo_fps) — не блокирует."""
        with self._lock:
            yolo_fps = (len(self._yolo_fps_times) /
                        (self._yolo_fps_times[-1] - self._yolo_fps_times[0])
                        if len(self._yolo_fps_times) > 1 else 0.0)
            return list(self._dets), self._inf_ms, yolo_fps

    def _run(self) -> None:
        while True:
            crops_data, do_throttle = self._q.get()
            t0 = time.perf_counter()
            all_dets = []
            for crop, roi, scale_x, scale_y in crops_data:
                t_inf = time.perf_counter()
                dets  = self._yolo(crop)
                if do_throttle:
                    throttle(t_inf)
                for d in dets:
                    all_dets.append({**d,
                        "ax1": roi.x + int(d["x1"] * scale_x),
                        "ay1": roi.y + int(d["y1"] * scale_y),
                        "ax2": roi.x + int(d["x2"] * scale_x),
                        "ay2": roi.y + int(d["y2"] * scale_y)})
            inf_ms = (time.perf_counter() - t0) * 1000
            with self._lock:
                self._dets   = all_dets
                self._inf_ms = inf_ms
                self._yolo_fps_times.append(time.perf_counter())


# ──────────────────────────────────────────────────────────────────────────────
# Stats на один препроцессор
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PStats:
    pre_ms:   deque = field(default_factory=lambda: deque(maxlen=30))
    inf_ms:   deque = field(default_factory=lambda: deque(maxlen=30))
    roi_ratio: deque = field(default_factory=lambda: deque(maxlen=30))
    fps:      deque = field(default_factory=lambda: deque(maxlen=30))
    n_det:    int = 0
    n_frames: int = 0

    @property
    def avg_pre(self):   return float(np.mean(self.pre_ms))   if self.pre_ms   else 0
    @property
    def avg_inf(self):   return float(np.mean(self.inf_ms))   if self.inf_ms   else 0
    @property
    def avg_fps(self):   return float(np.mean(self.fps))      if self.fps      else 0
    @property
    def avg_ratio(self): return float(np.mean(self.roi_ratio)) if self.roi_ratio else 1.0


# ──────────────────────────────────────────────────────────────────────────────
# Обработка одного препроцессора
# ──────────────────────────────────────────────────────────────────────────────

def process_one(frame_orig: np.ndarray,
                prep,
                yolo_worker: "YOLOWorker",
                stats: PStats,
                do_throttle: bool) -> np.ndarray:
    """
    Возвращает панель: чёрный фон + только ROI-вырезки из оригинала + боксы YOLO.
    Препроцессор работает синхронно (быстро).
    YOLO работает асинхронно в YOLOWorker — главный цикл не ждёт.
    """
    H, W = frame_orig.shape[:2]

    # ── Шаг 1: уменьшаем до PREPROC_W×PREPROC_H
    t0    = time.perf_counter()
    small = cv2.resize(frame_orig, (PREPROC_W, PREPROC_H))

    # ── Шаг 2: препроцессор на маленьком кадре (синхронно, быстро)
    rois_small = prep.extract(small)
    pre_ms = (time.perf_counter() - t0) * 1000
    if do_throttle:
        throttle(t0)

    # ── Шаг 3: масштабируем ROI обратно в оригинальное разрешение
    sx = W / PREPROC_W
    sy = H / PREPROC_H
    rois_full = [r.scale(sx, sy).clamp(W, H) for r in rois_small]
    rois_full  = merge_rois(rois_full, gap=int(10 * sx))

    full_px = W * H
    roi_px  = sum(r.area for r in rois_full)
    ratio   = roi_px / full_px if full_px > 0 else 1.0

    # ── Шаг 4: панель = чёрный фон, вставляем только ROI из оригинала
    panel = np.zeros_like(frame_orig)
    for roi in rois_full:
        crop = roi.crop(frame_orig)
        panel[roi.y:roi.y + roi.h, roi.x:roi.x + roi.w] = crop

    # ── Шаг 5: подготавливаем кропы и отправляем в async YOLO worker
    if rois_full:
        crops_data = []
        for roi in rois_full:
            crop = roi.crop(frame_orig)
            if crop.size == 0:
                continue
            ch, cw = crop.shape[:2]
            if max(ch, cw) > MAX_CROP_PX:
                scale   = MAX_CROP_PX / max(ch, cw)
                crop    = cv2.resize(crop, (max(1, int(cw*scale)), max(1, int(ch*scale))))
                scale_x = cw / crop.shape[1]
                scale_y = ch / crop.shape[0]
            else:
                scale_x = scale_y = 1.0
            crops_data.append((crop.copy(), roi, scale_x, scale_y))
        if crops_data:
            yolo_worker.submit(crops_data, do_throttle)

    # ── Шаг 6: берём последние детекции из worker (не блокирует!)
    all_dets, inf_ms, _ = yolo_worker.get_latest()

    # Рисуем детекции на панели
    for d in all_dets:
        cv2.rectangle(panel, (d["ax1"], d["ay1"]), (d["ax2"], d["ay2"]),
                      (0, 255, 100), 2)
        cv2.putText(panel, f"{d['label']} {d['conf']:.2f}",
                    (d["ax1"], d["ay1"] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 100), 1, cv2.LINE_AA)

    # Тонкие рамки ROI (чтобы видеть границы вырезок)
    for roi in rois_full:
        cv2.rectangle(panel, (roi.x, roi.y),
                      (roi.x + roi.w, roi.y + roi.h), (60, 60, 60), 1)

    # ── Обновляем статистику
    stats.pre_ms.append(pre_ms)
    stats.inf_ms.append(inf_ms)
    stats.roi_ratio.append(ratio)
    stats.n_det    += len(all_dets)
    stats.n_frames += 1

    return panel


# ──────────────────────────────────────────────────────────────────────────────
# Рендер одной панели с заголовком и метриками
# ──────────────────────────────────────────────────────────────────────────────

LABEL_H = 52   # высота заголовка над панелью (3 строки)

def render_panel(content: np.ndarray,
                 prep,
                 stats: PStats,
                 do_throttle: bool) -> np.ndarray:
    """Масштабирует content до PANEL_W×PANEL_H, добавляет заголовок."""
    scaled = cv2.resize(content, (PANEL_W, PANEL_H))

    header = np.zeros((LABEL_H, PANEL_W, 3), np.uint8)

    # Строка 1: название препроцессора
    cv2.putText(header, prep.name,
                (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                prep.color, 1, cv2.LINE_AA)

    # Строка 2: метрики
    total_ms = stats.avg_pre + stats.avg_inf
    speedup  = YOLO_REF_MS / total_ms if total_ms > 0 else 0
    cv2.putText(header,
                f"fps={stats.avg_fps:.1f}  pre={stats.avg_pre:.0f}ms  "
                f"inf={stats.avg_inf:.0f}ms  roi={stats.avg_ratio*100:.0f}%  "
                f"spd={speedup:.1f}x  det={stats.n_det}",
                (6, 31), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                (190, 190, 190), 1, cv2.LINE_AA)

    # Строка 3: статус (для AdaptiveHSV — что нашли и какие диапазоны)
    status = getattr(prep, "status", "")
    if status:
        cv2.putText(header, status,
                    (6, 47), cv2.FONT_HERSHEY_SIMPLEX, 0.33,
                    (160, 120, 255), 1, cv2.LINE_AA)

    return np.vstack([header, scaled])


# ──────────────────────────────────────────────────────────────────────────────
# Нижняя полоса HUD
# ──────────────────────────────────────────────────────────────────────────────

HUD_H = 36

def render_hud(total_w: int, cpu: float, ram_mb: float, do_throttle: bool) -> np.ndarray:
    hud = np.zeros((HUD_H, total_w, 3), np.uint8)
    thr = "Pi3B THROTTLE ON" if do_throttle else "native PC speed"
    cv2.putText(hud, f"  [{thr}]  preproc @ {PREPROC_W}×{PREPROC_H} → crops @ original res   "
                f"CPU {cpu:.0f}%   RAM {ram_mb:.0f}/{PI3B_RAM_MB}MB   [T] throttle  [Q] quit",
                (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (80, 200, 255), 1, cv2.LINE_AA)
    return hud


# ──────────────────────────────────────────────────────────────────────────────
# Синтетический источник
# ──────────────────────────────────────────────────────────────────────────────

class SyntheticSource:
    def __init__(self, w=640, h=480):
        self.w, self.h = w, h
        self._bg = self._make_bg()
        self._objs = [self._new() for _ in range(5)]

    def _make_bg(self):
        bg = np.zeros((self.h, self.w, 3), np.uint8)
        for y in range(self.h):
            t = y / self.h
            bg[y] = [int(100*(1-t)+40*t), int(140*(1-t)+70*t), int(190*(1-t)+110*t)]
        return bg

    def _new(self):
        return {"x": random.uniform(0, self.w), "y": random.uniform(0, self.h*.85),
                "vx": random.uniform(-4, 4), "vy": random.uniform(-2, 2),
                "s": random.randint(18, 45),
                "c": tuple(random.randint(0, 45) for _ in range(3)),
                "shape": random.choice(["rect", "circle"])}

    def read(self):
        f = self._bg.copy()
        f = cv2.add(f, np.random.randint(0, 18, f.shape, np.uint8))
        for o in self._objs:
            o["x"] = (o["x"] + o["vx"]) % self.w
            o["y"] = (o["y"] + o["vy"]) % (self.h * .9)
            x, y, s = int(o["x"]), int(o["y"]), o["s"]
            if o["shape"] == "rect":
                cv2.rectangle(f, (x-s//2, y-s//3), (x+s//2, y+s//3), o["c"], -1)
            else:
                cv2.circle(f, (x, y), s//2, o["c"], -1)
        return True, f


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",      default="0")
    ap.add_argument("--demo",        action="store_true")
    ap.add_argument("--no-throttle", action="store_true")
    ap.add_argument("--real-yolo",   action="store_true",
                    help="Загрузить YOLOv8n (ultralytics/torch, ~200+ МБ RAM)")
    ap.add_argument("--scale",       type=float, default=1.0)
    args = ap.parse_args()

    do_throttle = not args.no_throttle

    # ── Источник
    if args.demo:
        cap = SyntheticSource(640, 480)
        print("Синтетика: движущиеся объекты")
    else:
        src = int(args.source) if args.source.isdigit() else args.source
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            print(f"Не удалось открыть: {args.source}")
            sys.exit(1)

    # ── YOLO (один async worker на всех — переключение без перезапуска)
    yolo        = make_yolo(use_real=args.real_yolo)
    yolo_worker = YOLOWorker(yolo)

    # ── Препроцессоры
    _gmc = GMCMog2(threshold=40, min_area=200, pad=8)
    _sal = SaliencyPrep(min_area=200, pad=8)
    preps = [
        MOG2Sub(threshold=40, min_area=200, pad=8, cam_motion_thr=18.0),  # [1]
        GMCMog2(threshold=40, min_area=200, pad=8),                        # [2]
        SaliencyPrep(min_area=200, pad=8),                                 # [3]
        CombinedPrep(_gmc, _sal),                                          # [4]
    ]
    pstats = [PStats() for _ in preps]
    active = 1   # по умолчанию GMC+MOG2

    proc  = psutil.Process()
    cpu_v = [0.0]
    def _cpu():
        while True: cpu_v[0] = psutil.cpu_percent(interval=0.5)
    threading.Thread(target=_cpu, daemon=True).start()

    WIN = ("Pi3B Sim  "
           "[1]MOG2(static cam)  [2]GMC+MOG2(moving cam)  "
           "[3]Saliency(static obj)  [4]Combined  "
           "[T]throttle  [Q]quit")

    saliency_note = ("  (opencv-contrib-python не установлен → Laplacian fallback)"
                     if not _HAS_SALIENCY else "")
    print(f"Препроцессоры: [1] {preps[0].name}  [2] {preps[1].name}  "
          f"[3] {preps[2].name}  [4] {preps[3].name}{saliency_note}")
    print(f"Throttle: {'ON' if do_throttle else 'OFF'}   "
          f"Preproc: {PREPROC_W}×{PREPROC_H}   YOLO: async thread\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            if args.demo:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        if args.scale != 1.0:
            frame = cv2.resize(frame,
                               (int(frame.shape[1] * args.scale),
                                int(frame.shape[0] * args.scale)))

        t0   = time.perf_counter()
        prep = preps[active]
        stat = pstats[active]

        det_panel = process_one(frame, prep, yolo_worker, stat, do_throttle)
        dt = time.perf_counter() - t0
        stat.fps.append(1.0 / dt if dt > 0 else 0)

        small    = cv2.resize(frame, (PREPROC_W, PREPROC_H))
        mask_viz = prep.get_mask_viz(small)
        mask_viz = cv2.resize(mask_viz, (frame.shape[1], frame.shape[0]))

        _, _inf, yolo_fps = yolo_worker.get_latest()
        total_ms = stat.avg_pre + stat.avg_inf
        speedup  = YOLO_REF_MS / total_ms if total_ms > 0 else 0

        # Заголовки панелей
        status_short = getattr(prep, "status", "")[:45]
        for img, label, col in [
            (frame,     "RAW",                               (200, 200, 200)),
            (mask_viz,  f"MASK  {status_short}",             (100, 255, 180)),
            (det_panel, f"DETECTION  [{prep.name}]",         (0,   255, 100)),
        ]:
            cv2.rectangle(img, (0, 0), (img.shape[1], 24), (0, 0, 0), -1)
            cv2.putText(img, label, (6, 17),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, col, 1, cv2.LINE_AA)

        # HUD
        ram     = proc.memory_info().rss / 1024 / 1024
        thr_str = "Pi3B THROTTLE ON" if do_throttle else "native PC speed"
        hud_txt = (f"  [{thr_str}]  [{prep.name}]  "
                   f"cap={stat.avg_fps:.1f}fps  yolo={yolo_fps:.1f}fps  "
                   f"pre={stat.avg_pre:.0f}ms  inf={stat.avg_inf:.0f}ms  "
                   f"roi={stat.avg_ratio*100:.0f}%  speedup={speedup:.1f}x  "
                   f"det={stat.n_det}  CPU={cpu_v[0]:.0f}%  "
                   f"RAM={ram:.0f}/{PI3B_RAM_MB}MB")

        view_row = np.hstack([frame, mask_viz, det_panel])
        hud      = np.zeros((36, view_row.shape[1], 3), np.uint8)
        cv2.putText(hud, hud_txt, (6, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (80, 200, 255), 1, cv2.LINE_AA)
        cv2.imshow(WIN, np.vstack([view_row, hud]))

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord('t'):
            do_throttle = not do_throttle
            print(f"Throttle: {'ON' if do_throttle else 'OFF'}")
        elif key == ord('1'):
            active = 0; print(f"→ [1] {preps[0].name}")
        elif key == ord('2'):
            active = 1; print(f"→ [2] {preps[1].name}")
        elif key == ord('3'):
            active = 2; print(f"→ [3] {preps[2].name}")
        elif key == ord('4'):
            active = 3; print(f"→ [4] {preps[3].name}")

    if not args.demo:
        cap.release()
    cv2.destroyAllWindows()

    print("\n=== Итог ===")
    for i, (p, s) in enumerate(zip(preps, pstats)):
        if s.n_frames == 0:
            continue
        total = s.avg_pre + s.avg_inf
        spd   = YOLO_REF_MS / total if total > 0 else 0
        print(f"  [{i+1}] {p.name}  fps={s.avg_fps:.1f}  "
              f"pre={s.avg_pre:.0f}ms  inf={s.avg_inf:.0f}ms  "
              f"roi={s.avg_ratio*100:.0f}%  speedup={spd:.2f}x  det={s.n_det}")


if __name__ == "__main__":
    main()
