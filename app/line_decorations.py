"""線・矢印・両矢印・二重線の共通描画ジオメトリ。

指図書編集画面（Qt scene 座標）と PDF 出力（reportlab 座標）で同じ計算ロジックを
使うため、座標系に依存しない純粋な幾何計算だけをここに集約する。線分の端点 (x1,y1)〜
(x2,y2) を渡すと、矢じり線分や二重平行線の端点リストを返す。

line_type の値:
- "line"         : 単純な直線
- "arrow"        : 終点側に矢じり
- "double_arrow" : 始点側・終点側の両方に矢じり
- "double_line"  : 2本の平行線
"""
from __future__ import annotations

import math

# line_type の正準値。旧データ（line_type 無し）は "line" 扱い。
LINE_TYPE_LINE = "line"
LINE_TYPE_ARROW = "arrow"
LINE_TYPE_DOUBLE_ARROW = "double_arrow"
LINE_TYPE_DOUBLE_LINE = "double_line"
LINE_TYPES = (
    LINE_TYPE_LINE,
    LINE_TYPE_ARROW,
    LINE_TYPE_DOUBLE_ARROW,
    LINE_TYPE_DOUBLE_LINE,
)

# 矢じりの長さ（pt）と開き角（ラジアン）。見やすい固定値。
ARROW_HEAD_LENGTH = 12.0
ARROW_HEAD_ANGLE = math.radians(26.0)
# 二重線の2本の間隔（pt）。見やすい固定値。
DOUBLE_LINE_GAP = 3.5

Segment = tuple[float, float, float, float]


def normalize_line_type(value: object) -> str:
    """line_type を正準値へ正規化する。未知/未設定は "line"（旧データ互換）。"""
    text = str(value or "").strip().lower()
    return text if text in LINE_TYPES else LINE_TYPE_LINE


def _unit(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float]:
    dx = x2 - x1
    dy = y2 - y1
    dist = math.hypot(dx, dy)
    if dist <= 0.0:
        return 0.0, 0.0, 0.0
    return dx / dist, dy / dist, dist


def arrowhead_segments(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    size: float = ARROW_HEAD_LENGTH,
    angle: float = ARROW_HEAD_ANGLE,
) -> list[Segment]:
    """終点 (x2,y2) に付ける矢じりの2本の線分を返す。

    各線分は矢じりの羽根から終点へ向かう (ax, ay, x2, y2)。(x1,y1) は向きの基準。
    線が長さゼロのときは空リストを返す。
    """
    ux, uy, dist = _unit(x1, y1, x2, y2)
    if dist <= 0.0:
        return []
    # 終点から線に沿って戻る方向ベクトル。
    bx, by = -ux, -uy
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    # 戻り方向を ±angle 回転して羽根の方向を得る。
    lx = bx * cos_a - by * sin_a
    ly = bx * sin_a + by * cos_a
    rx = bx * cos_a + by * sin_a
    ry = -bx * sin_a + by * cos_a
    return [
        (x2 + lx * size, y2 + ly * size, x2, y2),
        (x2 + rx * size, y2 + ry * size, x2, y2),
    ]


def double_line_segments(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    gap: float = DOUBLE_LINE_GAP,
) -> list[Segment]:
    """二重線の2本の平行線分を返す。線の向きに対し垂直方向へ ±gap/2 ずらす。"""
    ux, uy, dist = _unit(x1, y1, x2, y2)
    if dist <= 0.0:
        # 長さゼロのときは平行線を作れないので元の線分のみ返す。
        return [(x1, y1, x2, y2)]
    nx, ny = -uy, ux  # 法線（垂直）単位ベクトル
    off = gap / 2.0
    return [
        (x1 + nx * off, y1 + ny * off, x2 + nx * off, y2 + ny * off),
        (x1 - nx * off, y1 - ny * off, x2 - nx * off, y2 - ny * off),
    ]


def line_segments(
    line_type: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> list[Segment]:
    """line_type に応じて描画すべき全線分（本体＋装飾）をまとめて返す。

    GUI/PDF 双方からこの1関数を呼べば描画が一致する。
    """
    lt = normalize_line_type(line_type)
    if lt == LINE_TYPE_DOUBLE_LINE:
        return double_line_segments(x1, y1, x2, y2)
    segs: list[Segment] = [(x1, y1, x2, y2)]
    if lt in (LINE_TYPE_ARROW, LINE_TYPE_DOUBLE_ARROW):
        segs.extend(arrowhead_segments(x1, y1, x2, y2))
    if lt == LINE_TYPE_DOUBLE_ARROW:
        segs.extend(arrowhead_segments(x2, y2, x1, y1))
    return segs
