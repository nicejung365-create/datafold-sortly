"""첫 화면 히어로 배경 영상(hero.mp4) 만들기

흩어진 데이터 점들이 세 무리(생애주기·분야·정보유형)로 모였다가 다시 흩어지는 모션 그래픽.
- 색: #3fb645 를 중심으로 밝은·어두운 초록, 바탕은 첫 화면 배경색 #242627
- 글자 없음 (위에 큰 제목과 버튼을 올림)
- 모든 움직임이 한 바퀴(0→1) 도는 주기 함수라서, 끝 장면이 처음 장면으로 그대로 이어져 반복해도 끊기지 않음

필요한 것: pip install imageio imageio-ffmpeg  (numpy·Pillow 는 이미 있음)
실행: python make_hero.py
"""
import math
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT_PATH = Path(__file__).parent / "hero.mp4"
WIDTH, HEIGHT = 1920, 1080
FPS = 30
SECONDS = 8
FRAMES = FPS * SECONDS

BG = (0x24, 0x26, 0x27)
GREEN = (0x3F, 0xB6, 0x45)       # 중심 색
GREEN_LIGHT = (0x8C, 0xE0, 0x90)
GREEN_DARK = (0x23, 0x7A, 0x2A)

POINTS = 120
LINK_DIST = 170  # 이 거리 안의 점끼리 선으로 이음
TAU = 2 * math.pi

rng = np.random.default_rng(7)

# 세 무리의 자리 (화면 가운데 줄을 피해 위·아래로 살짝 비켜, 제목 글자와 덜 겹치게 함)
CLUSTERS = np.array([
    [WIDTH * 0.22, HEIGHT * 0.70],
    [WIDTH * 0.50, HEIGHT * 0.28],
    [WIDTH * 0.78, HEIGHT * 0.70],
])

home = np.column_stack([rng.uniform(0, WIDTH, POINTS), rng.uniform(0, HEIGHT, POINTS)])
group = rng.integers(0, 3, POINTS)
# 무리 안에서의 자리: 무리 중심 둘레에 고르게 퍼짐
angle = rng.uniform(0, TAU, POINTS)
spread = rng.uniform(20, 150, POINTS)
target = CLUSTERS[group] + np.column_stack([np.cos(angle), np.sin(angle)]) * spread[:, None]
# 점마다 작은 원을 그리며 떠다님 — 한 영상 동안 정수 바퀴만 돌아야 처음과 끝이 맞음
orbit_r = rng.uniform(8, 30, POINTS)
orbit_k = rng.integers(1, 3, POINTS)
orbit_phase = rng.uniform(0, TAU, POINTS)
radius = rng.uniform(2.5, 7.0, POINTS)
color = np.array([GREEN, GREEN_LIGHT, GREEN_DARK])[rng.choice(3, POINTS, p=[0.55, 0.2, 0.25])]
twinkle_phase = rng.uniform(0, TAU, POINTS)

# 배경 은은한 초록 빛 번짐 두 개 (위치가 한 바퀴 돌아 제자리로 옴)
yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float32)


def background(t):
    img = np.empty((HEIGHT, WIDTH, 3), np.float32)
    img[:] = BG
    for cx, cy, rx, ry, size, strength, k in [
        (0.30, 0.40, 0.10, 0.08, 420, 0.12, 1),
        (0.72, 0.60, 0.08, 0.10, 480, 0.10, 1),
    ]:
        px = WIDTH * (cx + rx * math.cos(TAU * k * t))
        py = HEIGHT * (cy + ry * math.sin(TAU * k * t))
        glow = np.exp(-((xx - px) ** 2 + (yy - py) ** 2) / (2 * size ** 2)) * strength
        img += glow[..., None] * (np.array(GREEN, np.float32) - BG)
    return img


def positions(t):
    # 모이는 정도: t=0 과 t=1 에서 0(흩어짐), t=0.5 에서 1(세 무리) — 부드러운 곡선
    gather = (1 - math.cos(TAU * t)) / 2
    gather = gather * gather * (3 - 2 * gather)
    base = home * (1 - gather) + target * gather
    a = TAU * orbit_k * t + orbit_phase
    return base + np.column_stack([np.cos(a), np.sin(a)]) * orbit_r[:, None]


def frame(i):
    t = i / FRAMES
    pos = positions(t)

    lines = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw_lines = ImageDraw.Draw(lines)
    diff = pos[:, None, :] - pos[None, :, :]
    dist = np.sqrt((diff ** 2).sum(-1))
    for a, b in zip(*np.where(np.triu(dist < LINK_DIST, 1))):
        alpha = int(90 * (1 - dist[a, b] / LINK_DIST))
        draw_lines.line([tuple(pos[a]), tuple(pos[b])], fill=GREEN + (alpha,), width=1)

    dots = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw_dots = ImageDraw.Draw(dots)
    for (x, y), r, c, ph in zip(pos, radius, color, twinkle_phase):
        alpha = int(170 + 70 * math.sin(TAU * 2 * t + ph))  # 반짝임도 정수 바퀴
        draw_dots.ellipse([x - r, y - r, x + r, y + r], fill=tuple(int(v) for v in c) + (alpha,))
    glow = dots.filter(ImageFilter.GaussianBlur(10))

    img = Image.fromarray(np.clip(background(t), 0, 255).astype(np.uint8)).convert("RGBA")
    img = Image.alpha_composite(img, lines)
    img = Image.alpha_composite(img, glow)
    img = Image.alpha_composite(img, dots)
    return np.asarray(img.convert("RGB"))


def main():
    writer = imageio.get_writer(
        OUT_PATH, fps=FPS, codec="libx264", quality=8, pixelformat="yuv420p",
        macro_block_size=8, output_params=["-movflags", "+faststart"],
    )
    for i in range(FRAMES):
        writer.append_data(frame(i))
        if i % 30 == 0:
            print(f"{i}/{FRAMES} 장면")
    writer.close()
    print(f"저장: {OUT_PATH} ({OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
