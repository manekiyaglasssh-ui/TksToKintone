"""指図書編集画面（全画面プレビュー編集）。

指図書(1)のプレビューを背景表示し、Excelの図形挿入のように
テキスト・直線・四角形・丸/楕円を自由に書ける編集画面。テキスト/図形は
ドラッグで矩形範囲を指定して作成し、線はドラッグ始点〜終点で作成する。
作成後・再読み込み後とも、選択・移動・サイズ変更・削除・テキスト編集が
できる。

レイヤー構成:
- background_layer: 指図書(1)プレビュー（編集不可・保存対象外, ZValue=-100）
- edit_layer:       ユーザーが追加したテキスト/線/四角形/楕円（編集可・保存対象）

背景は常に「編集オブジェクトなし」の指図書(1)プレビューから生成する。編集
オブジェクトを焼き込んだPDF/画像を背景に使わないことで、保存済みテキストが
背景へ二重表示されるのを防ぐ（要件1・11・13）。保存済みオブジェクトはJSONから
読み込み、編集レイヤーにだけ配置する。

座標系:
- QGraphicsScene はPDFのポイント空間（0,0〜PAGE_W,PAGE_H、Qtはy下向き）で扱う。
- 保存JSONも同じ左上原点の scene 座標で保持する。
  PDF生成時だけ reportlab 座標（原点=左下）へ変換して重ね描きする。

各編集オブジェクトは作成時に発番した id を保持し、再読み込み・再保存しても
id が維持されるため、同じオブジェクトが二重生成されない（要件1・2）。
"""
from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QKeySequence,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTextOption,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QWidget,
)

from app.voucher_edit_objects import load_edit_objects, save_edit_objects
from app.voucher_edit_objects import COORDINATE_ORIGIN, GEOMETRY_BASIS
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark
from app.voucher_templates import PAGE_H, PAGE_W

# 背景消失・選択状態の再発切り分け用ロガー（要件12）。アプリ既定ロガーへ debug 出力する。
_log = logging.getLogger("tks_to_kintone_app")

# ツールモード
TOOL_SELECT = "select"
TOOL_TEXT = "text"
TOOL_LINE = "line"
TOOL_RECT = "rect"
TOOL_ELLIPSE = "ellipse"

# scene の data キー
_DATA_TYPE = 0

# 背景レイヤーの目印
_BG_MARK = "_background"

DEFAULT_FONT_SIZE = 12.0
DEFAULT_LINE_WIDTH = 1.0
DEFAULT_COLOR = (0.0, 0.0, 0.0)
DEFAULT_TEXT_COLOR = "#000000"
DEFAULT_STROKE_COLOR = "#000000"
MIN_TEXT_W = 60.0
MIN_TEXT_H = 18.0
# 画像・四角・丸のリサイズ最小サイズ（要件: min_width/min_height）。
MIN_RESIZE = 10.0
# リサイズ/端点ハンドルの見た目サイズとクリック判定サイズ（不具合1）。
# 見た目は小さく、クリック判定は大きくして掴みやすくする。
HANDLE_SIZE = 10.0
HANDLE_HIT_SIZE = 18.0
RECT_TEXT_PAD = 3.0
SYMBOL_TEXT_MAX_CHARS = 3

TEXT_FONT_CANDIDATES = [
    "Yu Gothic UI",
    "Meiryo",
    "MS Gothic",
]

# ツールバーのボタン幅・余白を広げ、削除=警告色/保存=安全色を割り当てる（要件2-5・2-6・2-7・3）。
# ライト/ダーク両モードで文字が読めるよう、警告色・安全色は白文字＋濃色背景にする。
EDIT_TOOLBAR_STYLE = """
QToolBar { spacing: 6px; padding: 4px; }
QToolBar QToolButton {
    border: 1px solid #666666;
    border-radius: 5px;
    padding-left: 12px;
    padding-right: 12px;
    padding-top: 4px;
    padding-bottom: 4px;
    min-height: 26px;
    margin: 1px;
}
QToolBar QToolButton:hover {
    border: 1px solid #999999;
}
QToolBar QToolButton:pressed {
    border: 1px solid #2aa8ff;
    background-color: rgba(42, 168, 255, 60);
}
QToolBar QToolButton:checked {
    border: 2px solid #2aa8ff;
    font-weight: bold;
}
QToolButton#dangerButton {
    background-color: #c62828;
    color: white;
    border: 1px solid #8e0000;
    border-radius: 5px;
    font-weight: bold;
}
QToolButton#dangerButton:hover { background-color: #d32f2f; border: 1px solid #b71c1c; }
QToolButton#dangerButton:pressed { background-color: #b71c1c; }
QToolButton#dangerButton:disabled { background-color: #9e9e9e; color: #eeeeee; border: 1px solid #757575; }
QToolButton#successButton {
    background-color: #0b7a3b;
    color: white;
    border: 1px solid #075c2d;
    border-radius: 5px;
    font-weight: bold;
}
QToolButton#successButton:hover { background-color: #109149; border: 1px solid #0a6a35; }
QToolButton#successButton:pressed { background-color: #075c2d; }
QToolButton#successButton:disabled { background-color: #9e9e9e; color: #eeeeee; border: 1px solid #757575; }
"""


def pick_text_font_family(candidates: list[str] | None = None) -> str:
    """OSに存在する通常テキスト用フォント候補を上から探して返す。

    どれも存在しない場合は Qt の汎用フォールバックに任せるため空文字を返す。
    """
    cands = candidates if candidates is not None else TEXT_FONT_CANDIDATES
    try:
        available = set(QFontDatabase.families())
    except Exception:
        available = set()
    for name in cands:
        if name in available:
            return name
    return ""


def resolve_text_font_family(family: str | None = None) -> str:
    """保存済みフォントが使える場合はそれを使い、無ければ候補から選ぶ。"""
    name = (family or "").strip()
    try:
        available = set(QFontDatabase.families())
    except Exception:
        available = set()
    if name and (not available or name in available):
        return name
    return pick_text_font_family()


def make_text_font(font_size: float, family: str | None = None,
                   bold: bool = False, italic: bool = False) -> QFont:
    """通常テキスト用 QFont を生成する。"""
    family = resolve_text_font_family(family)
    font = QFont(family) if family else QFont()
    font.setPointSizeF(float(font_size))
    font.setBold(bool(bold))
    font.setItalic(bool(italic))
    return font


def _color_name(color: str | QColor | None, default: str = "#000000") -> str:
    qcolor = QColor(color or default)
    return qcolor.name() if qcolor.isValid() else default


def _configure_text_document(item: QGraphicsTextItem) -> None:
    item.document().setDocumentMargin(0)
    item.document().setDefaultStyleSheet("p { margin: 0; line-height: 120%; }")


def _normalize_text_align(value: str | None) -> str:
    return value if value in {"left", "center", "right"} else "center"


def _normalize_vertical_align(value: str | None) -> str:
    return value if value in {"top", "middle", "bottom"} else "middle"


def _qt_text_alignment(text_align: str) -> Qt.AlignmentFlag:
    if text_align == "left":
        return Qt.AlignmentFlag.AlignLeft
    if text_align == "right":
        return Qt.AlignmentFlag.AlignRight
    return Qt.AlignmentFlag.AlignHCenter


def _apply_text_alignment(item: QGraphicsTextItem, text_align: str) -> None:
    opt = QTextOption()
    opt.setAlignment(_qt_text_alignment(text_align))
    item.document().setDefaultTextOption(opt)


def _text_document_height(item: QGraphicsTextItem) -> float:
    return float(item.document().documentLayout().documentSize().height())


def _text_content_size(text: str, font: QFont, font_size: float) -> tuple[float, float]:
    metrics = QFontMetricsF(font)
    lines = text.splitlines() or [""]
    width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
    line_h = float(font_size) * 1.2
    return float(width), float(len(lines) * line_h)


def is_symbol_text_candidate(text: str) -> bool:
    """短い単独注記を symbol_text として扱うか判定する。"""
    stripped = text.strip()
    return bool(stripped) and "\n" not in stripped and "\r" not in stripped and len(stripped) <= SYMBOL_TEXT_MAX_CHARS


def _scene_rect_from_item_rect(item: QGraphicsItem, rect: QRectF) -> QRectF:
    mapped = item.mapRectToScene(rect)
    return mapped.boundingRect() if hasattr(mapped, "boundingRect") else mapped


def render_order_sheet_background(pdf_bytes: bytes, zoom: float = 2.0) -> QPixmap | None:
    """指図書(1)PDFの1ページ目を背景用QPixmapへレンダリングする。

    PyMuPDF が無い場合は None を返し、呼び出し側で白背景にフォールバックする。
    """
    if not pdf_bytes:
        return None
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)
        scale_x = PAGE_W / float(pix.width) if pix.width else 0.0
        scale_y = PAGE_H / float(pix.height) if pix.height else 0.0
        _log.debug(
            "voucher_edit_background page.rect.width=%s page.rect.height=%s "
            "PAGE_W=%s PAGE_H=%s pixmap.width=%s pixmap.height=%s scale_x=%s scale_y=%s",
            page_w, page_h, PAGE_W, PAGE_H, pix.width, pix.height, scale_x, scale_y,
        )
        if abs(page_w - PAGE_W) > 0.1 or abs(page_h - PAGE_H) > 0.1:
            _log.warning(
                "背景PDFの実ページサイズがPAGE_W/PAGE_Hと異なります: "
                "page.rect.width=%s page.rect.height=%s PAGE_W=%s PAGE_H=%s",
                page_w, page_h, PAGE_W, PAGE_H,
            )
        image = QImage(pix.samples, pix.width, pix.height, pix.stride,
                       QImage.Format.Format_RGB888)
        return QPixmap.fromImage(image.copy())
    except Exception:
        return None


# ── 編集アイテム ──────────────────────────────────────────────────────────────

class _EditTextItem(QGraphicsTextItem):
    """ドラッグ矩形で作成するテキストボックス。ダブルクリックで文字編集。"""

    def __init__(self, text: str = "", obj_id: str | None = None,
                 font_size: float = DEFAULT_FONT_SIZE,
                 box_w: float = MIN_TEXT_W, box_h: float = MIN_TEXT_H,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 line_width: float = DEFAULT_LINE_WIDTH,
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 fill_color: str | None = None,
                 text_align: str = "left",
                 vertical_align: str = "top",
                 auto_fit: bool = True,
                 manual_resized: bool = False) -> None:
        super().__init__(text)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.font_family = resolve_text_font_family(font_family)
        self.font_size = float(font_size)
        self.font_bold = bool(font_bold)
        self.font_italic = bool(font_italic)
        self.text_color = _color_name(text_color)
        self.line_width = float(line_width)
        self.stroke_color = _color_name(stroke_color)
        self.fill_color = fill_color
        self.text_align = _normalize_text_align(text_align)
        self.vertical_align = _normalize_vertical_align(vertical_align)
        self.auto_fit = bool(auto_fit)
        self.manual_resized = bool(manual_resized)
        self.box_w = float(box_w)
        self.box_h = float(box_h)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic))
        _configure_text_document(self)
        _apply_text_alignment(self, self.text_align)
        self.setDefaultTextColor(QColor(self.text_color))
        self.setTextWidth(self.box_w)
        self.document().contentsChanged.connect(self._refresh_text_layout)
        self.setData(_DATA_TYPE, "text")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.fit_to_text_if_needed()

    def apply_font_size(self, font_size: float) -> None:
        self.font_size = float(font_size)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic))
        self.fit_to_text_if_needed()
        self._refresh_text_layout()

    def fit_to_text_if_needed(self, force: bool = False) -> None:
        if not force and (not self.auto_fit or self.manual_resized):
            return
        if not self.toPlainText().strip():
            return
        text_w, text_h = _text_content_size(self.toPlainText(), self.font(), self.font_size)
        new_w = max(text_w + 1.0, MIN_TEXT_W)
        new_h = max(text_h, self.font_size * 1.2, MIN_TEXT_H)
        if abs(new_w - self.box_w) > 0.1 or abs(new_h - self.box_h) > 0.1:
            self.prepareGeometryChange()
            self.box_w = float(new_w)
            self.box_h = float(new_h)
            self.setTextWidth(self.box_w)
            self._refresh_text_layout()

    def _vertical_text_offset(self) -> float:
        text_h = _text_document_height(self)
        if self.vertical_align == "top":
            return 0.0
        if self.vertical_align == "bottom":
            return max(self.box_h - text_h, 0.0)
        return max((self.box_h - text_h) / 2.0, 0.0)

    def _refresh_text_layout(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        # 選択表示・ヒットテスト用。保存座標は sceneBoundingRect() ではなく
        # box_rect_scene() の保持矩形を使う。
        text_rect = super().boundingRect().translated(0.0, self._vertical_text_offset())
        return text_rect.united(QRectF(0.0, 0.0, self.box_w, self.box_h))

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        painter.save()
        painter.translate(0.0, self._vertical_text_offset())
        super().paint(painter, option, widget)
        painter.restore()

    def set_box_size(self, width: float, height: float) -> None:
        self.auto_fit = False
        self.manual_resized = True
        self.prepareGeometryChange()
        self.box_w = float(width)
        self.box_h = float(height)
        self.setTextWidth(self.box_w)
        self._refresh_text_layout()

    def set_manual_box_size(self, width: float, height: float) -> None:
        self.set_box_size(width, height)

    def box_rect_scene(self) -> QRectF:
        return _scene_rect_from_item_rect(self, QRectF(0.0, 0.0, self.box_w, self.box_h))

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # 編集を抜けたら通常選択モードへ戻し、勝手に入力状態にならないようにする。
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        super().focusOutEvent(event)
        self.fit_to_text_if_needed()
        scene = self.scene()
        if scene is not None and hasattr(scene, "_window"):
            window = scene._window
            # 空文字（空白のみ）の単独テキストボックスは残さない（要件3）。
            if not self.toPlainText().strip():
                window.remove_text_item(self)
            elif window.maybe_convert_text_item_to_symbol(self):
                return
            window.commit_history()

    def serialize_edit_object(self) -> dict[str, Any]:
        self.fit_to_text_if_needed()
        rect = self.box_rect_scene()
        x = float(rect.x())
        scene_top = float(rect.y())
        w = float(rect.width())
        h = float(rect.height())
        return {
            "id": self.obj_id,
            "type": "text",
            "x": x,
            "y": scene_top,
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "width": w,
            "height": h,
            "w": w,
            "h": h,
            "text": self.toPlainText(),
            "font_family": self.font_family,
            "font_size": self.font_size,
            "font_bold": self.font_bold,
            "font_italic": self.font_italic,
            "text_color": self.text_color,
            "text_align": self.text_align,
            "vertical_align": self.vertical_align,
            "line_width": self.line_width,
            "stroke_color": self.stroke_color,
            "fill_color": self.fill_color,
            "auto_fit": self.auto_fit,
            "manual_resized": self.manual_resized,
            "color": list(DEFAULT_COLOR),
        }


class _EditSymbolTextItem(QGraphicsSimpleTextItem):
    """短い注記用の点アンカーテキスト。scene座標の中心点を保存する。"""

    def __init__(self, text: str = "", obj_id: str | None = None,
                 font_size: float = DEFAULT_FONT_SIZE,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 anchor: str = "center") -> None:
        super().__init__(text.strip())
        self.obj_id = obj_id or str(uuid.uuid4())
        self.font_family = resolve_text_font_family(font_family)
        self.font_size = float(font_size)
        self.font_bold = bool(font_bold)
        self.font_italic = bool(font_italic)
        self.text_color = _color_name(text_color)
        self.anchor = anchor if anchor == "center" else "center"
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic))
        self.setBrush(QBrush(QColor(self.text_color)))
        self.setData(_DATA_TYPE, "symbol_text")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

    def apply_font_size(self, font_size: float) -> None:
        center = self.anchor_scene_pos()
        self.font_size = float(font_size)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic))
        self.set_anchor_scene_pos(center)

    def anchor_scene_pos(self) -> QPointF:
        return self.sceneBoundingRect().center()

    def set_anchor_scene_pos(self, pos: QPointF) -> None:
        rect = self.boundingRect()
        self.setPos(pos.x() - rect.width() / 2.0, pos.y() - rect.height() / 2.0)

    def serialize_edit_object(self) -> dict[str, Any]:
        center = self.anchor_scene_pos()
        return {
            "id": self.obj_id,
            "type": "symbol_text",
            "x": float(center.x()),
            "y": float(center.y()),
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "text": self.text(),
            "font_family": self.font_family,
            "font_size": self.font_size,
            "font_bold": self.font_bold,
            "font_italic": self.font_italic,
            "text_color": self.text_color,
            "anchor": self.anchor,
            "color": list(DEFAULT_COLOR),
        }


class _ShapeInnerText(QGraphicsTextItem):
    """四角形・楕円の内部テキスト。編集終了時に履歴へコミットする。"""

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # 編集を抜けたらマウスを透過させ、クリックを親図形へ通す（選択・移動: 要件6・7）。
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        super().focusOutEvent(event)
        scene = self.scene()
        if scene is not None and hasattr(scene, "_window"):
            scene._window.commit_history()


class _ShapeTextMixin:
    """四角形・楕円が共通で持つ内部テキスト（中央寄せ・中央表示）の処理。"""

    def _init_shape_text(self, text: str, font_size: float,
                         font_family: str | None = None,
                         font_bold: bool = False, font_italic: bool = False,
                         text_color: str = DEFAULT_TEXT_COLOR,
                         text_align: str = "center",
                         vertical_align: str = "middle") -> None:
        self.font_family = resolve_text_font_family(font_family)
        self.font_size = float(font_size)
        self.font_bold = bool(font_bold)
        self.font_italic = bool(font_italic)
        self.text_color = _color_name(text_color)
        self.text_align = _normalize_text_align(text_align)
        self.vertical_align = _normalize_vertical_align(vertical_align)
        self._text = _ShapeInnerText(text, self)  # type: ignore[arg-type]
        self._text.setFont(make_text_font(self.font_size, self.font_family,
                                          self.font_bold, self.font_italic))
        _configure_text_document(self._text)
        self._text.setDefaultTextColor(QColor(self.text_color))
        self._text.setData(_DATA_TYPE, "_shape_text")
        # 通常時はマウスを透過させ、図形本体（親）が選択・移動を受け取る（要件6・7）。
        # 編集開始時のみ AllButtons に戻してカーソル操作できるようにする。
        self._text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        _apply_text_alignment(self._text, self.text_align)
        self._reposition_text()

    def _reposition_text(self) -> None:
        r = self.rect()  # type: ignore[attr-defined]
        text_w = max(r.width() - RECT_TEXT_PAD * 2, 1.0)
        self._text.setTextWidth(text_w)
        text_h = _text_document_height(self._text)
        tx = r.left() + RECT_TEXT_PAD
        if self.vertical_align == "top":
            ty = r.top()
        elif self.vertical_align == "bottom":
            ty = r.bottom() - text_h
        else:
            ty = r.top() + (r.height() - text_h) / 2.0
        self._text.setPos(tx, ty)

    def apply_font_size(self, font_size: float) -> None:
        self.font_size = float(font_size)
        self._text.setFont(make_text_font(self.font_size, self.font_family,
                                          self.font_bold, self.font_italic))
        self._reposition_text()

    def inner_text(self) -> str:
        return self._text.toPlainText()

    def edit_inner_text(self) -> None:
        # 編集中はマウスを受け取り、クリックでカーソル位置を変えられるようにする。
        self._text.setAcceptedMouseButtons(Qt.MouseButton.AllButtons)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self._text.setFocus(Qt.FocusReason.MouseFocusReason)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.edit_inner_text()
        super().mouseDoubleClickEvent(event)  # type: ignore[misc]

    def apply_line_width(self, line_width: float) -> None:
        self.line_width = float(line_width)
        pen = QPen(QColor(self.stroke_color))
        pen.setWidthF(self.line_width)
        pen.setCosmetic(True)
        self.setPen(pen)  # type: ignore[attr-defined]


class _EditRectItem(_ShapeTextMixin, QGraphicsRectItem):
    """ドラッグ矩形で作成する四角形。内部テキストを子アイテムとして持てる。"""

    def __init__(self, rect: QRectF, obj_id: str | None = None,
                 line_width: float = DEFAULT_LINE_WIDTH,
                 text: str = "", font_size: float = DEFAULT_FONT_SIZE,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 fill_color: str | None = None,
                 text_align: str = "center",
                 vertical_align: str = "middle") -> None:
        super().__init__(rect)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.line_width = float(line_width)
        self.stroke_color = _color_name(stroke_color)
        self.fill_color = fill_color
        self.setData(_DATA_TYPE, "rectangle")
        self.apply_line_width(self.line_width)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self._init_shape_text(text, font_size, font_family, font_bold,
                              font_italic, text_color, text_align,
                              vertical_align)

    def setRect(self, *args) -> None:  # noqa: N802
        super().setRect(*args)
        self._reposition_text()

    def serialize_edit_object(self) -> dict[str, Any]:
        rect = _scene_rect_from_item_rect(self, self.rect())
        return {
            "id": self.obj_id,
            "type": "rectangle",
            "x": float(rect.left()),
            "y": float(rect.top()),
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "width": float(rect.width()),
            "height": float(rect.height()),
            "w": float(rect.width()),
            "h": float(rect.height()),
            "line_width": self.line_width,
            "stroke_color": self.stroke_color,
            "fill_color": self.fill_color,
            "text": self.inner_text(),
            "font_family": self.font_family,
            "font_size": self.font_size,
            "font_bold": self.font_bold,
            "font_italic": self.font_italic,
            "text_color": self.text_color,
            "text_align": self.text_align,
            "vertical_align": self.vertical_align,
            "color": list(DEFAULT_COLOR),
        }


class _EditEllipseItem(_ShapeTextMixin, QGraphicsEllipseItem):
    """ドラッグ矩形で作成する丸/楕円。内部テキストを子アイテムとして持てる。"""

    def __init__(self, rect: QRectF, obj_id: str | None = None,
                 line_width: float = DEFAULT_LINE_WIDTH,
                 text: str = "", font_size: float = DEFAULT_FONT_SIZE,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 fill_color: str | None = None,
                 text_align: str = "center",
                 vertical_align: str = "middle") -> None:
        super().__init__(rect)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.line_width = float(line_width)
        self.stroke_color = _color_name(stroke_color)
        self.fill_color = fill_color
        self.setData(_DATA_TYPE, "ellipse")
        self.apply_line_width(self.line_width)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self._init_shape_text(text, font_size, font_family, font_bold,
                              font_italic, text_color, text_align,
                              vertical_align)

    def setRect(self, *args) -> None:  # noqa: N802
        super().setRect(*args)
        self._reposition_text()

    def serialize_edit_object(self) -> dict[str, Any]:
        rect = _scene_rect_from_item_rect(self, self.rect())
        return {
            "id": self.obj_id,
            "type": "ellipse",
            "x": float(rect.left()),
            "y": float(rect.top()),
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "width": float(rect.width()),
            "height": float(rect.height()),
            "w": float(rect.width()),
            "h": float(rect.height()),
            "line_width": self.line_width,
            "stroke_color": self.stroke_color,
            "fill_color": self.fill_color,
            "text": self.inner_text(),
            "font_family": self.font_family,
            "font_size": self.font_size,
            "font_bold": self.font_bold,
            "font_italic": self.font_italic,
            "text_color": self.text_color,
            "text_align": self.text_align,
            "vertical_align": self.vertical_align,
            "color": list(DEFAULT_COLOR),
        }


class _EditLineItem(QGraphicsLineItem):
    """ドラッグ始点〜終点で作成する直線。"""

    def __init__(self, x1: float, y1: float, x2: float, y2: float,
                 obj_id: str | None = None,
                 line_width: float = DEFAULT_LINE_WIDTH,
                 stroke_color: str = DEFAULT_STROKE_COLOR) -> None:
        super().__init__(x1, y1, x2, y2)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.line_width = float(line_width)
        self.stroke_color = _color_name(stroke_color)
        self.setData(_DATA_TYPE, "line")
        self.apply_line_width(self.line_width)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

    def apply_line_width(self, line_width: float) -> None:
        self.line_width = float(line_width)
        pen = QPen(QColor(self.stroke_color))
        pen.setWidthF(self.line_width)
        pen.setCosmetic(True)
        self.setPen(pen)

    def serialize_edit_object(self) -> dict[str, Any]:
        ln = self.line()
        p1 = self.mapToScene(ln.p1())
        p2 = self.mapToScene(ln.p2())
        return {
            "id": self.obj_id,
            "type": "line",
            "x1": float(p1.x()), "y1": float(p1.y()),
            "x2": float(p2.x()), "y2": float(p2.y()),
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "line_width": self.line_width,
            "stroke_color": self.stroke_color,
            "color": list(DEFAULT_COLOR),
        }


def qimage_to_png_bytes(image: QImage) -> bytes:
    """QImage を PNG バイト列へ変換する（画像挿入・貼り付けの保存用: 要件2-3・2-4）。"""
    if image.isNull():
        return b""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


class _EditImageItem(QGraphicsPixmapItem):
    """挿入/貼り付けした画像。移動・サイズ変更・保存に対応する（要件2-3・2-4）。

    画像実体は PNG バイト列で保持し、保存時に base64 化する。ファイルパスは
    保持しない（元ファイル消失でも復元できるようにするため）。
    """

    def __init__(self, image_bytes: bytes, image_format: str = "png",
                 obj_id: str | None = None,
                 width: float | None = None, height: float | None = None) -> None:
        super().__init__()
        self.obj_id = obj_id or str(uuid.uuid4())
        self.image_bytes = bytes(image_bytes)
        self.image_format = (image_format or "png").lower()
        pixmap = QPixmap()
        if self.image_bytes:
            pixmap.loadFromData(self.image_bytes)
        self._pixmap = pixmap
        self.setPixmap(pixmap)
        natural_w = float(pixmap.width()) or MIN_TEXT_W
        natural_h = float(pixmap.height()) or MIN_TEXT_H
        self.box_w = float(width) if width else natural_w
        self.box_h = float(height) if height else natural_h
        self.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.setData(_DATA_TYPE, "image")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self._apply_scale()

    def _apply_scale(self) -> None:
        pw = max(float(self._pixmap.width()), 1.0)
        ph = max(float(self._pixmap.height()), 1.0)
        self.prepareGeometryChange()
        self.setTransform(QTransform().scale(self.box_w / pw, self.box_h / ph))

    def set_box_size(self, width: float, height: float) -> None:
        self.box_w = max(float(width), MIN_RESIZE)
        self.box_h = max(float(height), MIN_RESIZE)
        self._apply_scale()

    def set_manual_box_size(self, width: float, height: float) -> None:
        self.set_box_size(width, height)

    def box_rect_scene(self) -> QRectF:
        pw = max(float(self._pixmap.width()), 1.0)
        ph = max(float(self._pixmap.height()), 1.0)
        return _scene_rect_from_item_rect(self, QRectF(0.0, 0.0, pw, ph))

    def serialize_edit_object(self) -> dict[str, Any]:
        rect = self.box_rect_scene()
        return {
            "id": self.obj_id,
            "type": "image",
            "x": float(rect.x()),
            "y": float(rect.y()),
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "width": float(rect.width()),
            "height": float(rect.height()),
            "w": float(rect.width()),
            "h": float(rect.height()),
            "image_data": base64.b64encode(self.image_bytes).decode("ascii"),
            "image_format": self.image_format,
            "color": list(DEFAULT_COLOR),
        }


def _commit_handle_resize(handle) -> None:
    """ハンドル操作（リサイズ/端点移動）の確定処理（要件: dirty/履歴/ハンドル再配置）。

    対象オブジェクトのサイズ変更後に未保存フラグを立て、Undo/Redo 用の履歴へ積み、
    選択中オブジェクトに追従するようハンドルを作り直す。
    """
    scene = handle.scene()
    window = getattr(scene, "_window", None) if scene is not None else None
    if window is None:
        return
    window.mark_dirty()
    window.commit_history()
    # refresh_handles は古いハンドル（＝この handle）を破棄して作り直すため最後に呼ぶ。
    window.refresh_handles()


class _ResizeHandle(QGraphicsRectItem):
    """選択中アイテムの右下に表示するサイズ変更ハンドル。

    見た目は HANDLE_SIZE（小）だが、クリック判定は HANDLE_HIT_SIZE（大）にして
    掴みやすくする。boundingRect()/shape() を拡大判定にすることで、押下時に
    画像本体ではなくハンドルがマウスイベントを確実に受け取る（不具合1）。
    """

    SIZE = HANDLE_SIZE
    # 補助アイテムの目印。背景/編集オブジェクトと区別し、保存・全選択対象外にする。
    _IS_HELPER = True

    def __init__(self, target) -> None:
        s = HANDLE_SIZE
        super().__init__(-s / 2, -s / 2, s, s)
        self._target = target
        self._suppress = False
        # リサイズ中フラグと、対象が元々移動可能だったか（解放時に戻すため）。
        self._resizing = False
        self._target_was_movable = True
        self.setBrush(QBrush(QColor(0, 120, 215)))
        self.setPen(QPen(QColor(255, 255, 255)))
        # 対象オブジェクト（ZValue=0前後）・背景（-100）より確実に前面へ出す。
        self.setZValue(10000)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        # ハンドルは左ドラッグで掴めるよう、左ボタンのマウスイベントを受け取る。
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.reposition()

    def boundingRect(self) -> QRectF:  # noqa: N802
        # 描画は小さな矩形だが、再描画・当たり判定領域は拡大判定にする。
        h = HANDLE_HIT_SIZE
        return QRectF(-h / 2, -h / 2, h, h)

    def shape(self) -> QPainterPath:  # noqa: N802
        # クリック判定を拡大し、画像本体よりハンドルを優先で掴めるようにする。
        path = QPainterPath()
        path.addRect(QRectF(-HANDLE_HIT_SIZE / 2, -HANDLE_HIT_SIZE / 2,
                            HANDLE_HIT_SIZE, HANDLE_HIT_SIZE))
        return path

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        # 見た目だけは小さな青い四角を描く（当たり判定は shape() の拡大矩形）。
        painter.setBrush(self.brush())
        painter.setPen(self.pen())
        s = HANDLE_SIZE
        painter.drawRect(QRectF(-s / 2, -s / 2, s, s))

    def reposition(self) -> None:
        if isinstance(self._target, (_EditTextItem, _EditImageItem)):
            br = self._target.box_rect_scene()
        elif isinstance(self._target, (_EditRectItem, _EditEllipseItem)):
            br = _scene_rect_from_item_rect(self._target, self._target.rect())
        else:
            br = self._target.sceneBoundingRect()
        self._suppress = True
        self.setPos(br.right(), br.bottom())
        self._suppress = False

    def itemChange(self, change, value):  # noqa: N802
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionChange
                and not self._suppress and self.scene() is not None):
            self._resize_target(value)
        return super().itemChange(change, value)

    def _resize_target(self, handle_scene_pos: QPointF) -> None:
        tgt = self._target
        if isinstance(tgt, (_EditRectItem, _EditEllipseItem)):
            top_left = tgt.mapRectToScene(tgt.rect()).topLeft()
            w = max(handle_scene_pos.x() - top_left.x(), MIN_RESIZE)
            h = max(handle_scene_pos.y() - top_left.y(), MIN_RESIZE)
            local_tl = tgt.mapFromScene(top_left)
            tgt.setRect(QRectF(local_tl.x(), local_tl.y(), w, h))
        elif isinstance(tgt, _EditTextItem):
            top_left = tgt.box_rect_scene().topLeft()
            w = max(handle_scene_pos.x() - top_left.x(), MIN_TEXT_W)
            h = max(handle_scene_pos.y() - top_left.y(), MIN_TEXT_H)
            tgt.set_manual_box_size(w, h)
        elif isinstance(tgt, _EditImageItem):
            top_left = tgt.box_rect_scene().topLeft()
            w = max(handle_scene_pos.x() - top_left.x(), MIN_RESIZE)
            h = max(handle_scene_pos.y() - top_left.y(), MIN_RESIZE)
            tgt.set_box_size(w, h)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # ハンドル押下中は対象オブジェクトの移動を止める（リサイズ中に本体が
        # 動かないようにする: 不具合1）。event.accept() で scene/本体側へ
        # 移動処理として流さない。
        self._resizing = True
        self._target_was_movable = bool(
            self._target.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self._target.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        super().mousePressEvent(event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        # 対象の移動可否を元へ戻し、確定処理（dirty/履歴/ハンドル再配置）を行う。
        self._target.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,
                             self._target_was_movable)
        self._resizing = False
        _commit_handle_resize(self)
        event.accept()


class _LineEndHandle(QGraphicsRectItem):
    """選択中の線の端点（始点/終点）を動かすハンドル。

    見た目は HANDLE_SIZE（小）、クリック判定は HANDLE_HIT_SIZE（大）にして、
    線本体ではなく端点ハンドルを確実に掴めるようにする（不具合1）。
    """

    SIZE = HANDLE_SIZE
    # 補助アイテムの目印。背景/編集オブジェクトと区別し、保存・全選択対象外にする。
    _IS_HELPER = True

    def __init__(self, target: _EditLineItem, which: str) -> None:
        s = HANDLE_SIZE
        super().__init__(-s / 2, -s / 2, s, s)
        self._target = target
        self._which = which  # "p1" | "p2"
        self._suppress = False
        self._resizing = False
        self._target_was_movable = True
        self.setBrush(QBrush(QColor(0, 120, 215)))
        self.setPen(QPen(QColor(255, 255, 255)))
        # 対象の線（ZValue=0前後）・背景（-100）より確実に前面へ出す。
        self.setZValue(10000)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        # ハンドルは左ドラッグで掴めるよう、左ボタンのマウスイベントを受け取る。
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.reposition()

    def boundingRect(self) -> QRectF:  # noqa: N802
        h = HANDLE_HIT_SIZE
        return QRectF(-h / 2, -h / 2, h, h)

    def shape(self) -> QPainterPath:  # noqa: N802
        path = QPainterPath()
        path.addRect(QRectF(-HANDLE_HIT_SIZE / 2, -HANDLE_HIT_SIZE / 2,
                            HANDLE_HIT_SIZE, HANDLE_HIT_SIZE))
        return path

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        painter.setBrush(self.brush())
        painter.setPen(self.pen())
        s = HANDLE_SIZE
        painter.drawRect(QRectF(-s / 2, -s / 2, s, s))

    def reposition(self) -> None:
        ln = self._target.line()
        pt = ln.p1() if self._which == "p1" else ln.p2()
        scene_pt = self._target.mapToScene(pt)
        self._suppress = True
        self.setPos(scene_pt)
        self._suppress = False

    def itemChange(self, change, value):  # noqa: N802
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionChange
                and not self._suppress and self.scene() is not None):
            tgt = self._target
            local = tgt.mapFromScene(value)
            ln = tgt.line()
            if self._which == "p1":
                tgt.setLine(local.x(), local.y(), ln.x2(), ln.y2())
            else:
                tgt.setLine(ln.x1(), ln.y1(), local.x(), local.y())
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 端点ドラッグ中は線本体の移動を止める（不具合1）。
        self._resizing = True
        self._target_was_movable = bool(
            self._target.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self._target.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        super().mousePressEvent(event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        self._target.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,
                             self._target_was_movable)
        self._resizing = False
        _commit_handle_resize(self)
        event.accept()


class _EditScene(QGraphicsScene):
    """ツールモードに応じてオブジェクトを描画する編集シーン。"""

    def __init__(self, window: "VoucherEditWindow") -> None:
        super().__init__(0, 0, PAGE_W, PAGE_H)
        self.setSceneRect(0, 0, PAGE_W, PAGE_H)
        self._window = window
        self._temp_item: QGraphicsItem | None = None
        self._start: QPointF | None = None
        self._select_snapshot: list[dict[str, Any]] | None = None
        # 押下時にクリックした編集オブジェクトと複数選択フラグ。解放時の単一選択担保に使う。
        self._press_target: QGraphicsItem | None = None
        self._press_multi: bool = False
        self._press_pos: QPointF | None = None
        # 押下位置がリサイズ/端点ハンドル上だったか。True の間は選択解除しない
        # （解除するとドラッグ中のハンドルが撤去されてリサイズできなくなる）。
        self._press_handle: bool = False

    @staticmethod
    def _manhattan_distance(a: QPointF, b: QPointF) -> float:
        """QPointF 差分のマンハッタン距離を PySide 差異に依存せず返す。"""
        return abs(a.x() - b.x()) + abs(a.y() - b.y())

    def _hits_existing_object(self, pos: QPointF) -> bool:
        """指定位置の最前面アイテムが既存編集オブジェクト/ハンドルか判定する。

        背景PDF・白背景・何も無い場合のみ False（＝新規作成可）を返す。図形ツール
        選択中でも、既存オブジェクト上の操作は選択・移動・サイズ変更を優先する
        （要件7）。
        """
        from PySide6.QtGui import QTransform

        item = self.itemAt(pos, QTransform())
        while item is not None:
            if isinstance(item, (_ResizeHandle, _LineEndHandle)):
                return True
            if hasattr(item, "serialize_edit_object"):
                return True
            if item.data(_DATA_TYPE) == "_shape_text":
                return True
            item = item.parentItem()
        return False

    def _press_on_handle(self, pos: QPointF) -> bool:
        """押下位置が表示中のリサイズ/端点ハンドル上か判定する。

        itemAt を使わず既知のハンドル一覧で判定する。余分な itemAt 呼び出しが
        PySide のオブジェクト管理と相互作用して既存オブジェクトを巻き込み削除する
        ことがあるため、ハンドル矩形の当たり判定で安全に確認する。
        """
        for h in self._window._handles:
            if (isinstance(h, (_ResizeHandle, _LineEndHandle))
                    and h.scene() is self
                    and h.sceneBoundingRect().contains(pos)):
                return True
        return False

    def _resolve_edit_object(self, pos: QPointF):
        """指定位置の編集オブジェクトを返す（子の内部テキストは親図形へ解決）。

        リサイズ/端点ハンドル上なら None を返し、ハンドル操作（リサイズ）を妨げない。
        四角・丸の内部テキスト子をクリックしても親図形を1つだけ選択できるようにする
        （要件6・7）。
        """
        from PySide6.QtGui import QTransform

        item = self.itemAt(pos, QTransform())
        while item is not None:
            if isinstance(item, (_ResizeHandle, _LineEndHandle)):
                return None
            if hasattr(item, "serialize_edit_object"):
                return item
            item = item.parentItem()
        return None

    def cancel_temp_item(self) -> None:
        """作成中の一時オブジェクト（プレビュー）を破棄する（Esc用: 要件8）。"""
        if self._temp_item is not None:
            if self._temp_item.scene() is not None:
                self.removeItem(self._temp_item)
            self._temp_item = None
            self._start = None
        self._press_target = None
        self._press_multi = False
        self._press_pos = None
        self._press_handle = False

    def mousePressEvent(self, event) -> None:
        tool = self._window.current_tool
        pos = event.scenePos()
        if (tool in (TOOL_TEXT, TOOL_LINE, TOOL_RECT, TOOL_ELLIPSE)
                and event.button() == Qt.MouseButton.LeftButton
                and not self._hits_existing_object(pos)):
            # 空白部分での新規作成。既存選択は解除し、対象を絞る（要件6・7・10）。
            self.clearSelection()
            self._start = pos
            if tool == TOOL_LINE:
                item = QGraphicsLineItem(pos.x(), pos.y(), pos.x(), pos.y())
                item.setPen(self._window.current_pen())
            else:
                # テキスト/図形は破線プレビュー矩形でドラッグ範囲を示す。
                item = QGraphicsRectItem(QRectF(pos, pos))
                pen = QPen(QColor(0, 120, 215))
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                item.setPen(pen)
            # 一時アイテムは preview レイヤー扱い。保存・全選択・削除の対象外（要件2・11）。
            item._IS_PREVIEW = True  # type: ignore[attr-defined]
            item.setZValue(900)
            self.addItem(item)
            self._temp_item = item
            self._press_target = None
            self._press_multi = False
            self._press_pos = pos
            # 背景の上に一時アイテムだけを重ねる。背景は絶対に消さない（要件1・2・4）。
            self._window.ensure_background_visible()
            self._window._debug_state("insert-start")
            event.accept()
            return
        # ここに来たのは「選択ツール」または「図形ツール選択中に既存オブジェクト上を
        # 操作した」場合。移動/サイズ変更前の状態を記録し、解放時に変化があれば履歴へ。
        self._select_snapshot = self._window.serialize_objects()
        # リサイズ/端点ハンドル上での押下か。ハンドル操作時は選択を維持する。
        self._press_handle = self._press_on_handle(pos)
        target = self._resolve_edit_object(pos)
        multi = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                          | Qt.KeyboardModifier.ShiftModifier))
        self._press_target = (
            target if event.button() == Qt.MouseButton.LeftButton else None)
        self._press_multi = multi
        self._press_pos = pos
        super().mousePressEvent(event)
        # クリック対象を明示的に単一選択する。内部テキスト子で選択が外れる・
        # 複数オブジェクトが選択されたままになる問題を防ぐ（要件6・7・10）。
        self._apply_click_selection()
        # 空白部分を修飾キー無しでクリックしたら選択解除（要件6）。
        # 解放時には解除しない（ラバーバンド選択の結果を保持するため）。
        # ハンドル押下時は解除しない。解除するとドラッグ中のハンドルが
        # _on_selection_changed → _remove_handles() で撤去され、リサイズできなくなる。
        if (self._press_target is None and not multi and not self._press_handle
                and event.button() == Qt.MouseButton.LeftButton):
            self.clearSelection()
        self._window.ensure_background_visible()

    def _apply_click_selection(self) -> None:
        """押下したオブジェクトを単一選択（Ctrl/Shift時は追加選択）にする。

        QGraphicsScene の grabber/子テキストの影響で選択が押下対象とズレることが
        あるため、押下時・解放時の双方でこの確定処理を呼ぶ（要件6・7・10）。
        """
        target = self._press_target
        if target is None or target.scene() is None:
            return
        if self._press_multi:
            target.setSelected(True)
        else:
            self._window._select_only(target)

    def mouseMoveEvent(self, event) -> None:
        if self._temp_item is not None and self._start is not None:
            pos = event.scenePos()
            if isinstance(self._temp_item, QGraphicsLineItem):
                self._temp_item.setLine(self._start.x(), self._start.y(), pos.x(), pos.y())
            else:
                self._temp_item.setRect(QRectF(self._start, pos).normalized())
            # ドラッグ中も背景を維持する（要件1・2・4）。
            self._window.ensure_background_visible()
            event.accept()
            return
        super().mouseMoveEvent(event)
        self._window.ensure_background_visible()

    def mouseReleaseEvent(self, event) -> None:
        if self._temp_item is not None and self._start is not None:
            temp = self._temp_item
            start = self._start
            end = event.scenePos()
            self.removeItem(temp)
            self._temp_item = None
            self._start = None
            tool = self._window.current_tool
            created = False
            if tool == TOOL_LINE:
                if self._manhattan_distance(start, end) >= 2.0:
                    self._window.add_line(start, end)
                    created = True
            elif tool == TOOL_TEXT:
                self._window.add_text_rect(QRectF(start, end).normalized())
                created = True
            elif tool == TOOL_RECT:
                rect = QRectF(start, end).normalized()
                if rect.width() >= 2.0 or rect.height() >= 2.0:
                    self._window.add_rect(rect, auto_edit=True)
                    created = True
            elif tool == TOOL_ELLIPSE:
                rect = QRectF(start, end).normalized()
                if rect.width() >= 2.0 or rect.height() >= 2.0:
                    self._window.add_ellipse(rect, auto_edit=True)
                    created = True
            if created:
                self._window.commit_history()
            # 一時アイテムを正式オブジェクトへ変換後も背景を維持する（要件1・2・4）。
            self._window.ensure_background_visible()
            self._window._debug_state("insert-complete")
            # 連続挿入のためツールは切り替えない（要件12）。
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # 解放時にも押下対象の単一選択を確定する（grabber 由来の選択ズレ対策: 要件6・7）。
        self._apply_click_selection()
        # 空白部分を「ドラッグせずに」クリックしたら選択解除（ラバーバンドは保持: 要件6）。
        # ハンドル押下時はリサイズ確定のため選択を維持する。
        if (self._press_target is None and not self._press_multi
                and not self._press_handle
                and self._press_pos is not None
                and self._manhattan_distance(self._press_pos, event.scenePos()) < 3.0):
            self.clearSelection()
        self._press_target = None
        self._press_multi = False
        self._press_pos = None
        self._press_handle = False
        if self._select_snapshot is not None:
            after = self._window.serialize_objects()
            if after != self._select_snapshot:
                self._window.commit_history()
            self._select_snapshot = None
            self._window.refresh_handles()
        self._window.ensure_background_visible()


class _EditGraphicsView(QGraphicsView):
    """Ctrl+V をウィンドウへ確実に届ける編集用ビュー（要件2-4）。

    キーイベントはフォーカスのある QGraphicsView に届くため、ウィンドウ側の
    keyPressEvent だけでは Ctrl+V を取りこぼすことがある。ビューでも Paste を
    拾い、ウィンドウの handle_paste_shortcut() へ委譲する。テキスト編集中は
    handle_paste_shortcut() が False を返すので、通常のテキスト貼り付けに委ねる。
    """

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Paste):
            window = self.window()
            handler = getattr(window, "handle_paste_shortcut", None)
            if callable(handler) and handler():
                event.accept()
                return
        super().keyPressEvent(event)


class VoucherEditWindow(QMainWindow):
    """指図書(1)を背景に図形・テキストを編集する全画面ウィンドウ。"""

    def __init__(self, order_no: str, background_pdf_bytes: bytes = b"",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.order_no = order_no
        # 初期ツールは「テキスト」。開いてすぐドラッグでテキストボックスを作れる（要件2）。
        self.current_tool = TOOL_TEXT
        self.current_font_size = DEFAULT_FONT_SIZE
        self.current_line_width = DEFAULT_LINE_WIDTH
        self.loaded_object_ids: set[str] = set()
        self._handles: list[QGraphicsItem] = []
        # 背景アイテムへの参照を保持する。scene 全走査だけでなくリストでも管理する（要件3）。
        self._background_items: list[QGraphicsItem] = []
        self._tool_actions: dict[str, Any] = {}
        # Undo/Redo 用のスナップショット履歴（要件1・3）。
        self._history: list[list[dict[str, Any]]] = []
        self._history_index: int = -1
        # 履歴復元中フラグ。復元中の commit_history を抑止しRedo履歴を守る（要件1）。
        self._is_restoring_history = False
        self._updating_property_ui = False
        # 未保存変更フラグ（要件3）。閉じる時に確認ダイアログを出すため使う。
        self._dirty = False
        self.setWindowTitle(f"指図書編集 — 受注No {order_no}")

        self._scene = _EditScene(self)
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self._view = _EditGraphicsView(self._scene)
        self._view.setRenderHints(self._view.renderHints())
        # 編集領域の余白を最小化し、プレビューを枠いっぱいに使う（要件2）。
        from PySide6.QtWidgets import QFrame

        self._view.setFrameShape(QFrame.Shape.NoFrame)
        self._view.setViewportMargins(0, 0, 0, 0)
        self._view.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(self._view)

        self._add_background(background_pdf_bytes)
        self._build_toolbar()
        self._install_shortcuts()
        self.load_edit_layer()
        # 初期読み込み完了時点は未保存変更なしとする。
        self._dirty = False
        self._debug_state("open")

    # ── 未保存変更フラグ（要件3）─────────────────────────────────────────────
    def mark_dirty(self) -> None:
        self._dirty = True

    def mark_saved(self) -> None:
        self._dirty = False

    def is_dirty(self) -> bool:
        return bool(self._dirty)

    # ── 背景レイヤー ─────────────────────────────────────────────────────────
    def _mark_background(self, item: QGraphicsItem) -> None:
        """背景アイテム共通の設定。編集・選択・削除・保存の対象外にする（要件5）。"""
        # 属性マークとデータマークの両方を付与する。clear/restore/select/delete は
        # この目印で背景を必ず除外する（要件1・5）。
        item._BG_MARK = True  # type: ignore[attr-defined]
        item.setData(_DATA_TYPE, _BG_MARK)
        item.setZValue(-100)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def _add_background(self, pdf_bytes: bytes) -> None:
        # 背景が消えた際に再生成できるよう PDF バイト列を保持する（要件6）。
        self._background_pdf_bytes = pdf_bytes
        pixmap = render_order_sheet_background(pdf_bytes)
        if pixmap is not None and not pixmap.isNull():
            item: QGraphicsItem = QGraphicsPixmapItem(pixmap)
            # ポイント空間にスケール（pixmapはzoom倍でレンダリング済み）。
            scale_x = PAGE_W / pixmap.width()
            scale_y = PAGE_H / pixmap.height()
            _log.debug(
                "voucher_edit_background_pixmap PAGE_W=%s PAGE_H=%s pixmap.width=%s "
                "pixmap.height=%s scale_x=%s scale_y=%s",
                PAGE_W, PAGE_H, pixmap.width(), pixmap.height(), scale_x, scale_y,
            )
            scale = scale_x
            item.setPos(0, 0)
            item.setScale(scale)
        else:
            # フォールバック: 白背景
            item = QGraphicsRectItem(0, 0, PAGE_W, PAGE_H)
            item.setBrush(QBrush(QColor(255, 255, 255)))
            item.setPen(QPen(QColor(200, 200, 200)))
        self._mark_background(item)
        self._scene.addItem(item)
        # 背景リストへ参照を保持する（要件3）。
        self._background_items.append(item)
        # 背景読込後にページ全体を編集領域へフィットする（要件2）。
        if getattr(self, "_view", None) is not None:
            self.fit_page_to_view()

    def background_items(self) -> list[QGraphicsItem]:
        """背景レイヤー（指図書プレビュー）のアイテム一覧（要件3・7）。

        scene 上に実在する背景アイテムだけを返す。保持リスト
        `self._background_items` も scene から外れたものを除いて整理する。
        """
        present = [it for it in self._scene.items()
                   if getattr(it, "_BG_MARK", False)]
        # 保持リストを scene の実体に同期する（削除済み参照を残さず、scene 上に
        # ある背景参照は取りこぼさない）。
        self._background_items = present
        return present

    def _debug_state(self, tag: str) -> None:
        """背景消失・選択状態の再発切り分け用の状態ログを出す（要件12）。"""
        try:
            scene = self._scene
            all_items = scene.items()
            helper = sum(1 for it in all_items if getattr(it, "_IS_HELPER", False))
            preview = sum(1 for it in all_items if getattr(it, "_IS_PREVIEW", False))
            selected_edit = sum(
                1 for it in scene.selectedItems()
                if hasattr(it, "serialize_edit_object")
            )
            _log.debug(
                "[指図書編集:%s] background=%d scene=%d edit=%d helper=%d "
                "preview=%d selected_edit=%d tool=%s rect=%s",
                tag, len(self.background_items()), len(all_items),
                len(self.edit_items()), helper, preview, selected_edit,
                self.current_tool, scene.sceneRect(),
            )
        except Exception:
            # ログ出力で本処理を止めない。
            pass

    def ensure_background_visible(self) -> None:
        """背景が万一消えていたら再生成する保険（要件6）。

        基本は背景を消さない実装だが、想定外の操作で背景アイテムが scene から
        失われた場合に備え、編集系操作の後にこのチェックを呼ぶ。
        """
        if not self.background_items():
            self.reload_background_only()

    def reload_background_only(self) -> None:
        """編集オブジェクトに触れず、背景レイヤーだけを再生成する（要件5・6）。

        scene.clear() や編集レイヤー削除・JSON再読込は一切しない。既存の編集
        オブジェクトはそのまま残し、背景アイテムだけを差し替える。
        """
        for it in self.background_items():
            self._scene.removeItem(it)
        self._background_items = []
        self._add_background(getattr(self, "_background_pdf_bytes", b""))

    # ── ツールバー ───────────────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = QToolBar("編集ツール")
        self.addToolBar(bar)
        # ツール選択（チェック可能にしてハイライト表示する: 要件11）。
        for label, tool in (("選択", TOOL_SELECT), ("テキスト", TOOL_TEXT),
                            ("線", TOOL_LINE), ("四角", TOOL_RECT),
                            ("丸", TOOL_ELLIPSE)):
            act = bar.addAction(label, lambda t=tool: self.set_tool(t))
            act.setCheckable(True)
            self._tool_actions[tool] = act
        bar.addSeparator()

        # 線幅変更UI（要件9）。
        bar.addWidget(QLabel(" 線幅: "))
        self._line_width_spin = QDoubleSpinBox()
        self._line_width_spin.setRange(0.1, 20.0)
        self._line_width_spin.setSingleStep(0.5)
        self._line_width_spin.setValue(self.current_line_width)
        self._line_width_spin.valueChanged.connect(self._on_line_width_changed)
        bar.addWidget(self._line_width_spin)

        # フォントサイズ変更UI（要件10）。
        bar.addWidget(QLabel(" 文字サイズ: "))
        self._font_size_spin = QSpinBox()
        self._font_size_spin.setRange(4, 200)
        self._font_size_spin.setValue(int(self.current_font_size))
        self._font_size_spin.valueChanged.connect(self._on_font_size_changed)
        bar.addWidget(self._font_size_spin)

        bar.addSeparator()
        # 画像挿入・貼り付け（要件2-3・2-4）。
        bar.addAction("画像挿入", self.insert_image_from_file)
        bar.addAction("貼り付け", self.paste_image_from_clipboard)
        bar.addSeparator()
        delete_action = bar.addAction("削除", self.delete_selected)
        bar.addSeparator()
        save_action = bar.addAction("保存", self.save)
        # 「座標マーカー」ボタンは通常UIから削除（add_debug_markers は内部・テスト用に残す）。
        save_close_action = bar.addAction("保存して閉じる", self.save_and_close)
        bar.addAction("閉じる", self.close)
        bar.addSeparator()
        # 全画面/最大化表示の切り替え（要件2-2）。
        self._fullscreen_action = bar.addAction("全画面", self.toggle_fullscreen)

        # 削除ボタンは赤い警告色、保存系ボタンは安全色にする（要件2-6・2-7・3）。
        self._style_action_widget(bar, delete_action, "dangerButton")
        self._style_action_widget(bar, save_action, "successButton")
        self._style_action_widget(bar, save_close_action, "successButton")
        # ツールバー全体のボタン幅・余白を広げ、警告色/安全色を割り当てる（要件2-5）。
        bar.setStyleSheet(EDIT_TOOLBAR_STYLE)

        # 選択ツールを初期ハイライト。
        self._update_tool_highlight()

    @staticmethod
    def _style_action_widget(bar: QToolBar, action, object_name: str) -> None:
        """ツールバーのアクションに objectName を付け、stylesheet で色付けできるようにする。"""
        widget = bar.widgetForAction(action)
        if widget is not None:
            widget.setObjectName(object_name)

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo)
        # Redo: 標準キー(環境依存) / Ctrl+Y / Ctrl+Shift+Z を割り当てる。同一キー列を
        # 二重登録すると Qt が「Ambiguous shortcut」で無反応になる（Ctrl+Y が効かない
        # 原因）ため、Undo(Ctrl+Z)とも重複排除してから一意なものだけ登録する（要件1）。
        seen = {QKeySequence(QKeySequence.StandardKey.Undo).toString()}
        for seq in (
            QKeySequence(QKeySequence.StandardKey.Redo),
            QKeySequence("Ctrl+Y"),
            QKeySequence("Ctrl+Shift+Z"),
        ):
            label = seq.toString()
            if not label or label in seen:
                continue
            seen.add(label)
            QShortcut(seq, self, activated=self.redo)
        QShortcut(QKeySequence.StandardKey.SelectAll, self, activated=self.select_all)
        # クリップボード画像の貼り付け（不具合2）はツール状態に依存させない。
        # どのツール選択中でも Ctrl+V で確実に拾えるよう QShortcut を登録する。
        # テキスト編集中はテキストコントロールが ShortcutOverride を受理して
        # この QShortcut を抑止する（＝文字貼り付けが優先される）ため、画像貼り付けと
        # 文字貼り付けは両立する。保険として handle_paste_shortcut も編集中は False を返す。
        self._paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self._paste_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._paste_shortcut.activated.connect(self.handle_paste_shortcut)

    # ── ツール状態 ───────────────────────────────────────────────────────────
    def set_tool(self, tool: str) -> None:
        self.current_tool = tool
        # 選択モードのときだけドラッグ選択を有効化。
        if tool == TOOL_SELECT:
            self._view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        else:
            self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._update_tool_highlight()
        # ツール切替でも背景は消さない（要件1・4）。
        self.ensure_background_visible()

    def _update_tool_highlight(self) -> None:
        """選択中ツールのボタンだけをハイライトする（要件11）。"""
        for tool, act in self._tool_actions.items():
            checked = tool == self.current_tool
            act.setChecked(checked)
            font = act.font()
            font.setBold(checked)
            act.setFont(font)

    def current_pen(self) -> QPen:
        pen = QPen(QColor(0, 0, 0))
        pen.setWidthF(self.current_line_width)
        pen.setCosmetic(True)
        return pen

    # ── 全画面 / 最大化表示の切り替え（要件2-1・2-2）─────────────────────────────
    def toggle_fullscreen(self) -> None:
        """全画面とタイトルバー付き最大化表示を切り替える。"""
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        self.showFullScreen()
        self._update_fullscreen_action()
        # 全画面切替後もページ全体を再フィットする（要件2）。
        self.fit_page_to_view()

    def exit_fullscreen(self) -> None:
        """全画面を解除し、タイトルバー付き最大化表示に戻す（要件2-1・2-2）。"""
        self.showMaximized()
        self._update_fullscreen_action()
        # 全画面解除後もページ全体を再フィットする（要件2）。
        self.fit_page_to_view()

    def _update_fullscreen_action(self) -> None:
        action = getattr(self, "_fullscreen_action", None)
        if action is None:
            return
        action.setText("全画面解除" if self.isFullScreen() else "全画面")

    # ── 線幅・フォントサイズUI ────────────────────────────────────────────────
    def _on_line_width_changed(self, value: float) -> None:
        self.current_line_width = float(value)
        if self._updating_property_ui:
            return
        changed = False
        for item in self._scene.selectedItems():
            if hasattr(item, "apply_line_width"):
                item.apply_line_width(self.current_line_width)
                changed = True
        if changed:
            self.refresh_handles()
            self.commit_history()

    def _on_font_size_changed(self, value: int) -> None:
        self.current_font_size = float(value)
        if self._updating_property_ui:
            return
        changed = False
        for item in self._scene.selectedItems():
            if hasattr(item, "apply_font_size"):
                item.apply_font_size(self.current_font_size)
                changed = True
        if changed:
            self.refresh_handles()
            self.commit_history()

    # ── 編集レイヤー操作 ─────────────────────────────────────────────────────
    def edit_items(self) -> list[QGraphicsItem]:
        """編集レイヤー（保存対象）のアイテム一覧。背景・ハンドルは除く（要件7）。"""
        result: list[QGraphicsItem] = []
        for it in self._scene.items():
            # 背景・補助・一時プレビューは絶対に編集レイヤーへ含めない（要件1・5・7・11）。
            if (getattr(it, "_BG_MARK", False) or getattr(it, "_IS_HELPER", False)
                    or getattr(it, "_IS_PREVIEW", False)):
                continue
            if hasattr(it, "serialize_edit_object"):
                result.append(it)
        return result

    def clear_edit_layer(self) -> None:
        """編集レイヤーだけを消去する（背景は必ず残す: 要件1）。"""
        self._remove_handles()
        self._scene.cancel_temp_item()
        for it in list(self._scene.items()):
            # 背景アイテムはスキップして必ず残す（要件1）。
            if getattr(it, "_BG_MARK", False):
                continue
            # 補助アイテム（ハンドル）・一時プレビューは消す。
            if getattr(it, "_IS_HELPER", False) or getattr(it, "_IS_PREVIEW", False):
                if it.scene() is not None:
                    self._scene.removeItem(it)
                continue
            # 編集オブジェクトだけ消す。
            if hasattr(it, "serialize_edit_object"):
                self._scene.removeItem(it)
        self.loaded_object_ids.clear()
        # 編集レイヤー消去後も背景は残す（要件1・4）。
        self.ensure_background_visible()

    def _register(self, item: QGraphicsItem) -> None:
        self._scene.addItem(item)
        self.loaded_object_ids.add(item.obj_id)

    def _select_only(self, item: QGraphicsItem) -> None:
        """既存の選択を解除してから対象だけを選択する（単一選択: 要件6・10）。

        新規作成・図形編集開始で対象を1つだけ選択状態にし、複数オブジェクトが
        同時選択（全選択のように見える状態）になるのを防ぐ。
        """
        self._scene.clearSelection()
        item.setSelected(True)

    # ── オブジェクト追加 ─────────────────────────────────────────────────────
    def add_text_rect(self, rect: QRectF, text: str = "",
                      font_size: float | None = None,
                      obj_id: str | None = None,
                      auto_edit: bool = True,
                      font_family: str | None = None,
                      font_bold: bool = False,
                      font_italic: bool = False,
                      text_color: str = DEFAULT_TEXT_COLOR,
                      line_width: float | None = None,
                      stroke_color: str = DEFAULT_STROKE_COLOR,
                      fill_color: str | None = None,
                      text_align: str = "left",
                      vertical_align: str = "top",
                      auto_fit: bool = True,
                      manual_resized: bool = False) -> _EditTextItem:
        fs = self.current_font_size if font_size is None else font_size
        lw = self.current_line_width if line_width is None else line_width
        w = max(rect.width(), MIN_TEXT_W)
        h = max(rect.height(), MIN_TEXT_H)
        item = _EditTextItem(text=text, obj_id=obj_id, font_size=fs,
                             box_w=w, box_h=h, font_family=font_family,
                             font_bold=font_bold, font_italic=font_italic,
                             text_color=text_color, line_width=lw,
                             stroke_color=stroke_color, fill_color=fill_color,
                             text_align=text_align,
                             vertical_align=vertical_align,
                             auto_fit=auto_fit,
                             manual_resized=manual_resized)
        item.setPos(rect.topLeft())
        self._register(item)
        if auto_edit and not text:
            # 作成直後はすぐ文字入力できるようにする。単一選択にして全選択化を防ぐ（要件6・10）。
            self._select_only(item)
            item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
            item.setFocus(Qt.FocusReason.OtherFocusReason)
        return item

    def add_symbol_text(self, pos: QPointF, text: str,
                        font_size: float | None = None,
                        obj_id: str | None = None,
                        font_family: str | None = None,
                        font_bold: bool = False,
                        font_italic: bool = False,
                        text_color: str = DEFAULT_TEXT_COLOR,
                        anchor: str = "center") -> _EditSymbolTextItem:
        fs = self.current_font_size if font_size is None else font_size
        item = _EditSymbolTextItem(text=text, obj_id=obj_id, font_size=fs,
                                   font_family=font_family,
                                   font_bold=font_bold,
                                   font_italic=font_italic,
                                   text_color=text_color,
                                   anchor=anchor)
        item.set_anchor_scene_pos(pos)
        self._register(item)
        return item

    def add_text_at(self, pos: QPointF, text: str | None = None,
                    font_size: float | None = None) -> _EditTextItem:
        """指定位置に最小サイズのテキストボックスを作成する（互換API）。"""
        rect = QRectF(pos.x(), pos.y(), MIN_TEXT_W, MIN_TEXT_H)
        return self.add_text_rect(rect, text=text or "", font_size=font_size)

    def add_line(self, p1: QPointF, p2: QPointF,
                 obj_id: str | None = None,
                 line_width: float | None = None,
                 stroke_color: str = DEFAULT_STROKE_COLOR) -> _EditLineItem:
        lw = self.current_line_width if line_width is None else line_width
        item = _EditLineItem(p1.x(), p1.y(), p2.x(), p2.y(),
                             obj_id=obj_id, line_width=lw,
                             stroke_color=stroke_color)
        self._register(item)
        return item

    def add_image(self, image_bytes: bytes, rect: QRectF | None = None,
                  obj_id: str | None = None, image_format: str = "png",
                  width: float | None = None, height: float | None = None,
                  select: bool = True) -> "_EditImageItem | None":
        """画像オブジェクトを編集レイヤーへ追加する（要件2-3・2-4）。

        rect を渡せばその位置・大きさで配置する。省略時は画像の自然サイズで
        scene 中央に配置する。
        """
        if not image_bytes:
            return None
        item = _EditImageItem(image_bytes, image_format=image_format,
                              obj_id=obj_id, width=width, height=height)
        if rect is not None:
            if rect.width() > 0 and rect.height() > 0:
                item.set_box_size(rect.width(), rect.height())
            item.setPos(rect.topLeft())
        else:
            # scene 中央に画像中心が来るよう配置する。
            cx = PAGE_W / 2.0
            cy = PAGE_H / 2.0
            item.setPos(cx - item.box_w / 2.0, cy - item.box_h / 2.0)
        self._register(item)
        if select:
            self._select_only(item)
        return item

    def insert_image_from_file(self, file_path: str | None = None) -> "_EditImageItem | None":
        """画像ファイルを選択して挿入する（要件2-3）。"""
        if file_path is None:
            file_path, _ = QFileDialog.getOpenFileName(
                self, "画像を選択", "",
                "画像ファイル (*.png *.jpg *.jpeg *.bmp *.gif);;すべてのファイル (*.*)",
            )
        if not file_path:
            return None
        image = QImage(file_path)
        if image.isNull():
            QMessageBox.warning(self, "画像挿入エラー", "画像の読み込みに失敗しました。")
            return None
        png_bytes = qimage_to_png_bytes(image)
        item = self.add_image(png_bytes, rect=self._default_image_rect(image))
        if item is not None:
            self.commit_history()
        self.ensure_background_visible()
        return item

    def paste_image_from_clipboard(self) -> "_EditImageItem | None":
        """クリップボードの画像を貼り付ける。画像が無ければ何もしない（要件2-4）。"""
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None or not mime.hasImage():
            return None
        image = clipboard.image()
        if image.isNull():
            return None
        png_bytes = qimage_to_png_bytes(image)
        item = self.add_image(png_bytes, rect=self._default_image_rect(image))
        if item is not None:
            self.commit_history()
        self.ensure_background_visible()
        return item

    def _default_image_rect(self, image: QImage) -> QRectF:
        """挿入画像の初期配置矩形（scene中央・最大幅を超えない大きさ）を返す。"""
        w = float(image.width()) or MIN_TEXT_W
        h = float(image.height()) or MIN_TEXT_H
        max_w = PAGE_W * 0.5
        max_h = PAGE_H * 0.5
        scale = min(max_w / w, max_h / h, 1.0)
        w *= scale
        h *= scale
        cx = PAGE_W / 2.0
        cy = PAGE_H / 2.0
        return QRectF(cx - w / 2.0, cy - h / 2.0, w, h)

    def add_debug_markers(self) -> None:
        """PDF位置合わせ用の座標マーカーをscene座標で追加する。"""
        markers = (
            ("左上", QPointF(50.0, 50.0)),
            ("中央", QPointF(PAGE_W / 2.0, PAGE_H / 2.0)),
            ("右下", QPointF(PAGE_W - 50.0, PAGE_H - 50.0)),
        )
        size = 10.0
        for label, pt in markers:
            self.add_line(QPointF(pt.x() - size, pt.y()),
                          QPointF(pt.x() + size, pt.y()),
                          line_width=0.8)
            self.add_line(QPointF(pt.x(), pt.y() - size),
                          QPointF(pt.x(), pt.y() + size),
                          line_width=0.8)
            self.add_text_rect(QRectF(pt.x() + 4.0, pt.y() + 4.0, 80.0, 18.0),
                               text=f"{label} ({pt.x():.1f},{pt.y():.1f})",
                               font_size=8.0, auto_edit=False)
        self.commit_history()

    def add_rect(self, rect: QRectF, obj_id: str | None = None,
                 line_width: float | None = None,
                 text: str = "", font_size: float | None = None,
                 auto_edit: bool = False,
                 font_family: str | None = None,
                 font_bold: bool = False,
                 font_italic: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 fill_color: str | None = None,
                 text_align: str = "center",
                 vertical_align: str = "middle") -> _EditRectItem:
        lw = self.current_line_width if line_width is None else line_width
        fs = self.current_font_size if font_size is None else font_size
        item = _EditRectItem(rect, obj_id=obj_id, line_width=lw,
                             text=text, font_size=fs,
                             font_family=font_family, font_bold=font_bold,
                             font_italic=font_italic, text_color=text_color,
                             stroke_color=stroke_color, fill_color=fill_color,
                             text_align=text_align,
                             vertical_align=vertical_align)
        self._register(item)
        if auto_edit and not text:
            self._begin_shape_text_edit(item)
        return item

    def add_ellipse(self, rect: QRectF, obj_id: str | None = None,
                    line_width: float | None = None,
                    text: str = "", font_size: float | None = None,
                    auto_edit: bool = False,
                    font_family: str | None = None,
                    font_bold: bool = False,
                    font_italic: bool = False,
                    text_color: str = DEFAULT_TEXT_COLOR,
                    stroke_color: str = DEFAULT_STROKE_COLOR,
                    fill_color: str | None = None,
                    text_align: str = "center",
                    vertical_align: str = "middle") -> _EditEllipseItem:
        lw = self.current_line_width if line_width is None else line_width
        fs = self.current_font_size if font_size is None else font_size
        item = _EditEllipseItem(rect, obj_id=obj_id, line_width=lw,
                                text=text, font_size=fs,
                                font_family=font_family, font_bold=font_bold,
                                font_italic=font_italic,
                                text_color=text_color,
                                stroke_color=stroke_color,
                                fill_color=fill_color,
                                text_align=text_align,
                                vertical_align=vertical_align)
        self._register(item)
        if auto_edit and not text:
            self._begin_shape_text_edit(item)
        return item

    def _begin_shape_text_edit(self, item) -> None:
        """図形作成直後、内部テキストの編集状態へ入る（カーソルを置く: 要件4）。"""
        # 単一選択にして他オブジェクトの選択枠が残らないようにする（要件6・10）。
        self._select_only(item)
        item.edit_inner_text()

    def remove_text_item(self, item) -> None:
        """単独テキストオブジェクトを削除する（空文字テキストの後始末: 要件3）。"""
        self._remove_handles()
        self.loaded_object_ids.discard(getattr(item, "obj_id", ""))
        if item.scene() is not None:
            self._scene.removeItem(item)
        self.ensure_background_visible()

    def maybe_convert_text_item_to_symbol(self, item: _EditTextItem) -> bool:
        """短い単独テキストボックスを中心アンカーの symbol_text へ置換する。"""
        text = item.toPlainText().strip()
        if not is_symbol_text_candidate(text):
            return False
        center = item.sceneBoundingRect().center()
        was_selected = item.isSelected()
        self._remove_handles()
        self.loaded_object_ids.discard(item.obj_id)
        if item.scene() is not None:
            self._scene.removeItem(item)
        symbol = self.add_symbol_text(center, text, font_size=item.font_size,
                                      obj_id=item.obj_id,
                                      font_family=item.font_family,
                                      font_bold=item.font_bold,
                                      font_italic=item.font_italic,
                                      text_color=item.text_color)
        if was_selected:
            self._select_only(symbol)
        self.ensure_background_visible()
        self.commit_history()
        return True

    def delete_selected(self) -> None:
        self._remove_handles()
        removed = False
        for item in self._scene.selectedItems():
            if hasattr(item, "serialize_edit_object"):
                self.loaded_object_ids.discard(item.obj_id)
                self._scene.removeItem(item)
                removed = True
        if removed:
            self.commit_history()
        self.ensure_background_visible()

    def select_all(self) -> None:
        """編集オブジェクトだけを全選択する（背景・ハンドルは除く: 要件4）。"""
        self._scene.clearSelection()
        for item in self.edit_items():
            item.setSelected(True)
        self.ensure_background_visible()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            # 全画面表示中の Esc は全画面解除を優先する（要件2-2）。
            if self.isFullScreen():
                self.exit_fullscreen()
                event.accept()
                return
            # Escの手順（要件8）。背景や編集オブジェクトは消さず、選択解除に徹する。
            # 1. テキスト編集中なら編集終了
            focus = self._scene.focusItem()
            if focus is not None:
                focus.clearFocus()
            # 2. 作成中オブジェクト（プレビュー）があればキャンセル
            self._scene.cancel_temp_item()
            # 3. 選択中オブジェクトを全解除（ハンドルも撤去）
            self._scene.clearSelection()
            self._remove_handles()
            # 4. ツールを選択ツールへ戻す
            self.set_tool(TOOL_SELECT)
            # 5. 背景は消さない（保険として可視性を保証）
            self.ensure_background_visible()
            self._debug_state("escape")
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            # テキスト編集中は通常のバックスペース等を優先する。
            focus = self._scene.focusItem()
            if focus is None:
                self.delete_selected()
                event.accept()
                return
        if event.matches(QKeySequence.StandardKey.Paste):
            # テキスト編集中なら handle_paste_shortcut() は False を返し、
            # 下の super().keyPressEvent でテキスト貼り付けへ委ねる（要件5）。
            if self.handle_paste_shortcut():
                event.accept()
                return
        super().keyPressEvent(event)

    def handle_paste_shortcut(self) -> bool:
        """Ctrl+V を処理する。画像を貼り付けたら True、委譲するなら False を返す。

        - テキスト編集中: テキスト貼り付けを優先するため何もせず False。
        - クリップボードに画像あり: 画像オブジェクトとして貼り付け True。
        - 画像なし: 既存挙動を壊さないよう False（通常の貼り付けに委ねる）。

        ビュー/ウィンドウのどちらにフォーカスがあっても Ctrl+V を確実に拾えるよう、
        _EditGraphicsView.keyPressEvent と本ウィンドウ keyPressEvent の双方から呼ぶ。
        """
        if self._is_text_editing():
            return False
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None or not mime.hasImage():
            return False
        item = self.paste_image_from_clipboard()
        if item is None:
            return False
        # 貼り付け後は操作しやすいように選択ツールへ戻し、貼り付けた画像を選択する。
        self.set_tool(TOOL_SELECT)
        self._select_only(item)
        return True

    def _is_text_editing(self) -> bool:
        """いずれかのテキストアイテムが編集状態（カーソル入力可）か判定する（要件5）。"""
        focus = self._scene.focusItem()
        if focus is None:
            return False
        flags = focus.textInteractionFlags() if hasattr(focus, "textInteractionFlags") else None
        if flags is None:
            return False
        return bool(flags & Qt.TextInteractionFlag.TextEditorInteraction)

    # ── サイズ変更ハンドル ───────────────────────────────────────────────────
    def _remove_handles(self) -> None:
        for h in self._handles:
            if h.scene() is not None:
                self._scene.removeItem(h)
        self._handles = []

    def _on_selection_changed(self) -> None:
        try:
            self._remove_handles()
            selected = [it for it in self._scene.selectedItems()
                        if hasattr(it, "serialize_edit_object")]
        except RuntimeError:
            # 破棄処理中に selectionChanged が発火した場合は無視する。
            return
        # 選択変更でも背景は消さない（要件4）。
        self.ensure_background_visible()
        self._sync_property_ui_from_selection(selected)
        # 単一選択時のみハンドル表示。複数選択や未選択では選択枠を出さない（要件10）。
        if len(selected) != 1:
            return
        target = selected[0]
        if isinstance(target, (_EditRectItem, _EditEllipseItem, _EditTextItem, _EditImageItem)):
            handle = _ResizeHandle(target)
            self._scene.addItem(handle)
            self._handles.append(handle)
        elif isinstance(target, _EditLineItem):
            for which in ("p1", "p2"):
                handle = _LineEndHandle(target, which)
                self._scene.addItem(handle)
                self._handles.append(handle)

    def refresh_handles(self) -> None:
        """選択状態を保ったままハンドルを作り直す（移動/リサイズ後の追従）。"""
        self._on_selection_changed()

    def _sync_property_ui_from_selection(self, selected: list[QGraphicsItem]) -> None:
        """選択オブジェクトの属性をツールバーへ反映する。"""
        if not selected:
            return
        item = selected[0]
        self._updating_property_ui = True
        try:
            if hasattr(item, "line_width"):
                self.current_line_width = float(item.line_width)
                self._line_width_spin.setValue(self.current_line_width)
            if hasattr(item, "font_size"):
                self.current_font_size = float(item.font_size)
                self._font_size_spin.setValue(int(round(self.current_font_size)))
        finally:
            self._updating_property_ui = False

    # ── Undo / Redo（スナップショット方式: 要件3）──────────────────────────────
    def commit_history(self) -> None:
        """現在の編集レイヤー状態を履歴へ積む（直前と同一なら無視）。

        履歴復元（Undo/Redo）中は呼ばれても何もしない。これにより復元処理の途中で
        発火する focusOut 等が Redo 履歴を消してしまうのを防ぐ（要件1）。
        """
        if self._is_restoring_history:
            return
        # 保険: テキスト/図形挿入・削除などの変更後も背景が残っていることを保証する（要件6）。
        self.ensure_background_visible()
        snap = self.serialize_objects()
        if (0 <= self._history_index < len(self._history)
                and snap == self._history[self._history_index]):
            return
        del self._history[self._history_index + 1:]
        self._history.append(snap)
        self._history_index = len(self._history) - 1
        # オブジェクト追加・移動・サイズ変更・削除などで未保存変更が発生（要件3・6）。
        self.mark_dirty()

    def undo(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self._restore_snapshot(self._history[self._history_index])
            self.mark_dirty()
        self._debug_state("undo")

    def redo(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._restore_snapshot(self._history[self._history_index])
            self.mark_dirty()
        self._debug_state("redo")

    def _restore_snapshot(self, snapshot: list[dict[str, Any]]) -> None:
        """履歴スナップショットから編集レイヤーを再構築する。

        背景は残したまま編集オブジェクトだけを差し替える（要件1・2）。復元中は
        履歴追加を抑止し、Redoスタックを保持する（要件1）。
        """
        self._is_restoring_history = True
        try:
            # 背景は残す実装の clear_edit_layer で編集オブジェクトだけ消す（要件2）。
            self.clear_edit_layer()
            for obj in snapshot:
                self._add_loaded_object(obj)
        finally:
            self._is_restoring_history = False
        # 保険: 想定外の操作で背景が失われていたら復旧する（要件6）。
        self.ensure_background_visible()

    # ── シリアライズ（scene座標オブジェクト）──────────────────────────────────
    def serialize_objects(self) -> list[dict[str, Any]]:
        """編集レイヤーのアイテムのみをscene座標オブジェクトへ変換する。

        背景PDFやサイズ変更ハンドルは保存対象に含めない（要件2・3）。
        履歴比較を安定させるため id 順に整列して返す。
        """
        objects: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.edit_items():
            obj = item.serialize_edit_object()
            # 空文字（空白のみ）の単独テキストは保存・履歴対象に含めない（要件3）。
            # 図形（四角・丸）は内部テキストが空でも図形として残す。
            if obj.get("type") in ("text", "symbol_text") and not str(obj.get("text", "")).strip():
                continue
            obj_id = obj.get("id")
            if obj_id in seen:
                continue
            seen.add(obj_id)
            objects.append(obj)
        objects.sort(key=lambda o: str(o.get("id") or ""))
        return objects

    # ── 既存オブジェクト読み込み（scene座標 → scene）──────────────────────────
    def load_edit_layer(self) -> None:
        """編集レイヤーを一度クリアしてからJSONを読み込む（二重生成防止）。"""
        self.clear_edit_layer()
        try:
            objects = load_edit_objects(self.order_no)
        except Exception:
            objects = []
        for obj in objects:
            obj_id = str(obj.get("id") or "")
            if obj_id and obj_id in self.loaded_object_ids:
                # 同じIDは重複作成しない（要件2）。
                continue
            self._add_loaded_object(obj)
        # 初期ツールは「テキスト」にして、すぐテキストボックスを作れるようにする（要件2）。
        self.set_tool(TOOL_TEXT)
        # 読み込み直後の状態を履歴の起点にする。
        self._history = [self.serialize_objects()]
        self._history_index = 0

    # 互換のため旧名も残す。
    def _load_existing(self) -> None:
        self.load_edit_layer()

    def _add_loaded_object(self, obj: dict[str, Any]) -> None:
        kind = obj.get("type")
        is_scene_origin = obj.get("coordinate_origin") == COORDINATE_ORIGIN
        obj_id = str(obj.get("id") or "") or None
        font_family = resolve_text_font_family(str(obj.get("font_family") or ""))
        font_size = float(obj.get("font_size") or DEFAULT_FONT_SIZE)
        line_width = float(obj.get("line_width") or DEFAULT_LINE_WIDTH)
        text_color = str(obj.get("text_color") or DEFAULT_TEXT_COLOR)
        stroke_color = str(obj.get("stroke_color") or DEFAULT_STROKE_COLOR)
        fill_color = obj.get("fill_color")
        font_bold = bool(obj.get("font_bold", False))
        font_italic = bool(obj.get("font_italic", False))
        if kind == "symbol_text":
            text = str(obj.get("text", "")).strip()
            if not text:
                return
            x = float(obj.get("x", 0.0))
            y = float(obj.get("y", 0.0))
            scene_y = y if is_scene_origin else PAGE_H - y
            self.add_symbol_text(QPointF(x, scene_y), text,
                                 font_size=font_size, obj_id=obj_id,
                                 font_family=font_family,
                                 font_bold=font_bold,
                                 font_italic=font_italic,
                                 text_color=text_color,
                                 anchor=str(obj.get("anchor") or "center"))
        elif kind == "text":
            # 空文字（空白のみ）の単独テキストは復元しない（要件3）。
            if not str(obj.get("text", "")).strip():
                return
            x = float(obj.get("x", 0.0))
            y = float(obj.get("y", 0.0))
            if "height" in obj or "h" in obj:
                h = float(obj.get("height") or obj.get("h") or MIN_TEXT_H)
                w = float(obj.get("width") or obj.get("w") or MIN_TEXT_W)
                scene_top = y if is_scene_origin else PAGE_H - y - h
            else:
                # 旧形式: y はベースライン基準。ボックスへ近似変換する。
                h = MIN_TEXT_H
                w = MIN_TEXT_W
                scene_top = y if is_scene_origin else PAGE_H - y - font_size
            self.add_text_rect(QRectF(x, scene_top, w, h),
                               text=str(obj.get("text", "")),
                               font_size=font_size, obj_id=obj_id,
                               auto_edit=False, font_family=font_family,
                               font_bold=font_bold,
                               font_italic=font_italic,
                               text_color=text_color,
                               line_width=line_width,
                               stroke_color=stroke_color,
                               fill_color=fill_color,
                               text_align="left",
                               vertical_align="top",
                               auto_fit=bool(obj.get("auto_fit", True)),
                               manual_resized=bool(obj.get("manual_resized", False)))
        elif kind == "line":
            x1 = float(obj.get("x1", 0.0)); raw_y1 = float(obj.get("y1", 0.0))
            x2 = float(obj.get("x2", 0.0)); raw_y2 = float(obj.get("y2", 0.0))
            y1 = raw_y1 if is_scene_origin else PAGE_H - raw_y1
            y2 = raw_y2 if is_scene_origin else PAGE_H - raw_y2
            self.add_line(QPointF(x1, y1), QPointF(x2, y2), obj_id=obj_id,
                          line_width=line_width, stroke_color=stroke_color)
        elif kind == "image":
            data_b64 = obj.get("image_data") or ""
            if not data_b64:
                return
            try:
                image_bytes = base64.b64decode(data_b64)
            except (ValueError, TypeError):
                return
            x = float(obj.get("x", 0.0))
            w = float(obj.get("width") or obj.get("w") or MIN_TEXT_W)
            h = float(obj.get("height") or obj.get("h") or MIN_TEXT_H)
            y = float(obj.get("y", 0.0))
            top = y if is_scene_origin else PAGE_H - y - h
            self.add_image(image_bytes, rect=QRectF(x, top, w, h), obj_id=obj_id,
                           image_format=str(obj.get("image_format") or "png"),
                           select=False)
        elif kind in ("rectangle", "ellipse"):
            x = float(obj.get("x", 0.0))
            w = float(obj.get("width") or obj.get("w") or MIN_TEXT_W)
            h = float(obj.get("height") or obj.get("h") or MIN_TEXT_H)
            y = float(obj.get("y", 0.0))
            top = y if is_scene_origin else PAGE_H - y - h
            adder = self.add_rect if kind == "rectangle" else self.add_ellipse
            adder(QRectF(x, top, w, h), obj_id=obj_id,
                  line_width=line_width,
                  text=str(obj.get("text", "")),
                  font_size=font_size,
                  font_family=font_family,
                  font_bold=font_bold,
                  font_italic=font_italic,
                  text_color=text_color,
                  stroke_color=stroke_color,
                  fill_color=fill_color,
                  text_align=str(obj.get("text_align") or "center"),
                  vertical_align=str(obj.get("vertical_align") or "middle"))

    # ── 保存 ─────────────────────────────────────────────────────────────────
    def _persist(self) -> bool:
        """編集オブジェクトを保存する。成功で True、失敗で False（エラー表示済み）。"""
        try:
            objects = self.serialize_objects()
            # append ではなく、現在の編集レイヤー一覧で上書き保存する（要件2）。
            save_edit_objects(self.order_no, objects)
            self.loaded_object_ids = {o["id"] for o in objects}
            # 保存成功で未保存変更フラグを下ろす（要件3）。
            self.mark_saved()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "保存エラー", f"編集内容の保存に失敗しました:\n{exc}")
            return False

    def save(self) -> None:
        if not self._persist():
            return
        QMessageBox.information(
            self, "保存完了",
            "編集内容を保存しました。\n指図書(1)・指図書(2)・梱包明細書へ反映されます。",
        )

    def save_and_close(self) -> None:
        """保存に成功したら画面を閉じる。失敗時は閉じない（要件5）。"""
        if self._persist():
            self.close()

    # ── 表示フィット（要件2）─────────────────────────────────────────────────
    def fit_page_to_view(self) -> None:
        """指図書ページ全体を編集領域いっぱいに（アスペクト比維持で）表示する。"""
        view = getattr(self, "_view", None)
        if view is None:
            return
        view.fitInView(QRectF(0.0, 0.0, PAGE_W, PAGE_H),
                       Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        super().showEvent(event)
        apply_windows_title_bar_theme(self, current_title_bar_is_dark())
        # 画面表示後にページ全体を編集領域いっぱいへフィット（要件2）。
        self.fit_page_to_view()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        super().resizeEvent(event)
        # ウィンドウ／全画面切替・最大化などのリサイズ時に再フィットする（要件2）。
        self.fit_page_to_view()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        """未保存変更があれば確認してから閉じる（要件3）。"""
        if not self.is_dirty():
            super().closeEvent(event)
            return
        choice = self._prompt_unsaved_changes()
        if choice == "cancel":
            event.ignore()
            return
        if choice == "save":
            if not self._persist():
                # 保存失敗時は閉じない。
                event.ignore()
                return
        # "discard"（保存せずに閉じる）／保存成功 → そのまま閉じる。
        super().closeEvent(event)

    def _prompt_unsaved_changes(self) -> str:
        """未保存変更の確認ダイアログを出す。戻り値: "save"/"discard"/"cancel"（要件3）。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("確認")
        box.setText("編集内容が保存されていません。\n保存せずに閉じますか？")
        save_close_btn = box.addButton("保存して閉じる", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("保存せずに閉じる", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return "cancel"
        if clicked is save_close_btn:
            return "save"
        return "discard"
