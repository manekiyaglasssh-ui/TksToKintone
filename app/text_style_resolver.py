"""指図書編集文字の共有スタイルと論理メトリクス。

Qt と ReportLab は同じフォントエンジンではないため、描画APIそのものを
共有することはできない。一方、保存データの解釈、単位、行送り、装飾の
位置を各側で別々に持つと、同じオブジェクトでも見た目がずれる。この
モジュールは両側が参照する、環境に依存しない契約を定義する。
"""
from __future__ import annotations

from dataclasses import dataclass

TEXT_POINT_UNIT = "pt"
TEXT_LINE_HEIGHT_FACTOR = 1.2
TEXT_DECORATION_WIDTH_FACTOR = 0.045
TEXT_UNDERLINE_OFFSET_FACTOR = -0.12
TEXT_STRIKEOUT_OFFSET_FACTOR = 0.30
TEXT_SYNTHETIC_ITALIC_SHEAR = 0.20


@dataclass(frozen=True)
class TextStyle:
    family: str
    size_pt: float
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikeout: bool = False

    @classmethod
    def from_object(cls, obj: dict[str, object]) -> "TextStyle":
        """新旧保存形式を同じ正準スタイルへ変換する。"""
        def flag(name: str) -> bool:
            return bool(obj.get(f"font_{name}", obj.get(name, False)))
        return cls(
            family=str(obj.get("font_family") or "").strip(),
            size_pt=float(obj.get("font_size") or 10.0),
            bold=flag("bold"), italic=flag("italic"),
            underline=flag("underline"), strikeout=flag("strikeout"),
        )


def line_height_pt(size_pt: float) -> float:
    return max(0.1, float(size_pt)) * TEXT_LINE_HEIGHT_FACTOR


def decoration_geometry(size_pt: float) -> tuple[float, float, float]:
    """線幅、下線/取り消し線のベースラインからの位置を返す（pt）。"""
    size = max(0.1, float(size_pt))
    return (
        max(0.45, size * TEXT_DECORATION_WIDTH_FACTOR),
        size * TEXT_UNDERLINE_OFFSET_FACTOR,
        size * TEXT_STRIKEOUT_OFFSET_FACTOR,
    )
