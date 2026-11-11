"""マニュアルの概念図 (インラインの SVG) を作って manual.html に差し込む。

manual.html の <figure class="fig" data-fig="名前">…</figure> の中身を、ここで作った SVG と説明文に置き換える。
図を直したら流し直す (何度流しても同じ結果になる)。

    ~/.venv/gpumd312/bin/python tools/figures.py

色は manual.html の CSS の変数 (--text・--muted・--fa〜--fd など) を使うので、ダークモードでも読める。
文字はスマホ幅で読めるよう、図の幅を 400 前後にして 12〜13 の大きさで書く。
"""
from __future__ import annotations

import html
import itertools
import math
import re
from pathlib import Path

import numpy as np

MANUAL = Path(__file__).resolve().parent.parent / "packages" / "matsimapp" / "src" / "matsimapp" / "docs" / "manual.html"
FIGS: dict[str, tuple[str, str]] = {}   # 名前 → (SVG, 説明文)


# ---- 部品 ---------------------------------------------------------------------------------

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def t(x, y, s, cls="", anchor="middle", size=None, weight=None, rotate=0) -> str:
    """文字。s に改行を入れると複数行。rotate は (x, y) を中心に回す角度 [度] (縦書きの軸の名前など)。"""
    lines = str(s).split("\n")
    attrs = f' class="{cls}"' if cls else ""
    if rotate:
        attrs += f' transform="rotate({rotate} {x:.1f} {y:.1f})"'
    style = []
    if size:
        style.append(f"font-size:{size}px")
    if weight:
        style.append(f"font-weight:{weight}")
    if style:
        attrs += f' style="{";".join(style)}"'
    if len(lines) == 1:
        return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"{attrs}>{esc(s)}</text>'
    step = (size or 13) * 1.3
    y0 = y - step * (len(lines) - 1) / 2
    spans = "".join(f'<tspan x="{x:.1f}" y="{y0 + i * step:.1f}">{esc(line)}</tspan>' for i, line in enumerate(lines))
    return f'<text text-anchor="{anchor}"{attrs}>{spans}</text>'


def box(x, y, w, h, label="", cls="bx", tcls="", size=None, rx=8) -> str:
    out = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" class="{cls}"/>'
    if label:
        out += t(x + w / 2, y + h / 2 + 4.5, label, tcls, size=size)
    return out


def line(x1, y1, x2, y2, cls="ln") -> str:
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" class="{cls}"/>'


def arrow(x1, y1, x2, y2, cls="ln", head=7) -> str:
    """矢印 (先の三角は線と同じ色の塗り)。"""
    ang = math.atan2(y2 - y1, x2 - x1)
    bx, by = x2 - head * math.cos(ang), y2 - head * math.sin(ang)
    left = (bx + head * 0.5 * math.sin(ang), by - head * 0.5 * math.cos(ang))
    right = (bx - head * 0.5 * math.sin(ang), by + head * 0.5 * math.cos(ang))
    fill = cls.replace("ln", "fl").replace("lm", "flm").split()[0]
    for c in ("la", "lb", "lc", "ld"):
        if c in cls.split():
            fill = "f" + c[1]
    return (line(x1, y1, bx, by, cls)
            + f'<polygon points="{x2:.1f},{y2:.1f} {left[0]:.1f},{left[1]:.1f} {right[0]:.1f},{right[1]:.1f}" class="{fill}"/>')


def darrow(x1, y1, x2, y2, cls="ln") -> str:
    """両向きの矢印 (寸法線)。"""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    return arrow(mx, my, x1, y1, cls) + arrow(mx, my, x2, y2, cls)


def poly(points, cls="ln", closed=False) -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<{"polygon" if closed else "polyline"} points="{pts}" class="{cls}"/>'


def circle(x, y, r, cls="atom") -> str:
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" class="{cls}"/>'


def svg(name: str, w: int, h: int, body: str, caption: str, label: str) -> None:
    FIGS[name] = (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">{body}</svg>', caption)


def curve_points(xs, ys, x0, y0, w, h, xr, yr):
    """データ座標 (xr, yr の範囲) を図の座標 (左上 x0, y0、幅 w、高さ h) に。"""
    px = x0 + (np.asarray(xs) - xr[0]) / (xr[1] - xr[0]) * w
    py = y0 + h - (np.asarray(ys) - yr[0]) / (yr[1] - yr[0]) * h
    return list(zip(px, py))


# ---- 1 章: 全体の流れ --------------------------------------------------------------------

def fig_flow():
    steps = [("構造を用意する", "ファイル・Materials Project・モデリング"),
             ("計算を選び、条件を決める", "どれを選ぶかは第 2 部"),
             ("確認して実行", "原子数・時間の目安・入力ファイル"),
             ("結果を見る", "グラフ・結果の要点・3D 表示"),
             ("解析する・次の計算へ", "ボロノイ・結晶構造の判定・最終構造を登録")]
    body = ""
    for i, (head, note) in enumerate(steps):
        y = 10 + i * 62
        body += box(20, y, 330, 46, cls="bx")
        body += t(36, y + 20, f"{i + 1}. {head}", anchor="start", weight=700)
        body += t(36, y + 38, note, "m", anchor="start", size=11.5)
        if i < len(steps) - 1:
            body += arrow(185, y + 46, 185, y + 62)
    body += f'<path d="M350,{10 + 4 * 62 + 23} C378,{10 + 4 * 62 + 23} 378,33 350,33" class="lm dash"/>'
    body += '<polygon points="350,33 358,29 358,37" class="flm"/>'
    body += t(398, 166, "く\nり\n返\nす", "m", size=11)
    svg("flow", 420, 320, body, "アプリでの計算の流れ。最終構造を登録すると、次の計算の出発点にできます。", "計算の流れ")


# ---- 4 章: 計算の選び方 -------------------------------------------------------------------

def fig_choose():
    body = box(110, 6, 180, 34, "何を知りたい?", cls="bx strong", tcls="b")
    cols = [("数値 (物性値)", "fa", ["静的計算", "熱膨張", "弾性 (有限温度)", "熱伝導・拡散", "フォノン"]),
            ("過程 (何が起こるか)", "fb", ["MD (昇温・アニール)", "+ 結晶構造の判定", "+ ボロノイ解析", "+ 原子分布"]),
            ("安定性・障壁", "fc", ["自由エネルギー", "融点 (固液共存)", "構造相転移", "PLUMED (障壁)"])]
    for i, (head, color, items) in enumerate(cols):
        x = 6 + i * 136
        body += arrow(200, 40, x + 62, 62)
        body += box(x, 64, 124, 32, cls=f"bx {color}-edge")
        body += t(x + 62, 85, head, size=12, weight=700)
        for k, item in enumerate(items):
            y = 104 + k * 30
            body += box(x + 6, y, 112, 24, item, cls="bx soft", size=11.5)
    body += t(200, 268, "迷ったら: 小さく短く流す → ポテンシャルを確かめる → 全体像 → 精密", "m", size=11.5)
    svg("choose", 420, 280, body, "知りたいことの種類で、向いている計算が変わります (詳しくは下の表)。", "計算の選び方")


# ---- 5 章: 計算の前に決めること ---------------------------------------------------------------

def fig_cutoff():
    body = ""
    for panel, (x0, size, ok) in enumerate(((10, 170, True), (215, 110, False))):
        y0 = 40
        # 周期的に並んだとなりのセル (薄く)
        cx0 = x0 + (0 if ok else 30)
        if not ok:   # となりのセル (周期的に並んだ同じセル) を右と上下に薄く。図の外は切る
            for gx, gy in ((size, 0), (0, -size), (0, size)):
                body += box(cx0 + gx, y0 + gy, size, size, cls="ghost", rx=0).replace(
                    "/>", ' clip-path="url(#cut-clip)"/>')
        body += box(cx0, y0, size, size, cls="bx", rx=0)
        n = 5 if ok else 3
        for i in range(n):
            for j in range(n):
                body += circle(cx0 + (i + 0.5) * size / n, y0 + (j + 0.5) * size / n, 4, "atom")
        cx, cy = cx0 + size / 2, y0 + size / 2
        rc = 70
        body += circle(cx, cy, 6, "atom fa")
        body += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rc}" class="lc dash" fill="none"/>'
        body += arrow(cx, cy, cx + rc * 0.7, cy - rc * 0.7, "lc")
        body += t(cx + 14, cy - 34, "rc", "c halo", size=12, weight=700)
        title = "OK: 厚み ≥ 2 × rc" if ok else "NG: セルが小さい"
        body += t(cx0 + size / 2, 24, title, "c" if ok else "b", weight=700)
    body += darrow(10, 222, 180, 222, "lm")
    body += t(95, 238, "セルの厚み", "m", size=11.5)
    body += t(300, 190, "円がとなりのセルに\nはみ出すと、同じ原子を\n2 回数えてしまう", "m", size=11)
    body = '<clipPath id="cut-clip"><rect x="0" y="34" width="420" height="180"/></clipPath>' + body
    svg("cutoff", 420, 250, body, "ポテンシャルは、各原子のまわり半径 rc (カットオフ) の原子から力を計算します。"
        "セルの厚みが 2 × rc より小さいと正しく計算できないので、スーパーセルにします (既定で 12 Å 以上)。", "セルとカットオフ")


def fig_timeline():
    x0, y0, w, h = 40, 20, 350, 130
    tt = np.linspace(0, 1, 300)
    rng = np.random.default_rng(3)
    target = 0.55
    temp = target + (0.05 - target) * np.exp(-tt / 0.08) + 0.035 * rng.standard_normal(len(tt)) * (1 - np.exp(-tt / 0.05))
    body = line(x0, y0 + h, x0 + w, y0 + h) + line(x0, y0, x0, y0 + h)
    body += f'<rect x="{x0 + w * 0.3:.1f}" y="{y0}" width="{w * 0.7:.1f}" height="{h}" class="band"/>'
    body += line(x0, y0 + h - target * h, x0 + w, y0 + h - target * h, "lb dash")
    body += t(x0 + w - 2, y0 + h - target * h - 22, "目標の温度 (点線)", "b halo", anchor="end", size=11)
    body += poly(curve_points(tt, temp, x0, y0, w, h, (0, 1), (0, 1)), "la")
    body += line(x0 + w * 0.3, y0, x0 + w * 0.3, y0 + h, "lm dash")
    body += t(x0 + w * 0.15, y0 + 16, "平衡化", weight=700, size=12)
    body += t(x0 + w * 0.65, y0 + 16, "本計算 (ここを平均・解析)", weight=700, size=12)
    body += t(x0 - 8, y0 + h / 2, "温度", "m", size=11, anchor="end")
    body += t(x0 + w / 2, y0 + h + 18, "時間 →  (時間刻み 1 fs なら 1,000 ステップ = 1 ps)", "m", size=11)
    svg("timeline", 410, 175, body, "始めは温度や体積が目標に向かって動くので (平衡化)、落ち着いてからの部分 (本計算) を使います。"
        "グラフで、目標のまわりを一定の幅で揺れているかを確かめます。", "平衡化と本計算")


def fig_ensemble():
    body = ""
    panels = [("NVT", "体積・温度が一定", "fixed"), ("NPT", "圧力・温度が一定", "press"), ("NVE", "エネルギーが一定", "iso")]
    for i, (name, note, kind) in enumerate(panels):
        x = 12 + i * 135
        y = 30
        body += t(x + 55, 20, name, weight=700)
        if kind == "press":
            body += box(x + 6, y + 6, 98, 98, cls="ghost dash", rx=0)
            for (ax, ay, bx2, by2) in ((x + 55, y - 8, x + 55, y + 8), (x + 55, y + 126, x + 55, y + 110),
                                       (x - 8, y + 58, x + 8, y + 58), (x + 118, y + 58, x + 102, y + 58)):
                body += arrow(ax, ay, bx2, by2, "lb")
        body += box(x + 12, y + 12, 86, 86, cls="bx thick" if kind == "fixed" else "bx", rx=0)
        rng = np.random.default_rng(i + 1)
        for _ in range(9):
            body += circle(x + 22 + rng.random() * 66, y + 22 + rng.random() * 66, 4, "atom")
        if kind != "iso":
            wave = " ".join(f"{x + 12 + k * 4:.1f},{y + 140 + 4 * math.sin(k):.1f}" for k in range(22))
            body += f'<polyline points="{wave}" class="lb"/>'
            body += t(x + 55, y + 160, "熱浴 (温度を保つ)", "b", size=11)
        else:
            body += t(x + 55, y + 150, "外とやり取りなし", "m", size=11)
        body += t(x + 55, y + 182, note, "m", size=11)
    body += t(204, 245, "NPT の矢印: セルの大きさ (形) が圧力に合わせて変わる", "m", size=11)
    svg("ensemble", 420, 255, body, "アンサンブルは「何を一定にするか」。平衡な体積を求める・相転移を見るときは NPT、"
        "体積を決めて調べるときは NVT、熱浴の影響を入れたくないときは NVE。", "アンサンブル")


def fig_cellmode():
    body = ""
    shapes = [("元のセル", [(0, 0), (60, 0), (60, 60), (0, 60)]),
              ("等方", [(-8, -8), (68, -8), (68, 68), (-8, 68)]),
              ("直方体", [(-12, 6), (72, 6), (72, 54), (-12, 54)]),
              ("三斜晶", [(6, 0), (78, 0), (60, 60), (-12, 60)])]
    notes = ["", "形を保って\n伸び縮み", "3 辺が別々", "形も変わる\n(構造相転移)"]
    for i, (name, pts) in enumerate(shapes):
        x = 30 + i * 98
        y = 40
        if i:
            body += poly([(x + px, y + py) for px, py in shapes[0][1]], "ghost-l dash", closed=True)
        body += poly([(x + px, y + py) for px, py in pts], "bx", closed=True)
        body += t(x + 30, 22, name, weight=700, size=12)
        if notes[i]:
            body += t(x + 30, 140, notes[i], "m", size=11)
    svg("cellmode", 420, 165, body, "NPT でのセルの変え方。結晶の形が変わる転移 (bcc → hcp など) は「三斜晶」でないと起こりません。"
        "液体では形が崩れやすいので「等方」。", "セルの変え方")


# ---- 10 章: PLUMED ----------------------------------------------------------------------

def _fes(s):
    """2 つの谷を持つ自由エネルギー (模式)。谷 A (s≈0.27) が深く、谷 B (s≈0.73)。"""
    return (1.25 - 1.0 * np.exp(-((s - 0.27) / 0.11) ** 2) - 0.8 * np.exp(-((s - 0.73) / 0.11) ** 2)
            + 1.8 * (s - 0.5) ** 4)


def fig_metad():
    s = np.linspace(0, 1, 200)
    f = _fes(s)
    body = ""
    stages = [("① 谷 A の中で揺れるだけ", None, 0.27),
              ("② 今いる所に山を積む → 谷 A が埋まる", ("A", 0.62), 0.33),
              ("③ 障壁を越えて谷 B へ", ("A", 0.98), 0.70),
              ("④ 全体が平らになる → 積んだ山を裏返すと F(s)", ("AB", 1.18), 0.5)]
    for k, (title, fill, ball) in enumerate(stages):
        x0, y0, w, h = 30, 36 + k * 118, 360, 70
        bias = np.zeros_like(s)
        if fill:
            region, level = fill
            mask = s < 0.52 if region == "A" else np.ones_like(s, bool)
            bias = np.where(mask, np.clip(level - f, 0, None), 0)
        top = f + bias
        yr = (-0.05, 1.4)
        pts_f = curve_points(s, f, x0, y0, w, h, (0, 1), yr)
        pts_t = curve_points(s, top, x0, y0, w, h, (0, 1), yr)
        if fill:
            body += poly(pts_t + pts_f[::-1], "hill", closed=True)
        body += poly(pts_f, "ln thick")
        if fill:
            body += poly(pts_t, "lb")
        bx = np.interp(ball, s, top)
        px, py = curve_points([ball], [bx], x0, y0, w, h, (0, 1), yr)[0]
        body += circle(px, py - 7, 6, "fa")
        body += t(x0, y0 - 16, title, anchor="start", size=12, weight=700)
        if k == 0:
            body += t(x0 + 0.27 * w, y0 + h + 10, "谷 A", "m", size=11)
            body += t(x0 + 0.73 * w, y0 + h + 10, "谷 B", "m", size=11)
            body += t(x0 + 0.5 * w, y0 - 1, "障壁", "m", size=11)
    body += t(30, 36 + 4 * 118 - 14, "太い線: 自由エネルギー F(s)　オレンジ: 積んだ山 (バイアス)", "m", anchor="start", size=11)
    body += t(30, 36 + 4 * 118 + 4, "横軸: CV の値 s　青い玉: 今の系の状態", "m", anchor="start", size=11)
    svg("metad", 420, 520, body, "メタダイナミクスの考え方。山 (ガウス関数) を少しずつ積むと、ふつうの MD では越えられない障壁を越えます。"
        "最後に積んだ山の形を裏返すと、谷の深さと障壁の高さ (自由エネルギー面) が分かります。", "メタダイナミクス")


def fig_cv():
    body = ""
    rng = np.random.default_rng(7)
    # 原子の集まり → 1 つの数
    body += box(10, 10, 120, 100, cls="bx", rx=6)
    for _ in range(26):
        body += circle(20 + rng.random() * 100, 20 + rng.random() * 80, 3.5, "atom")
    body += t(70, 126, "数百〜数万個の原子", "m", size=11)
    body += arrow(140, 60, 200, 60, "ln")
    body += t(170, 48, "まとめる", "m", size=11)
    x0, x1 = 215, 400
    body += line(x0, 60, x1, 60)
    for v in (0, 0.5, 1):
        xx = x0 + v * (x1 - x0)
        body += line(xx, 56, xx, 64) + t(xx, 78, f"{v:g}", "m", size=11)
    body += circle(x0 + 0.9 * (x1 - x0), 60, 6, "fa") + t(x0 + 0.9 * (x1 - x0), 44, "bcc", "a", size=12)
    body += circle(x0 + 0.1 * (x1 - x0), 60, 6, "fb") + t(x0 + 0.1 * (x1 - x0), 44, "hcp", "b", size=12)
    body += t((x0 + x1) / 2, 100, "CV (例: bcc との類似度)", "m", size=11)
    # よい CV と悪い CV
    for k, (title, ma, mb, cls) in enumerate((("よい CV: 2 つの状態がはっきり分かれる", 0.2, 0.8, "c"),
                                               ("悪い CV: 重なって区別できない", 0.45, 0.55, "b"))):
        y0 = 168 + k * 100
        xs = np.linspace(0, 1, 150)
        body += t(10, y0 - 8, title, cls, anchor="start", size=12, weight=700)
        body += line(20, y0 + 60, 400, y0 + 60)
        for m, c in ((ma, "la"), (mb, "lb")):
            ys = np.exp(-((xs - m) / 0.08) ** 2)
            body += poly(curve_points(xs, ys, 20, y0, 380, 56, (0, 1), (0, 1.05)), c)
        body += t(20 + ma * 380, y0 + 76, "状態 1", "a", size=11) + t(20 + mb * 380, y0 + 76, "状態 2", "b", size=11)
    body += t(210, 370, "曲線: MD 中の CV の値の分布", "m", size=11)
    svg("cv", 420, 380, body, "CV (集団変数) は、たくさんの原子の並び方を 1 つの数にまとめたもの。始めの状態と目的の状態で、"
        "値がはっきり違う CV を選びます。", "CV の考え方")


def fig_params():
    body = ""
    # σ とゆらぎ
    xs = np.linspace(0, 1, 160)
    body += t(10, 18, "幅 σ: CV のゆらぎの 1/2〜1 倍", anchor="start", weight=700, size=12)
    ys = np.exp(-((xs - 0.5) / 0.18) ** 2)
    body += poly(curve_points(xs, ys, 20, 26, 380, 70, (0, 1), (0, 1.1)), "lm")
    g = 0.55 * np.exp(-((xs - 0.5) / 0.08) ** 2)
    body += poly(curve_points(xs, g, 20, 26, 380, 70, (0, 1), (0, 1.1)), "lb thick")
    body += line(20, 96, 400, 96)
    body += t(330, 50, "記録した CV の分布", "m", size=11) + t(110, 70, "山 1 つ (幅 σ)", "b halo", size=11)
    # 高さ・間隔・well-tempered
    y0 = 130
    body += t(10, y0, "高さと置く間隔 (well-tempered では、だんだん低くなる)", anchor="start", weight=700, size=12)
    base = y0 + 90
    body += line(20, base, 400, base)
    for k in range(12):
        x = 34 + k * 31
        hgt = 62 / (1 + 0.28 * k)
        body += line(x, base, x, base - hgt, "lb thick")
    body += darrow(34, base + 12, 65, base + 12, "lm") + t(50, base + 26, "置く間隔 (PACE)", "m", size=11, anchor="start")
    body += darrow(24, base, 24, base - 62, "lm") + t(28, base - 66, "高さ", "m", size=11, anchor="start")
    body += t(400, base + 26, "時間 →", "m", size=11, anchor="end")
    body += t(210, base + 50, "γ (バイアス係数) が大きいほど、ゆっくり低くなる\n= 高い障壁まで埋める", "m", size=11)
    svg("params", 420, 290, body, "メタダイナミクスのパラメータの意味。まずバイアスなしで CV を記録し、そのゆらぎから幅を決めるのが確実です。",
        "パラメータ")


def fig_fes_read():
    s = np.linspace(0, 1, 200)
    f = _fes(s)
    f = f - f.min()
    x0, y0, w, h = 40, 20, 340, 150
    yr = (-0.05, 1.35)
    body = line(x0, y0 + h, x0 + w, y0 + h) + line(x0, y0, x0, y0 + h)
    body += poly(curve_points(s, f, x0, y0, w, h, (0, 1), yr), "ln thick")
    ia, ib = np.argmin(np.where(s < 0.5, f, 9)), np.argmin(np.where(s > 0.5, f, 9))
    itop = ia + np.argmax(f[ia:ib])
    (ax, ay), (bx, by), (tx, ty) = curve_points(s[[ia, ib, itop]], f[[ia, ib, itop]], x0, y0, w, h, (0, 1), yr)
    body += line(ax - 20, ay, bx + 30, ay, "lm dash") + line(bx - 30, by, bx + 30, by, "lm dash")
    body += line(ax - 10, ty, tx + 20, ty, "lm dash")
    body += darrow(ax, ay - 4, ax, ty, "lb") + t(ax + 8, (ay + ty) / 2 + 4, "障壁", "b halo", anchor="start", size=12)
    body += darrow(bx + 24, ay, bx + 24, by, "la") + t(bx + 30, (ay + by) / 2 + 4, "ΔF", "a halo", anchor="start", size=12)
    body += t(ax, ay + 18, "谷 A (安定)", size=11) + t(bx, by + 18, "谷 B", size=11)
    body += t(x0 - 6, y0 + h / 2, "F", "m", anchor="end", size=12)
    body += t(x0 + w / 2, y0 + h + 18, "CV", "m", size=11)
    # 収束
    y1 = 230
    for k, (title, ok) in enumerate((("収束している: 形がそろう", True), ("まだ: 形が違う", False))):
        xa = 20 + k * 200
        body += t(xa + 90, y1 - 8, title, "c" if ok else "b", size=12, weight=700)
        body += line(xa, y1 + 80, xa + 180, y1 + 80)
        for j, frac in enumerate((0.25, 0.5, 0.75, 1.0)):
            if ok:
                ff = f * 1.0 + (1 - frac) * 0.12
            else:   # 谷 B がまだ埋まりきっていない: 途中ほど谷 B が浅く、谷 A が細い
                ff = (1.25 - 1.0 * np.exp(-((s - 0.27) / (0.06 + 0.05 * frac)) ** 2)
                      - 0.8 * frac ** 2 * np.exp(-((s - 0.73) / 0.11) ** 2) + 1.8 * (s - 0.5) ** 4)
                ff = ff - ff.min()
            cls = "la" if frac == 1.0 else "lm dash"
            body += poly(curve_points(s, ff, xa, y1, 180, 75, (0, 1), (-0.05, 1.6)), cls)
    body += t(210, y1 + 100, "点線: 途中 (25・50・75%) まで / 青: 最後まで。上下にずれるだけなら問題なし", "m", size=11)
    svg("fes_read", 420, 340, body, "自由エネルギー曲線の読み方。谷が安定な状態、谷のあいだの山の高さが障壁、谷の深さの差が自由エネルギー差 ΔF。"
        "途中までの曲線と形がそろっていれば収束しています。", "自由エネルギー曲線")


def fig_fes2d():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    x = np.linspace(0, 1, 160)
    X, Y = np.meshgrid(x, x)
    F = (1.2 - 1.0 * np.exp(-(((X - 0.85) / 0.12) ** 2 + ((Y - 0.12) / 0.12) ** 2))
         - 0.85 * np.exp(-(((X - 0.15) / 0.12) ** 2 + ((Y - 0.85) / 0.13) ** 2))
         - 0.35 * np.exp(-(((X - 0.5) / 0.25) ** 2 + ((Y - 0.5) / 0.25) ** 2))
         + 0.6 * ((X - 0.5) ** 2 + (Y - 0.5) ** 2))
    levels = np.linspace(F.min(), F.min() + 1.0, 8)
    cs = plt.contourf(X, Y, F, levels=levels)
    x0, y0, w, h = 60, 14, 240, 240
    body = ""
    shades = ["s1", "s2", "s3", "s4", "s5", "s6", "s7"]
    for level, segs in zip(range(len(cs.allsegs)), cs.allsegs):
        for seg in segs:
            if len(seg) < 3:
                continue
            pts = [(x0 + px * w, y0 + h - py * h) for px, py in seg]
            body += poly(pts, f"cont {shades[min(level, 6)]}", closed=True)
    plt.close("all")
    body = f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" class="cont s8"/>' + body
    body += f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" class="frame"/>'
    path = [(0.85, 0.12), (0.62, 0.3), (0.5, 0.5), (0.35, 0.68), (0.15, 0.85)]
    body += poly([(x0 + px * w, y0 + h - py * h) for px, py in path], "lw dash thick")
    body += t(x0 + 0.85 * w, y0 + h - 0.12 * h + 4, "bcc", "w", size=12, weight=700)
    body += t(x0 + 0.15 * w, y0 + h - 0.85 * h + 4, "hcp", "w", size=12, weight=700)
    body += t(x0 + 0.5 * w + 12, y0 + h - 0.5 * h - 8, "峠 (障壁)", "w", size=11, anchor="start")
    body += t(x0 + w / 2, y0 + h + 18, "CV 1: bcc との類似度 →", "m", size=11)
    body += t(x0 - 14, y0 + h / 2, "CV 2: hcp との類似度 →", "m", size=11, rotate=-90)
    for k in range(7):
        body += box(318, 30 + k * 24, 18, 18, cls=f"cont {shades[k]}", rx=2)
    body += t(342, 44, "低い (安定)", "m", size=11, anchor="start") + t(342, 188, "高い", "m", size=11, anchor="start")
    svg("fes2d", 420, 280, body, "2 次元の自由エネルギー面 (模式図)。色の濃い所が安定な状態 (谷)。2 つの谷を結ぶ道のいちばん高い所 (峠) が、"
        "転移の障壁です。", "2 次元の自由エネルギー面")


def fig_plumed_flow():
    body = box(90, 8, 240, 40, cls="bx fa-edge") + t(210, 33, "GPUMD (MD・GPU)", weight=700, size=13)
    body += arrow(160, 48, 160, 84, "la") + t(152, 70, "毎ステップ 座標", "a", size=11, anchor="end")
    body += arrow(270, 84, 270, 48, "lb") + t(278, 70, "バイアスの力", "b", size=11, anchor="start")
    body += box(20, 84, 380, 152, cls="bx fb-edge", rx=10)
    items = [("GROUP", "原子を選ぶ", 30, 110), ("CV (cv1)", "1 つの数に", 155, 110), ("METAD", "山を積む", 280, 110),
             ("PRINT", "記録する", 155, 178)]
    for name, note, x, y in items:
        body += box(x, y, 110, 44, cls="bx soft")
        body += t(x + 55, y + 19, name, size=12, weight=700) + t(x + 55, y + 36, note, "m", size=11)
    body += arrow(140, 132, 155, 132) + arrow(265, 132, 280, 132) + arrow(210, 154, 210, 178)
    body += t(32, 102, "PLUMED (plumed.dat・CPU)", "b", size=12, anchor="start", weight=700)
    body += arrow(210, 222, 210, 270, "lm") + box(155, 270, 110, 28, "COLVAR", cls="bx", size=12)
    body += arrow(335, 154, 335, 270, "lm") + box(280, 270, 110, 28, "HILLS", cls="bx", size=12)
    body += t(210, 318, "COLVAR: CV の記録　HILLS: 積んだ山 → アプリが自由エネルギー面を作る", "m", size=11)
    svg("plumed_flow", 420, 330, body, "PLUMED は毎ステップ GPUMD から原子の座標を受け取り、CV を計算して山 (バイアス) の力を返します。"
        "CV の記録は COLVAR、積んだ山は HILLS に書かれます。", "PLUMED の流れ")


# ---- 11 章: 構造相転移 -------------------------------------------------------------------

def fig_hysteresis():
    x0, y0, w, h = 50, 20, 330, 160
    T = np.linspace(0, 1, 200)
    up = 1 / (1 + np.exp(-(T - 0.68) / 0.025))
    down = 1 / (1 + np.exp(-(T - 0.36) / 0.025))
    body = line(x0, y0 + h, x0 + w, y0 + h) + line(x0, y0, x0, y0 + h)
    body += poly(curve_points(T, up, x0, y0, w, h, (0, 1), (-0.05, 1.05)), "lb thick")
    body += poly(curve_points(T, down, x0, y0, w, h, (0, 1), (-0.05, 1.05)), "la thick")
    body += arrow(x0 + 0.5 * w, y0 + h - 4, x0 + 0.6 * w, y0 + h - 4, "lb")
    body += arrow(x0 + 0.55 * w, y0 + 6, x0 + 0.45 * w, y0 + 6, "la")
    body += t(x0 + 0.84 * w, y0 + 0.62 * h, "行き (昇温)", "b", size=12)
    body += t(x0 + 0.16 * w, y0 + 0.4 * h, "帰り (降温)", "a", size=12)
    mid = (0.68 + 0.36) / 2
    body += line(x0 + mid * w, y0, x0 + mid * w, y0 + h, "lc dash")
    body += t(x0 + mid * w, y0 + h + 30, "真ん中 = 転移点の目安", "c", size=11)
    body += darrow(x0 + 0.36 * w, y0 + h * 0.5, x0 + 0.68 * w, y0 + h * 0.5, "lm")
    body += t(x0 + mid * w, y0 + h * 0.5 + 16, "ヒステリシス", "m halo", size=11)
    body += t(x0 - 8, y0 + 8, "1", "m", anchor="end", size=11) + t(x0 - 8, y0 + h, "0", "m", anchor="end", size=11)
    body += t(x0 - 8, y0 + h / 2, "高温相\nの割合", "m", anchor="end", size=11)
    body += t(x0 + w / 2, y0 + h + 15, "温度 →", "m", size=11)
    svg("hysteresis", 420, 215, body, "MD の構造相転移は、昇温では本当の転移点より高い温度 (過熱)、降温では低い温度 (過冷却) で起こります。"
        "行きと帰りを流し、その真ん中を目安にします。小さい系・短い計算ほどずれが大きくなります。", "ヒステリシス")


# ---- 12 章: ジョブ ------------------------------------------------------------------------

def fig_lanes():
    body = t(10, 22, "GPU の列 (計算)", anchor="start", weight=700, size=12)
    for k, (name, state) in enumerate((("MD", "実行中"), ("拡散", "待ち"), ("PLUMED", "待ち"))):
        body += box(10 + k * 132, 32, 122, 40, cls="bx" + (" fa-edge" if state == "実行中" else " soft"))
        body += t(71 + k * 132, 50, name, size=12, weight=700) + t(71 + k * 132, 66, state, "m", size=11)
    body += t(10, 104, "解析の列 (CPU)", anchor="start", weight=700, size=12)
    for k, (name, state) in enumerate((("ボロノイ", "実行中"), ("結晶構造の判定", "待ち"))):
        body += box(10 + k * 132, 114, 122, 40, cls="bx" + (" fc-edge" if state == "実行中" else " soft"))
        body += t(71 + k * 132, 132, name, size=12, weight=700) + t(71 + k * 132, 148, state, "m", size=11)
    body += t(210, 180, "それぞれ投入した順に 1 つずつ。2 つの列は同時に進む", "m", size=11)
    svg("lanes", 420, 190, body, "ジョブの順番。GPU の計算は 1 つずつ、解析 (ボロノイ・結晶構造の判定) は別の列で 1 つずつ動きます。",
        "ジョブの列")


# ---- 14 章: 入力ファイル -----------------------------------------------------------------

def fig_runin():
    blocks = [("共通の設定", ["potential nep89.txt", "velocity 300", "time_step 1"], "soft"),
              ("ステージ 1 (平衡化)", ["ensemble npt_ber 300 300 100 …", "dump_thermo 100", "run 10000"], "fa-edge"),
              ("ステージ 2 (本計算)", ["ensemble nvt_nhc 300 300 100", "dump_xyz 1000 dump.xyz", "run 100000"], "fb-edge")]
    body = ""
    y = 8
    for k, (title, lines, cls) in enumerate(blocks):
        hgt = 26 + 18 * len(lines)
        body += box(10, y, 300, hgt, cls=f"bx {cls}")
        body += t(20, y + 18, title, anchor="start", weight=700, size=12)
        for j, ln in enumerate(lines):
            body += t(28, y + 38 + j * 18, ln, "mono", anchor="start", size=11.5)
        if k:
            body += t(318, y + 38 + 2 * 18, "← run で実行", "m", anchor="start", size=11)
        y += hgt + 12
    body += arrow(395, 20, 395, y - 16, "lm") + t(390, y - 4, "上から順", "m", size=11, anchor="end")
    svg("runin", 420, y + 4, body, "run.in は上から順に読まれます。設定のあと、ensemble (どう制御するか) と出力を書いて run で実行。"
        "run ごとに 1 つのステージです。", "run.in の構造")


# ---- 8 章: アレニウス ------------------------------------------------------------------

def fig_arrhenius():
    body = ""
    # MSD
    x0, y0, w, h = 40, 26, 150, 110
    body += t(115, 14, "MSD と時間", weight=700, size=12)
    body += line(x0, y0 + h, x0 + w, y0 + h) + line(x0, y0, x0, y0 + h)
    for slope, cls, name in ((0.9, "lb", "高温"), (0.45, "la", "中"), (0.04, "lm", "低温")):
        body += line(x0, y0 + h, x0 + w, y0 + h - slope * h, cls + " thick")
        body += t(x0 + w + 4, y0 + h - slope * h + 4, name, "m", size=11, anchor="start")
    body += t(x0 + w / 2, y0 + h + 16, "時間", "m", size=11) + t(x0 - 6, y0 + h / 2, "MSD", "m", size=11, anchor="end")
    body += t(x0 + w / 2, y0 + h + 32, "傾き = 6D (3 次元)", "m", size=11)
    # アレニウス
    x0 = 250
    body += t(x0 + 75, 14, "アレニウスプロット", weight=700, size=12)
    body += line(x0, y0 + h, x0 + w, y0 + h) + line(x0, y0, x0, y0 + h)
    pts = [(0.1, 0.88), (0.3, 0.68), (0.5, 0.47), (0.7, 0.29)]
    body += line(x0, y0 + h - 0.98 * h, x0 + 0.85 * w, y0 + h - 0.1 * h, "la")
    for px, py in pts:
        body += circle(x0 + px * w, y0 + h - py * h, 4, "fa")
    body += circle(x0 + 0.9 * w, y0 + h - 0.05 * h, 4, "hollow")
    body += t(x0 + 0.9 * w + 8, y0 + h - 0.05 * h - 22, "外す\n(拡散していない)", "m halo", size=10.5, anchor="end")
    body += t(x0 + w / 2, y0 + h + 16, "1000 / T", "m", size=11) + t(x0 - 6, y0 + h / 2, "ln D", "m", size=11, anchor="end")
    body += t(x0 + w / 2, y0 + h + 32, "傾き = −Ea / kB", "m", size=11)
    svg("arrhenius", 420, 180, body, "拡散係数 D は MSD (平均二乗変位) の傾きから。温度を変えて ln D を 1000/T に対して並べ、直線の傾きから"
        "活性化エネルギー Ea を出します。ほとんど拡散していない点 (MSD が伸びない低温) は外します。", "アレニウスプロット")


# ---- 15〜17 章: 解析 ---------------------------------------------------------------------

def fig_voronoi():
    from scipy.spatial import Voronoi
    rng = np.random.default_rng(11)
    pts = []
    for i in range(-2, 9):
        for j in range(-2, 8):
            pts.append((i + 0.5 * (j % 2) + 0.18 * rng.standard_normal(), j * 0.87 + 0.18 * rng.standard_normal()))
    pts = np.array(pts)
    vor = Voronoi(pts)
    scale, ox, oy = 46, 22, 10
    body = ""
    centre = int(np.argmin(np.linalg.norm(pts - np.array([3.2, 2.6]), axis=1)))
    for k, region_index in enumerate(vor.point_region):
        region = vor.regions[region_index]
        if not region or -1 in region:
            continue
        poly_pts = [(ox + vor.vertices[v][0] * scale, oy + vor.vertices[v][1] * scale) for v in region]
        if all(0 <= x <= 330 and 0 <= y <= 250 for x, y in poly_pts):
            cls = "vcell hi" if k == centre else "vcell"
            body += poly(poly_pts, cls, closed=True)
    for k, (px, py) in enumerate(pts):
        x, y = ox + px * scale, oy + py * scale
        if 0 <= x <= 330 and 0 <= y <= 250:
            body += circle(x, y, 5 if k == centre else 3.5, "fa" if k == centre else "atom")
    nfaces = len(vor.regions[vor.point_region[centre]])
    body += t(340, 110, f"この原子の\nまわりの多角形\n= {nfaces} 角形\n\n辺の数\n= となりの数", "m", size=11, anchor="start")
    svg("voronoi", 420, 255, body, "ボロノイ多面体の 2 次元の模式図。となりの原子との垂直二等分線で囲んだ領域です。3 次元では多面体になり、"
        "その面を三角形・四角形・五角形・六角形ごとに数えたものがボロノイ指数 ⟨n3,n4,n5,n6⟩ です。", "ボロノイ多面体")


def fig_cna():
    cx, cy, r = 120, 130, 70
    ang = [math.radians(a) for a in range(0, 360, 60)]
    nb = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in ang]
    j = 0
    common = [1, 5]
    body = ""
    for k, (x, y) in enumerate(nb):
        body += line(cx, cy, x, y, "lm")
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0)):
        cls = "lb thick" if (a in (j, *common) and b in (j, *common)) else "lm"
        body += line(*nb[a], *nb[b], cls)
    for k, (x, y) in enumerate(nb):
        cls = "fa" if k == j else ("fb" if k in common else "atom")
        body += circle(x, y, 9 if k == j else 7, cls)
    body += circle(cx, cy, 9, "fc")
    body += t(cx - 16, cy - 14, "中心", "c halo", size=11, weight=700, anchor="end")
    body += t(nb[j][0] + 14, nb[j][1] + 4, "となり j", "a", size=11, anchor="start")
    body += t(250, 40, "① 中心ととなり j の両方に結合\n　している原子 (オレンジ) の数", size=11.5, anchor="start")
    body += t(250, 100, "② その原子どうしの結合の数", size=11.5, anchor="start")
    body += t(250, 145, "③ 結合のつながりの長さ", size=11.5, anchor="start")
    body += t(250, 200, "すべてのとなりで\n(4,2,1) → fcc\n(4,2,1)×6 + (4,2,2)×6 → hcp\n(6,6,6)×8 + (4,4,4)×6 → bcc",
              "m", size=11, anchor="start")
    svg("cna", 420, 260, body, "結晶構造の判定 (CNA) の考え方 (2 次元の模式図)。中心の原子のとなり 1 つずつについて、"
        "共通のとなりの数・その結合の数・つながりを数え、その組み合わせが理想的な結晶と同じかで判定します。", "CNA")


def fig_distribution():
    body = box(20, 20, 140, 220, cls="bx", rx=0)
    rng = np.random.default_rng(5)
    zs = []
    for _ in range(70):
        z = rng.random()
        sym = "fa" if z < 0.45 else "fb"
        if 0.4 < z < 0.6 and rng.random() < 0.5:
            sym = "fb"
        zs.append((z, sym))
        body += circle(30 + rng.random() * 120, 30 + (1 - z) * 200, 3.5, sym)
    body += arrow(10, 240, 10, 20, "lm") + t(10, 14, "z", "m", size=12)
    body += arrow(170, 130, 210, 130, "ln") + t(190, 118, "射影", "m", size=11)
    bins = np.linspace(0, 1, 11)
    for k in range(10):
        lo, hi = bins[k], bins[k + 1]
        na = sum(1 for z, s in zs if lo <= z < hi and s == "fa")
        nb_ = sum(1 for z, s in zs if lo <= z < hi and s == "fb")
        y = 30 + (1 - hi) * 200
        body += f'<rect x="220" y="{y + 2:.1f}" width="{na * 12}" height="16" class="fa"/>'
        body += f'<rect x="{220 + na * 12}" y="{y + 2:.1f}" width="{nb_ * 12}" height="16" class="fb"/>'
    body += t(300, 252, "原子数 (分割幅ごと)", "m", size=11)
    svg("distribution", 420, 262, body, "原子分布: 選んだ元素の原子を 1 つの軸 (ここでは z) に射影し、分割幅ごとに数えます。"
        "膜の厚み方向の組成・界面への偏析・拡散のプロファイルが見えます。", "原子分布")


def fig_rdf_cutoff():
    r = np.linspace(0, 6, 400)
    g = (2.9 * np.exp(-((r - 2.55) / 0.16) ** 2) + 1.0 * np.exp(-((r - 3.6) / 0.22) ** 2)
         + 1.35 * np.exp(-((r - 4.45) / 0.25) ** 2) + 1.1 * np.exp(-((r - 5.05) / 0.25) ** 2))
    g = np.where(r < 2.1, 0, g) + 0.9 * (1 / (1 + np.exp(-(r - 3.0) / 0.25))) * (r > 2.1) * 0.3
    x0, y0, w, h = 50, 30, 330, 170
    xr, yr = (0, 6), (0, 3.3)
    rc = 3.1
    shade = [(px, py) for px, py in curve_points(r[r <= rc], g[r <= rc], x0, y0, w, h, xr, yr)]
    base_l = curve_points([rc], [0], x0, y0, w, h, xr, yr)[0]
    body = poly(shade + [base_l, (x0, y0 + h)], "shade-a", closed=True)
    body += line(x0, y0 + h, x0 + w, y0 + h) + line(x0, y0, x0, y0 + h)
    body += poly(curve_points(r, g, x0, y0, w, h, xr, yr), "ln thick")
    px = x0 + rc / 6 * w
    body += line(px, y0 - 6, px, y0 + h, "lb dash thick")
    body += t(px + 6, y0 + 4, "数える距離 (第 1 の谷)", "b halo", anchor="start", size=12)
    body += t(x0 + 2.55 / 6 * w, y0 + 34, "第 1 配位殻", "a halo", size=12, weight=700)
    body += t(x0 + 2.55 / 6 * w, y0 + h - 18, "ここを数える\n= 配位数", "a halo", size=11)
    body += t(x0 + 4.7 / 6 * w, y0 + 70, "第 2・第 3 近接…", "m", size=11)
    for v in range(0, 7):
        body += t(x0 + v / 6 * w, y0 + h + 16, str(v), "m", size=11)
    body += t(x0 + w / 2, y0 + h + 34, "原子からの距離 r (Å)", "m", size=11)
    body += t(x0 - 26, y0 + h / 2, "g(r)", "m", size=12, rotate=-90)
    svg("rdf_cutoff", 420, 245, body, "動径分布関数 g(r) は、原子からの距離 r に、平均に比べて何倍の原子があるか。"
        "最初の山 (第 1 配位殻) のあとの谷までにある原子の数が、配位数です。", "動径分布関数と配位数")


def fig_sro():
    """規則化・ランダム・クラスター化の 2 次元の並びと、第 1 近接の α (違う元素の組)。"""
    rng = np.random.default_rng(4)
    n = 6
    panels = [("規則化", "α < 0", lambda i, j: (i + j) % 2), ("ランダム", "α ≈ 0", lambda i, j: int(rng.random() < 0.5)),
              ("クラスター化", "α > 0", lambda i, j: int(i >= 3))]
    body = ""
    for k, (title, value, rule) in enumerate(panels):
        x0 = 14 + k * 136
        body += t(x0 + 58, 18, title, weight=700, size=12)
        for i in range(n):
            for j in range(n):
                body += circle(x0 + 8 + i * 20, 34 + j * 20, 7.5, "fa" if rule(i, j) else "fb")
        body += t(x0 + 58, 168, value, "a" if k == 0 else ("m" if k == 1 else "b"), weight=700, size=13)
    body += t(210, 192, "青と橙の 2 元素。α は「となりに違う元素がいる割合」が\nランダムからどれだけずれるか", "m", size=11)
    svg("sro", 420, 214, body, "ウォーレン・カウリーの秩序度 (第 1 近接、違う元素の組)。となりに違う元素がランダムより多ければ負 (規則化)、"
        "少なければ正 (同じ元素が集まる)。殻 (第 1・第 2 近接…) ごとに求めます。", "短範囲秩序")


def fig_poly():
    """代表的な配位多面体 (四面体・八面体・立方体) と、多面体どうしの共有のしかた。"""
    def project(p, cx, cy, s):
        x, y, z = p
        # 斜めから見た投影 (z まわり 30°、x まわり 20°)
        a, b = math.radians(30), math.radians(-20)
        x1, y1 = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
        y2, z2 = y1 * math.cos(b) - z * math.sin(b), y1 * math.sin(b) + z * math.cos(b)
        return cx + x1 * s, cy - z2 * s, y2

    def solid(verts, faces, cx, cy, s, cls):
        pts = [project(v, cx, cy, s) for v in verts]
        out = ""
        for f in sorted(faces, key=lambda f: -sum(pts[v][2] for v in f) / len(f)):
            out += poly([(pts[v][0], pts[v][1]) for v in f], f"solid {cls}", closed=True)
        out += circle(cx, cy, 4, cls.replace("edge", "").strip() or "atom")
        return out
    # 頂点を 1 つ真上に向けた四面体 (斜めから見て立体に見えるように)
    t4 = [(0, 0, 1.2)] + [(1.13 * math.cos(a), 1.13 * math.sin(a), -0.4) for a in (0.3, 0.3 + 2 * math.pi / 3, 0.3 + 4 * math.pi / 3)]
    o6 = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    c8 = list(itertools.product((-1, 1), repeat=3))
    tf = [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]
    of = [(a, b, c) for a in (0, 1) for b in (2, 3) for c in (4, 5)]
    cf = [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)]
    body = ""
    for k, (name, verts, faces, cls, scale, note) in enumerate((
            ("四面体 (4)", t4, tf, "fa", 32, "SiO4・ZnS"), ("八面体 (6)", o6, of, "fb", 36, "TiO6・MgO6"),
            ("立方体 (8)", c8, cf, "fc", 22, "CaF8・bcc"))):
        cx = 70 + k * 140
        body += solid(verts, faces, cx, 66, scale, cls)
        body += t(cx, 122, name, weight=700, size=12) + t(cx, 138, note, "m", size=11)
    # 共有のしかた (2 次元の模式: 三角形どうし)
    y0 = 172
    body += t(10, y0, "多面体どうしのつながり方 (模式図)", anchor="start", weight=700, size=12)
    for k, (name, shared) in enumerate((("頂点共有", 1), ("辺共有", 2), ("面共有", 3))):
        x0 = 30 + k * 132
        a = [(x0, y0 + 60), (x0 + 30, y0 + 14), (x0 + 60, y0 + 60)]
        if shared == 1:
            b = [(x0 + 60, y0 + 60), (x0 + 90, y0 + 14), (x0 + 108, y0 + 66)]
        elif shared == 2:
            b = [(x0 + 30, y0 + 14), (x0 + 60, y0 + 60), (x0 + 84, y0 + 18)]
        else:
            b = [(x0 + 4, y0 + 56), (x0 + 30, y0 + 20), (x0 + 56, y0 + 56)]
        body += poly(a, "solid fa", closed=True) + poly(b, "solid fb", closed=True)
        body += t(x0 + 46, y0 + 86, name, size=12, weight=700)
        body += t(x0 + 46, y0 + 102, f"共有する原子 {shared if shared < 3 else '3 以上'}", "m", size=11)
    svg("poly", 420, 290, body, "配位多面体: 中心の原子 (点) のまわりの配位原子を頂点とする多面体。形は、中心から見た頂点どうしの角度の並びで判定します。"
        "多面体どうしは、共有する配位原子の数で頂点・辺・面共有に分けます (面共有は 3 次元で、模式図では重ねて示しています)。", "配位多面体")


# ---- 差し込み ----------------------------------------------------------------------------

def build_all():
    for name, func in list(globals().items()):
        if name.startswith("fig_") and callable(func):
            func()


def insert() -> list[str]:
    text = MANUAL.read_text(encoding="utf-8")
    used = []

    def repl(match):
        name = match.group(1)
        if name not in FIGS:
            raise SystemExit(f"図がありません: {name}")
        used.append(name)
        svg_text, caption = FIGS[name]
        return f'<figure class="fig" data-fig="{name}">{svg_text}<figcaption>{esc(caption)}</figcaption></figure>'
    text = re.sub(r'<figure class="fig" data-fig="([a-z0-9_]+)">.*?</figure>', repl, text, flags=re.S)
    MANUAL.write_text(text, encoding="utf-8")
    return used


if __name__ == "__main__":
    build_all()
    used = insert()
    unused = sorted(set(FIGS) - set(used))
    print(f"差し込んだ図: {len(used)} 枚 {used}")
    if unused:
        print(f"マニュアルに置き場所のない図: {unused}")
