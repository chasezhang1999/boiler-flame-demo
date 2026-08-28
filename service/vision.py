"""
图像处理：火焰分割、等值轮廓、相对亮温伪彩、量化指标。

这一层只跟像素打交道，不做任何判断也不碰 HTTP —— 判级归模型，
编排归 Dify，报告归 report.py。拆开是为了这部分能单独测：
给一张图，指标应该是确定的，跟模型和网络都无关。
"""

import cv2
import numpy as np


# ---------------------------------------------------------------- 图像处理

def decode(raw: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法解码图片，检查是不是 jpg/png")
    # 统一到长边 1280，太大的图后面算得慢，也没必要
    h, w = img.shape[:2]
    if max(h, w) > 1280:
        s = 1280 / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def flame_mask(bgr: np.ndarray):
    """
    返回 (full, main)：

      full —— 所有高亮区域。贴壁挂焦的亮斑是独立小连通域，必须留在这里，
              否则最该报警的信号会被当噪点扔掉。
      main —— 最大连通域，即主火焰，用来画轮廓和算火焰本体的形态指标。
    """
    v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 2]
    v = cv2.GaussianBlur(v, (7, 7), 0)
    _, full = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    full = cv2.morphologyEx(full, cv2.MORPH_CLOSE, k, iterations=2)
    full = cv2.morphologyEx(full, cv2.MORPH_OPEN, k, iterations=1)

    main = full.copy()
    n, labels, stats, _ = cv2.connectedComponentsWithStats(full, 8)
    if n > 1:
        biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        main = np.where(labels == biggest, 255, 0).astype(np.uint8)
    return full, main


def temp_index(bgr: np.ndarray) -> np.ndarray:
    """
    相对亮温指数，0~1。

    双色测温的简化版：火焰温度越高越偏白，绿/红通道比值越接近 1；
    温度越低越偏暗红，比值越小。未经黑体炉标定，只是相对量，不是摄氏度。
    """
    b, g, r = cv2.split(bgr.astype(np.float32))
    ratio = g / (r + 1.0)
    ratio = np.clip(ratio, 0.0, 1.2) / 1.2
    # 亮度做个加权，避免暗部噪声被算成高温
    v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 2].astype(np.float32) / 255.0
    return np.clip(ratio * 0.7 + v * 0.3, 0.0, 1.0)


def draw_contours(bgr: np.ndarray, temp: np.ndarray, mask: np.ndarray,
                   spots: list) -> np.ndarray:
    """外焰 / 中间层 / 焰心 三级等值轮廓，叠在原图上，另标出疑似贴壁亮斑。"""
    out = bgr.copy()
    inside = temp[mask > 0]
    if inside.size == 0:
        return out

    levels = [
        (np.percentile(inside, 40), (80, 220, 80), "OUTER"),
        (np.percentile(inside, 70), (60, 200, 255), "MID"),
        (np.percentile(inside, 92), (255, 255, 255), "CORE"),
    ]
    for thr, color, label in levels:
        layer = ((temp >= thr) & (mask > 0)).astype(np.uint8) * 255
        layer = cv2.morphologyEx(
            layer, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
        )
        cnts, _ = cv2.findContours(layer, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cnts = [c for c in cnts if cv2.contourArea(c) > 150]
        cv2.drawContours(out, cnts, -1, color, 2)
        if cnts:
            big = max(cnts, key=cv2.contourArea)
            x, y, w, _ = cv2.boundingRect(big)
            cv2.putText(out, label, (x, max(y - 6, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    # 火焰形心 + 画面中心，偏斜一眼可见
    m = cv2.moments(mask, binaryImage=True)
    if m["m00"] > 0:
        cx, cy = int(m["m10"] / m["m00"]), int(m["m01"] / m["m00"])
        h, w = bgr.shape[:2]
        cv2.drawMarker(out, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 26, 2)
        cv2.drawMarker(out, (w // 2, h // 2), (200, 200, 200), cv2.MARKER_TILTED_CROSS, 20, 1)
        cv2.line(out, (w // 2, h // 2), (cx, cy), (0, 0, 255), 1, cv2.LINE_AA)

    # 疑似贴壁 / 挂焦亮斑，品红框标出来
    for s in spots:
        x, y, bw, bh = s["bbox"]
        pad = 6
        cv2.rectangle(out, (x - pad, y - pad), (x + bw + pad, y + bh + pad), (255, 0, 255), 2)
        cv2.putText(out, "HOTSPOT", (max(x - pad, 2), max(y - pad - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1, cv2.LINE_AA)
    return out


def draw_heatmap(bgr: np.ndarray, temp: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    火焰区域上伪彩，区域外压暗保留背景轮廓，右侧加色标。

    用 INFERNO 不用 JET：JET 最高端映射到暗红，越热反而看着越暗，焰心会被误读成
    低温区；且色阶不均匀会造出并不存在的分层。INFERNO 感知均匀、越热越亮越白，
    跟人看火焰的直觉一致。
    """
    inside = temp[mask > 0]
    if inside.size:
        lo, hi = np.percentile(inside, 2), np.percentile(inside, 98)
    else:
        lo, hi = 0.0, 1.0
    norm = np.clip((temp - lo) / max(hi - lo, 1e-6), 0, 1)

    heat = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    dark = (bgr * 0.35).astype(np.uint8)
    m3 = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR) > 0
    out = np.where(m3, cv2.addWeighted(heat, 0.75, bgr, 0.25, 0), dark)

    # 色标
    h, w = out.shape[:2]
    bar_h, bar_w, pad = int(h * 0.45), 16, 18
    y0 = (h - bar_h) // 2
    grad = np.linspace(255, 0, bar_h).astype(np.uint8).reshape(-1, 1)
    bar = cv2.applyColorMap(np.repeat(grad, bar_w, axis=1), cv2.COLORMAP_INFERNO)
    out[y0:y0 + bar_h, w - pad - bar_w:w - pad] = bar
    cv2.rectangle(out, (w - pad - bar_w, y0), (w - pad, y0 + bar_h), (255, 255, 255), 1)
    cv2.putText(out, "HIGH", (w - pad - bar_w - 46, y0 + 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(out, "LOW", (w - pad - bar_w - 38, y0 + bar_h),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(out, "relative brightness temp (uncalibrated)", (12, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)
    return out


def wall_hotspots(temp: np.ndarray, full: np.ndarray, main: np.ndarray) -> list:
    """
    主火焰之外、且贴近画面边缘的独立高亮块 —— 疑似贴壁火焰或已挂焦发亮的位置。
    每项带 bbox 供画图用，写进 metrics 前会剥掉。
    """
    h, w = full.shape
    others = cv2.bitwise_and(full, cv2.bitwise_not(main))
    # 边缘带按各轴独立算：炉膛摄像头多是 16:9，左右两侧才是水冷壁，
    # 用 min(h,w) 会让横向带过窄，贴壁亮斑漏检。
    band_x, band_y = max(int(w * 0.15), 8), max(int(h * 0.15), 8)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(others, 8)

    spots = []
    for i in range(1, n):
        a = float(stats[i, cv2.CC_STAT_AREA])
        if a < max(h * w * 0.0004, 60):       # 太小的当噪点
            continue
        x0, y0 = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        bw, bh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        # 外接框只要伸进边缘带就算，比只看形心稳
        if not (x0 < band_x or x0 + bw > w - band_x or
                y0 < band_y or y0 + bh > h - band_y):
            continue
        cx, cy = cents[i]
        blob = (labels == i)
        if float(temp[blob].mean()) < 0.5:    # 不够亮的不算
            continue
        vpos = "上" if cy < h / 3 else ("下" if cy > h * 2 / 3 else "中")
        hpos = "左" if cx < w / 3 else ("右" if cx > w * 2 / 3 else "中")
        spots.append({
            "位置": f"{vpos}{hpos}",
            "面积占比%": round(a / (h * w) * 100, 2),
            "相对亮温": round(float(temp[blob].mean()), 3),
            "bbox": (x0, y0, bw, bh),
        })
    spots.sort(key=lambda s: -s["面积占比%"])
    return spots[:5]


def compute_metrics(bgr: np.ndarray, temp: np.ndarray, mask: np.ndarray,
             full: np.ndarray, spots: list) -> dict:
    """喂给大模型的量化特征。光让它看 800x800 缩略图判级不稳，配上数字稳得多。"""
    h, w = mask.shape
    total = float(h * w)
    area = float(np.count_nonzero(mask))
    if area == 0:
        return {"flame_detected": False}

    m = cv2.moments(mask, binaryImage=True)
    cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
    off_x = (cx - w / 2) / (w / 2) * 100
    off_y = (cy - h / 2) / (h / 2) * 100

    inside = temp[mask > 0]
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    big = max(cnts, key=cv2.contourArea)
    perim = cv2.arcLength(big, True)
    # 圆度：1 是正圆，越小说明火焰形状越破碎/拉长
    circularity = 4 * np.pi * cv2.contourArea(big) / max(perim ** 2, 1e-6)

    # 贴壁风险代理：全部高亮区域（含主火焰外的亮斑）里，落在画面边缘带的高温像素占比
    edge = np.zeros_like(mask)
    bx, by = max(int(w * 0.10), 6), max(int(h * 0.10), 6)
    edge[:by, :] = edge[-by:, :] = edge[:, :bx] = edge[:, -bx:] = 255
    thr = np.percentile(inside, 85)
    hot = ((temp > thr) & (full > 0)).astype(np.uint8) * 255
    edge_hot = float(np.count_nonzero(cv2.bitwise_and(hot, edge))) / max(float(np.count_nonzero(hot)), 1.0)

    clean_spots = [{k: v for k, v in s.items() if k != "bbox"} for s in spots]

    return {
        "flame_detected": True,
        "flame_fill_ratio_pct": round(area / total * 100, 1),
        "centroid_offset_x_pct": round(off_x, 1),
        "centroid_offset_y_pct": round(off_y, 1),
        "centroid_offset_total_pct": round(float(np.hypot(off_x, off_y)), 1),
        "temp_index_mean": round(float(inside.mean()), 3),
        "temp_index_p95": round(float(np.percentile(inside, 95)), 3),
        "high_temp_area_pct": round(float(np.count_nonzero(inside > 0.75)) / area * 100, 1),
        "contour_circularity": round(float(circularity), 3),
        "hot_pixels_near_edge_pct": round(edge_hot * 100, 1),
        "wall_hotspot_count": len(clean_spots),
        "wall_hotspot_area_pct": round(sum(s["面积占比%"] for s in clean_spots), 2),
        "wall_hotspots": clean_spots,
        "whiteness_mean": round(float(np.mean(bgr[mask > 0])), 1),
    }
