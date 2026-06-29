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
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QLineF,
    QObject,
    QPointF,
    QRectF,
    QSettings,
    QThread,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QTextOption,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QPushButton,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsSimpleTextItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.config import resource_path
from app.line_decorations import (
    ARROW_HEAD_LENGTH,
    DOUBLE_LINE_GAP,
    LINE_TYPE_ARROW,
    LINE_TYPE_DOUBLE_ARROW,
    LINE_TYPE_DOUBLE_LINE,
    LINE_TYPE_LINE,
    line_segments,
    normalize_line_type,
)
from app.voucher_edit_objects import load_edit_objects, save_edit_objects
from app.voucher_edit_objects import COORDINATE_ORIGIN, GEOMETRY_BASIS
from app.voucher_edit_templates import (
    DEFAULT_TARGET_VOUCHERS,
    LOCKED_REFLECT_TARGETS,
    delete_template,
    is_locked_template,
    load_templates,
    load_user_templates,
    save_user_templates,
)
from app.voucher_templates import VOUCHER_TYPES
from app.theme_utils import apply_windows_title_bar_theme, current_title_bar_is_dark
from app.voucher_templates import PAGE_H, PAGE_W

# 背景消失・選択状態の再発切り分け用ロガー（要件12）。アプリ既定ロガーへ debug 出力する。
_log = logging.getLogger("tks_to_kintone_app")

# ツールモード
TOOL_SELECT = "select"
TOOL_TEXT = "text"
TOOL_LINE = "line"
TOOL_ARROW = "arrow"
TOOL_DOUBLE_ARROW = "double_arrow"
TOOL_DOUBLE_LINE = "double_line"
TOOL_RECT = "rect"
TOOL_ELLIPSE = "ellipse"
# 手書きペン／消しゴム（タブレット編集モード主体のツール）。
TOOL_PEN = "pen"
TOOL_ERASER = "eraser"
# 掴む（パン）モード。プレビューをドラッグでスクロール／パンする。描画はしない。
TOOL_GRAB = "grab"

# 手書きペンの太さプリセット（細/中/太）と初期値。
PEN_WIDTH_THIN = 1.5
PEN_WIDTH_MEDIUM = 3.0
PEN_WIDTH_THICK = 6.0
PEN_WIDTHS: tuple[tuple[str, float], ...] = (
    ("細", PEN_WIDTH_THIN),
    ("中", PEN_WIDTH_MEDIUM),
    ("太", PEN_WIDTH_THICK),
)
DEFAULT_PEN_WIDTH = PEN_WIDTH_MEDIUM
DEFAULT_PEN_COLOR = "#000000"
# 手書きペンの色プリセット（黒/赤/青）。
PEN_COLORS: tuple[tuple[str, str], ...] = (
    ("黒", "#000000"),
    ("赤", "#d32f2f"),
    ("青", "#1976d2"),
)
# 消しゴムの当たり判定半径（scene ポイント単位）。なぞった位置に近い手書き線を消す。
ERASER_RADIUS = 12.0

# 線系ツール（ドラッグで始点〜終点の線を引く）。line_type への対応表も持つ。
LINE_TOOLS = (TOOL_LINE, TOOL_ARROW, TOOL_DOUBLE_ARROW, TOOL_DOUBLE_LINE)
TOOL_TO_LINE_TYPE = {
    TOOL_LINE: LINE_TYPE_LINE,
    TOOL_ARROW: LINE_TYPE_ARROW,
    TOOL_DOUBLE_ARROW: LINE_TYPE_DOUBLE_ARROW,
    TOOL_DOUBLE_LINE: LINE_TYPE_DOUBLE_LINE,
}

# scene の data キー
_DATA_TYPE = 0

# 背景レイヤーの目印
_BG_MARK = "_background"

# テンプレートバッヂ（編集画面だけの補助表示）の目印。
# _BG_MARK / _IS_HELPER / _IS_PREVIEW と同じく PDF反映・保存・Undo/Redo対象外（要件6）。
_IS_BADGE = "_is_badge"


def _normalize_target_vouchers(value: object) -> list[str]:
    """反映先伝票を正規化する。未設定/不正なら既定（03/04/05）。"""
    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if str(v).strip()]
        if cleaned:
            return cleaned
    return list(DEFAULT_TARGET_VOUCHERS)

DEFAULT_FONT_SIZE = 12.0
DEFAULT_LINE_WIDTH = 1.0
DEFAULT_COLOR = (0.0, 0.0, 0.0)
DEFAULT_TEXT_COLOR = "#000000"
DEFAULT_STROKE_COLOR = "#000000"
MIN_TEXT_W = 60.0
MIN_TEXT_H = 18.0
# 画像・四角・丸のリサイズ最小サイズ（要件: min_width/min_height）。
MIN_RESIZE = 10.0
MIN_OBJECT_WIDTH = 8.0
MIN_OBJECT_HEIGHT = 8.0
PASTE_OFFSET_X = 12.0
PASTE_OFFSET_Y = 12.0
# リサイズ/端点ハンドルの見た目サイズとクリック判定サイズ（不具合1）。
# 見た目は小さく、クリック判定は大きくして掴みやすくする。
HANDLE_SIZE = 10.0
HANDLE_HIT_SIZE = 18.0
RECT_TEXT_PAD = 3.0
SYMBOL_TEXT_MAX_CHARS = 3

# 画像「背景を透過」は rembg による背景除去で行う（要件1）。紙撮影画像の
# 薄いグレー背景や影もしきい値方式では消えないため、機械学習ベースへ移行した。

# 二値化／背景を透過（閾値）で使う RGB しきい値。スマホ撮影の紙背景はグレー・
# 黄ばみ・影を含み純白にならないため、各成分の全チャンネル一致ではなく「輝度」で
# 白背景を判定する（is_light_background_pixel）。プリセットは実機調整に合わせて
# 低めへ調整し、既定は「中」(90,90,90)。設定値は QSettings に保存して保持する。
THRESHOLD_PRESETS: dict[str, int] = {"low": 60, "mid": 90, "high": 120}
DEFAULT_THRESHOLD_RGB: tuple[int, int, int] = (
    THRESHOLD_PRESETS["mid"],
    THRESHOLD_PRESETS["mid"],
    THRESHOLD_PRESETS["mid"],
)
# QSettings の保存先（voucher_window.py と同じ Org/App を共用する）。
SETTINGS_ORG = "Manekiya"
SETTINGS_APP = "TksToKintone"
SETTINGS_THRESHOLD_R = "voucher_edit/threshold_r"
SETTINGS_THRESHOLD_G = "voucher_edit/threshold_g"
SETTINGS_THRESHOLD_B = "voucher_edit/threshold_b"
# ユーザーが任意に保存できるカスタム閾値（要件3〜6）。
SETTINGS_CUSTOM_THRESHOLD_R = "voucher_edit/custom_threshold_r"
SETTINGS_CUSTOM_THRESHOLD_G = "voucher_edit/custom_threshold_g"
SETTINGS_CUSTOM_THRESHOLD_B = "voucher_edit/custom_threshold_b"
# 左ペインのボタン表記。rembg 用と閾値用を2行表示で見分けられるようにする（要件4・5）。
TRANSPARENT_REMBG_LABEL = "背景を透過\n（rembg）"
TRANSPARENT_THRESHOLD_LABEL = "背景を透過\n（閾値）"

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
QToolBar QToolButton[editToolButton="true"]:checked {
    background-color: #0d6efd;
    color: #ffffff;
    border: 2px solid #66b2ff;
    font-weight: bold;
}
QToolBar QToolButton[editToolButton="true"]:checked:disabled {
    background-color: #52606d;
    color: #e1e6eb;
    border: 1px solid #697580;
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

# タブレット編集モード用ツールバーのスタイル（要件: 高さ48px以上・横幅80px以上・
# 文字大きめ・間隔広め）。ライト/ダークどちらのテーマでも視認できるよう、配色を
# 固定の濃色背景＋白文字にする（テーマのパレットに依存しない）。
TABLET_TOOLBAR_STYLE = """
QToolBar#tabletToolBar {
    spacing: 4px;
    padding: 4px;
    background-color: #23282d;
    border: none;
}
QToolBar#tabletToolBar QToolButton {
    min-height: 44px;
    min-width: 64px;
    font-size: 14px;
    font-weight: bold;
    color: #ffffff;
    background-color: #3a4047;
    border: 1px solid #8a939c;
    border-radius: 8px;
    padding: 4px 8px;
    margin: 2px;
}
QToolBar#tabletToolBar QToolButton:hover {
    background-color: #4a525a;
    border: 1px solid #b0b9c2;
}
QToolBar#tabletToolBar QToolButton:pressed {
    background-color: #2aa8ff;
}
QToolBar#tabletToolBar QToolButton:checked {
    background-color: #0d6efd;
    color: #ffffff;
    border: 2px solid #66b2ff;
}
QToolBar#tabletToolBar QToolButton#tabletSaveButton {
    background-color: #0b7a3b;
    border: 1px solid #075c2d;
}
QToolBar#tabletToolBar QToolButton#tabletSaveButton:hover { background-color: #109149; }
QToolBar#tabletToolBar QToolButton#tabletDeleteButton {
    background-color: #c62828;
    border: 1px solid #8e0000;
}
QToolBar#tabletToolBar QToolButton#tabletDeleteButton:hover { background-color: #d32f2f; }
QToolBar#tabletToolBar QToolButton#tabletExitButton {
    background-color: #5a4a00;
    border: 1px solid #8a7400;
}
QToolBar#tabletToolBar QToolButton#tabletExitButton:hover { background-color: #7a6400; }
"""

REFLECT_TARGET_SELECTED_STYLE = """
QPushButton {
    background-color: #0d6efd;
    color: #ffffff;
    border: 2px solid #66b2ff;
    border-radius: 6px;
    font-weight: bold;
    padding: 5px 10px;
}
QPushButton:hover {
    background-color: #0b5ed7;
}
"""

REFLECT_TARGET_LIGHT_STYLE = """
QPushButton {
    background-color: #1f7a8c;
    color: #ffffff;
    border: 1px solid #166575;
    border-radius: 6px;
    font-weight: normal;
    padding: 5px 10px;
}
QPushButton:hover {
    background-color: #2a96ac;
}
"""

REFLECT_TARGET_DARK_STYLE = """
QPushButton {
    background-color: #546e7a;
    color: #ffffff;
    border: 1px solid #455a64;
    border-radius: 6px;
    font-weight: normal;
    padding: 5px 10px;
}
QPushButton:hover {
    background-color: #607d8b;
}
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
                 manual_resized: bool = False,
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__(text)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
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
            "target_vouchers": list(self.target_vouchers),
            "color": list(DEFAULT_COLOR),
        }


class _EditSymbolTextItem(QGraphicsSimpleTextItem):
    """短い注記用の点アンカーテキスト。scene座標の中心点を保存する。"""

    def __init__(self, text: str = "", obj_id: str | None = None,
                 font_size: float = DEFAULT_FONT_SIZE,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 anchor: str = "center",
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__(text.strip())
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
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
            "target_vouchers": list(self.target_vouchers),
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
                 vertical_align: str = "middle",
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__(rect)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
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
            "target_vouchers": list(self.target_vouchers),
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
                 vertical_align: str = "middle",
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__(rect)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
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
            "target_vouchers": list(self.target_vouchers),
            "color": list(DEFAULT_COLOR),
        }


class _EditLineItem(QGraphicsLineItem):
    """ドラッグ始点〜終点で作成する線。

    line_type により直線/矢印/両矢印/二重線を描き分ける。矢じり線分や二重平行線の
    座標計算は line_decorations に集約し、PDF出力と同じロジックを使う。
    """

    def __init__(self, x1: float, y1: float, x2: float, y2: float,
                 obj_id: str | None = None,
                 line_width: float = DEFAULT_LINE_WIDTH,
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 font_size: float = DEFAULT_FONT_SIZE,
                 line_type: str = LINE_TYPE_LINE,
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__(x1, y1, x2, y2)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
        self.line_width = float(line_width)
        self.font_size = float(font_size)
        self.line_type = normalize_line_type(line_type)
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
        self.prepareGeometryChange()

    def apply_font_size(self, font_size: float) -> None:
        self.font_size = float(font_size)

    def boundingRect(self) -> QRectF:  # noqa: N802
        # 矢じりがクリップされないよう、装飾分の余白を確保する。
        margin = self.line_width + ARROW_HEAD_LENGTH + DOUBLE_LINE_GAP
        return super().boundingRect().adjusted(-margin, -margin, margin, margin)

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        ln = self.line()
        pen = self.pen()
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for sx1, sy1, sx2, sy2 in line_segments(
                self.line_type, ln.x1(), ln.y1(), ln.x2(), ln.y2()):
            painter.drawLine(QLineF(sx1, sy1, sx2, sy2))
        # 既存の線と同じく、選択時は破線の枠で選択状態を示す。
        if option.state & QStyle.StateFlag.State_Selected:
            sel = QPen(QColor(0, 120, 215))
            sel.setStyle(Qt.PenStyle.DashLine)
            sel.setCosmetic(True)
            painter.setPen(sel)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(super().boundingRect())

    def serialize_edit_object(self) -> dict[str, Any]:
        ln = self.line()
        p1 = self.mapToScene(ln.p1())
        p2 = self.mapToScene(ln.p2())
        return {
            "id": self.obj_id,
            "type": "line",
            "line_type": self.line_type,
            "x1": float(p1.x()), "y1": float(p1.y()),
            "x2": float(p2.x()), "y2": float(p2.y()),
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "line_width": self.line_width,
            "font_size": self.font_size,
            "stroke_color": self.stroke_color,
            "target_vouchers": list(self.target_vouchers),
            "color": list(DEFAULT_COLOR),
        }


class _EditFreehandItem(QGraphicsPathItem):
    """手書きフリーハンド（自由曲線）。

    押下〜移動中に取得した連続座標(points, scene座標)を保持し、QPainterPath で
    滑らかに描画する。保存形式は type="freehand" の points 配列（ポリライン）。
    通常モードでも表示・選択・移動・削除・保存・PDF出力の対象になる。
    """

    def __init__(self, points: list, obj_id: str | None = None,
                 pen_width: float = DEFAULT_PEN_WIDTH,
                 stroke_color: str = DEFAULT_PEN_COLOR,
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__()
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
        self.pen_width = float(pen_width)
        self.stroke_color = _color_name(stroke_color)
        self._points: list[QPointF] = [
            QPointF(float(p[0]), float(p[1])) for p in points
        ]
        self.setData(_DATA_TYPE, "freehand")
        self.apply_line_width(self.pen_width)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self._rebuild_path()

    @staticmethod
    def _build_smooth_path(points: list[QPointF]) -> QPainterPath:
        """中点を結ぶ2次ベジェで滑らかな手書き曲線パスを作る。"""
        path = QPainterPath()
        if not points:
            return path
        path.moveTo(points[0])
        if len(points) == 1:
            # 1点だけのタップは微小線分で見えるようにする。
            path.lineTo(points[0].x() + 0.01, points[0].y())
            return path
        if len(points) == 2:
            path.lineTo(points[1])
            return path
        for i in range(1, len(points) - 1):
            mid = QPointF((points[i].x() + points[i + 1].x()) / 2.0,
                          (points[i].y() + points[i + 1].y()) / 2.0)
            path.quadTo(points[i], mid)
        path.lineTo(points[-1])
        return path

    def _rebuild_path(self) -> None:
        self.prepareGeometryChange()
        self.setPath(self._build_smooth_path(self._points))

    def add_point(self, scene_pos: QPointF) -> None:
        self._points.append(QPointF(scene_pos))
        self._rebuild_path()

    def points(self) -> list[QPointF]:
        return list(self._points)

    def apply_line_width(self, line_width: float) -> None:
        self.pen_width = float(line_width)
        pen = QPen(QColor(self.stroke_color))
        pen.setWidthF(self.pen_width)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(pen)
        self.prepareGeometryChange()

    def apply_font_size(self, font_size: float) -> None:
        # フリーハンドはフォント非対応。共通UIからの呼び出し互換のため何もしない。
        return

    def serialize_edit_object(self) -> dict[str, Any]:
        # 移動済みでも正しい scene 座標になるよう mapToScene で変換する。
        pts = [[float(self.mapToScene(p).x()), float(self.mapToScene(p).y())]
               for p in self._points]
        return {
            "id": self.obj_id,
            "type": "freehand",
            "points": pts,
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "pen_width": self.pen_width,
            "line_width": self.pen_width,
            "stroke_color": self.stroke_color,
            "target_vouchers": list(self.target_vouchers),
            "color": list(DEFAULT_COLOR),
        }


class _EditFreehandLayerItem(QGraphicsItem):
    """タブレット編集用の手書きレイヤー（複数ストロークを1オブジェクトで管理）。

    1本ごとの手書き線（stroke）を保持し、レイヤー単位で1つの大きな手書きオブジェクト
    として扱う（要件2）。各 stroke は points / pen_width / stroke_color を持つ。
    保存形式は type="freehand_layer"。座標は scene 座標（左上原点）。

    アイテム位置は常に (0,0) に固定し、移動・選択はしない（手書き中の誤操作を防ぐ）。
    そのため scene 座標とアイテム座標は一致し、消しゴム判定もそのまま行える。
    """

    def __init__(self, layer_id: str | None = None,
                 layer_name: str = "レイヤー1",
                 target_vouchers: list[str] | None = None,
                 pen_width: float = DEFAULT_PEN_WIDTH,
                 stroke_color: str = DEFAULT_PEN_COLOR,
                 visible: bool = True, locked: bool = False,
                 strokes: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.obj_id = layer_id or str(uuid.uuid4())
        self.layer_id = self.obj_id
        self.layer_name = str(layer_name) or "レイヤー1"
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
        self.pen_width = float(pen_width)
        self.stroke_color = _color_name(stroke_color)
        self.locked = bool(locked)
        self._strokes: list[dict[str, Any]] = []
        for s in (strokes or []):
            self._append_stroke_data(s)
        self.setData(_DATA_TYPE, "freehand_layer")
        # 手書きレイヤーは選択・移動しない（描画専用の大きなオブジェクト）。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setVisible(bool(visible))
        self._bounds = QRectF(0, 0, PAGE_W, PAGE_H)
        self._recompute_bounds()

    def _append_stroke_data(self, s: dict[str, Any]) -> None:
        pts = s.get("points") or []
        points = [QPointF(float(p[0]), float(p[1])) for p in pts
                  if isinstance(p, (list, tuple)) and len(p) >= 2]
        self._strokes.append({
            "points": points,
            "pen_width": float(s.get("pen_width") or self.pen_width),
            "stroke_color": _color_name(s.get("stroke_color") or self.stroke_color),
        })

    def _recompute_bounds(self) -> None:
        rect = QRectF()
        for stroke in self._strokes:
            for p in stroke["points"]:
                r = QRectF(p.x(), p.y(), 0.01, 0.01)
                rect = r if rect.isNull() else rect.united(r)
        if rect.isNull():
            # 空レイヤーはページ全体を範囲にしておく（クリック検出は無効なので無害）。
            rect = QRectF(0, 0, PAGE_W, PAGE_H)
        margin = max(self.pen_width, 8.0) + 4.0
        self._bounds = rect.adjusted(-margin, -margin, margin, margin)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._bounds

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        for stroke in self._strokes:
            points = stroke["points"]
            if not points:
                continue
            pen = QPen(QColor(stroke["stroke_color"]))
            pen.setWidthF(float(stroke["pen_width"]))
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(_EditFreehandItem._build_smooth_path(points))

    def stroke_count(self) -> int:
        return len(self._strokes)

    def add_stroke(self, points: list, pen_width: float | None = None,
                   stroke_color: str | None = None) -> None:
        """このレイヤーへ1本のストロークを追加する（要件2）。"""
        pts = [QPointF(float(p[0]), float(p[1])) for p in points]
        if len(pts) < 2:
            return
        self.prepareGeometryChange()
        self._strokes.append({
            "points": pts,
            "pen_width": float(self.pen_width if pen_width is None else pen_width),
            "stroke_color": _color_name(
                self.stroke_color if stroke_color is None else stroke_color),
        })
        self._recompute_bounds()
        self.update()

    def erase_near(self, scene_pos: QPointF, radius: float = ERASER_RADIUS) -> bool:
        """消しゴム位置に近いストロークだけを削除する（要件3: 選択中レイヤー対象）。

        アイテムは (0,0) 固定のため scene 座標がそのままアイテム座標になる。
        """
        remaining: list[dict[str, Any]] = []
        removed = False
        for stroke in self._strokes:
            hit = False
            for p in stroke["points"]:
                if (abs(p.x() - scene_pos.x()) <= radius
                        and abs(p.y() - scene_pos.y()) <= radius):
                    hit = True
                    break
            if hit:
                removed = True
            else:
                remaining.append(stroke)
        if removed:
            self.prepareGeometryChange()
            self._strokes = remaining
            self._recompute_bounds()
            self.update()
        return removed

    def apply_line_width(self, line_width: float) -> None:
        # 共通UIからの呼び出し互換。レイヤー既定の太さ（以後の新規ストローク）を更新する。
        self.pen_width = float(line_width)

    def apply_font_size(self, font_size: float) -> None:
        # フォント非対応。共通UI互換のため何もしない。
        return

    def serialize_edit_object(self) -> dict[str, Any]:
        strokes = []
        for stroke in self._strokes:
            strokes.append({
                "points": [[float(p.x()), float(p.y())] for p in stroke["points"]],
                "pen_width": float(stroke["pen_width"]),
                "stroke_color": stroke["stroke_color"],
            })
        return {
            "id": self.obj_id,
            "type": "freehand_layer",
            "layer_id": self.layer_id,
            "layer_name": self.layer_name,
            "coordinate_origin": COORDINATE_ORIGIN,
            "geometry_basis": GEOMETRY_BASIS,
            "pen_width": self.pen_width,
            "line_width": self.pen_width,
            "stroke_color": self.stroke_color,
            "visible": bool(self.isVisible()),
            "locked": bool(self.locked),
            "strokes": strokes,
            "target_vouchers": list(self.target_vouchers),
            "color": list(DEFAULT_COLOR),
        }


def is_debug_visible() -> bool:
    """ランチャー設定「デバッグ表示」がONかを返す（要件2）。

    ランチャーが保存する QSettings("Manekiya", "TksToKintone") の "ui/debug_visible"
    を読む。未設定や読み取り失敗時は False（非表示）。
    """
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings("Manekiya", "TksToKintone")
        raw = settings.value("ui/debug_visible", "0")
    except Exception:  # pragma: no cover - 設定読み取り失敗時は非表示
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


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


def _clamp_threshold(value: object, default: int) -> int:
    """0〜255 に収めた int を返す。不正値は default。"""
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(0, min(255, v))


def load_threshold_rgb() -> tuple[int, int, int]:
    """保存済みの RGB しきい値を返す。未保存なら既定（中）（要件9）。"""
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    r = _clamp_threshold(settings.value(SETTINGS_THRESHOLD_R), DEFAULT_THRESHOLD_RGB[0])
    g = _clamp_threshold(settings.value(SETTINGS_THRESHOLD_G), DEFAULT_THRESHOLD_RGB[1])
    b = _clamp_threshold(settings.value(SETTINGS_THRESHOLD_B), DEFAULT_THRESHOLD_RGB[2])
    return (r, g, b)


def save_threshold_rgb(rgb: tuple[int, int, int]) -> None:
    """RGB しきい値を保存する。画面を閉じても保持される（要件9）。"""
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue(SETTINGS_THRESHOLD_R, _clamp_threshold(rgb[0], DEFAULT_THRESHOLD_RGB[0]))
    settings.setValue(SETTINGS_THRESHOLD_G, _clamp_threshold(rgb[1], DEFAULT_THRESHOLD_RGB[1]))
    settings.setValue(SETTINGS_THRESHOLD_B, _clamp_threshold(rgb[2], DEFAULT_THRESHOLD_RGB[2]))


def load_custom_threshold_rgb() -> tuple[int, int, int] | None:
    """保存済みのカスタム RGB しきい値を返す。未保存なら None（要件5・6）。"""
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    r = settings.value(SETTINGS_CUSTOM_THRESHOLD_R)
    g = settings.value(SETTINGS_CUSTOM_THRESHOLD_G)
    b = settings.value(SETTINGS_CUSTOM_THRESHOLD_B)
    if r is None or g is None or b is None:
        return None
    return (
        _clamp_threshold(r, DEFAULT_THRESHOLD_RGB[0]),
        _clamp_threshold(g, DEFAULT_THRESHOLD_RGB[1]),
        _clamp_threshold(b, DEFAULT_THRESHOLD_RGB[2]),
    )


def save_custom_threshold_rgb(rgb: tuple[int, int, int]) -> None:
    """カスタム RGB しきい値を保存する。再起動後も読み込める（要件6）。"""
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue(SETTINGS_CUSTOM_THRESHOLD_R, _clamp_threshold(rgb[0], DEFAULT_THRESHOLD_RGB[0]))
    settings.setValue(SETTINGS_CUSTOM_THRESHOLD_G, _clamp_threshold(rgb[1], DEFAULT_THRESHOLD_RGB[1]))
    settings.setValue(SETTINGS_CUSTOM_THRESHOLD_B, _clamp_threshold(rgb[2], DEFAULT_THRESHOLD_RGB[2]))


def is_light_background_pixel(
    r: int, g: int, b: int, threshold_rgb: tuple[int, int, int]
) -> bool:
    """ピクセルが「白に近い背景」かを輝度で判定する（要件3・4）。

    全チャンネル一致ではなく輝度（ITU-R BT.601）で比較する。スマホ撮影の紙背景は
    グレー・黄ばみ・影で純白にならないため、輝度がしきい値平均以上なら背景扱いにする。
    """
    brightness = int(0.299 * r + 0.587 * g + 0.114 * b)
    threshold = int(sum(threshold_rgb) / 3)
    return brightness >= threshold


def make_binarized_bytes(image_bytes: bytes, threshold_rgb: tuple[int, int, int]) -> bytes:
    """各ピクセルを白/黒へ二値化した PNG バイト列を返す（要件6）。

    輝度がしきい値平均以上なら白、未満なら黒にする。元の alpha は維持する。
    画像の寸法は変えないため、位置・サイズ・倍率は不変のまま差し替えられる。
    """
    if not image_bytes:
        return bytes(image_bytes)
    image = QImage()
    image.loadFromData(bytes(image_bytes))
    if image.isNull():
        return bytes(image_bytes)
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            alpha = c.alpha()
            if is_light_background_pixel(c.red(), c.green(), c.blue(), threshold_rgb):
                image.setPixelColor(x, y, QColor(255, 255, 255, alpha))
            else:
                image.setPixelColor(x, y, QColor(0, 0, 0, alpha))
    return qimage_to_png_bytes(image)


def make_threshold_transparent_bytes(
    image_bytes: bytes, threshold_rgb: tuple[int, int, int]
) -> bytes:
    """白・薄いグレー背景を輝度判定で透明化した PNG バイト列を返す（要件7）。

    輝度がしきい値平均以上なら alpha=0（透明）、それ以外は元の色・元の alpha のまま残す。
    黒線・文字・図形は残り、白〜薄いグレーの背景だけが透明になる。寸法は不変。
    """
    if not image_bytes:
        return bytes(image_bytes)
    image = QImage()
    image.loadFromData(bytes(image_bytes))
    if image.isNull():
        return bytes(image_bytes)
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if is_light_background_pixel(c.red(), c.green(), c.blue(), threshold_rgb):
                image.setPixelColor(x, y, QColor(c.red(), c.green(), c.blue(), 0))
    return qimage_to_png_bytes(image)


class ThresholdSettingsDialog(QDialog):
    """RGB しきい値を設定するダイアログ（要件3〜9）。

    各成分は スライダー＋数値入力(QSpinBox, 0〜255) で双方向同期する。
    低/中/高 プリセットと カスタム ボタン（カスタム値の反映／現在値の保存）を持つ。
    OK で通常の閾値として保存、Cancel では通常閾値を変更しない（カスタム保存は維持）。
    """

    def __init__(self, rgb: tuple[int, int, int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("閾値設定")
        # 数値入力欄が潰れないようダイアログ幅を確保する（要件5）。
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        self._sliders: dict[str, QSlider] = {}
        self._spins: dict[str, QSpinBox] = {}
        # スライダー⇔数値入力の相互更新でシグナルが往復しないよう抑止するフラグ。
        self._syncing = False
        for key, caption, initial in (
            ("r", "R", rgb[0]),
            ("g", "G", rgb[1]),
            ("b", "B", rgb[2]),
        ):
            row = QHBoxLayout()
            name = QLabel(caption)
            name.setFixedWidth(16)
            value = _clamp_threshold(initial, DEFAULT_THRESHOLD_RGB[0])
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 255)
            slider.setValue(value)
            # スライダーが横幅を取りすぎて数値欄を潰さないよう最小幅を抑える（要件5）。
            slider.setMinimumWidth(180)
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setValue(value)
            # 0/60/90/120/255 等の数値が必ず見える幅を確保する（要件4）。
            spin.setMinimumWidth(72)
            slider.valueChanged.connect(
                lambda v, k=key: self._on_slider_changed(k, v)
            )
            spin.valueChanged.connect(
                lambda v, k=key: self._on_spin_changed(k, v)
            )
            row.addWidget(name)
            row.addWidget(slider, 1)
            row.addWidget(spin)
            layout.addLayout(row)
            self._sliders[key] = slider
            self._spins[key] = spin

        preset_row = QHBoxLayout()
        for caption, preset in (("低", "low"), ("中", "mid"), ("高", "high")):
            btn = QPushButton(caption)
            btn.clicked.connect(lambda _=False, p=preset: self._apply_preset(p))
            preset_row.addWidget(btn)
        custom_btn = QPushButton("カスタム")
        custom_btn.clicked.connect(self._on_custom)
        preset_row.addWidget(custom_btn)
        layout.addLayout(preset_row)
        # プリセットの意味を表示し、実機調整しやすくする（要件9）。
        hint = QLabel("低：背景を広く消す　／　中：標準　／　高：白い部分だけ消す")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_channel(self, key: str, value: int) -> None:
        """スライダーと数値入力の両方を同じ値へ更新する（往復シグナル抑止）。"""
        value = _clamp_threshold(value, DEFAULT_THRESHOLD_RGB[0])
        self._syncing = True
        try:
            self._sliders[key].setValue(value)
            self._spins[key].setValue(value)
        finally:
            self._syncing = False

    def _on_slider_changed(self, key: str, value: int) -> None:
        """スライダー変更時：対応する数値入力も更新する（要件7）。"""
        if self._syncing:
            return
        self._set_channel(key, value)

    def _on_spin_changed(self, key: str, value: int) -> None:
        """数値入力変更時：対応するスライダーも更新する（要件7）。"""
        if self._syncing:
            return
        self._set_channel(key, value)

    def set_values(self, rgb: tuple[int, int, int]) -> None:
        """R/G/B のスライダーと数値入力を一括更新する（要件7）。"""
        for key, value in zip(("r", "g", "b"), rgb):
            self._set_channel(key, value)

    def _apply_preset(self, preset: str) -> None:
        """低/中/高 ボタン：R/G/B を一括設定する（要件7・9）。"""
        value = THRESHOLD_PRESETS.get(preset, DEFAULT_THRESHOLD_RGB[0])
        self.set_values((value, value, value))

    def _on_custom(self) -> None:
        """カスタムボタン：カスタム値の反映／現在値の保存を選ばせる（要件4〜6）。"""
        box = QMessageBox(self)
        box.setWindowTitle("カスタム")
        box.setText(
            "カスタム値を反映しますか？\n現在のRGB値をカスタムに保存しますか？"
        )
        apply_btn = box.addButton("カスタム値を反映", QMessageBox.ButtonRole.AcceptRole)
        save_btn = box.addButton("現在値を保存", QMessageBox.ButtonRole.ActionRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is apply_btn:
            self._apply_custom_values()
        elif clicked is save_btn:
            self._save_current_as_custom()

    def _apply_custom_values(self) -> None:
        """保存済みカスタム値を R/G/B へ反映する。未保存なら通知のみ（要件5）。"""
        custom = load_custom_threshold_rgb()
        if custom is None:
            QMessageBox.information(self, "カスタム", "カスタム値が保存されていません。")
            return
        self.set_values(custom)

    def _save_current_as_custom(self) -> None:
        """現在の R/G/B 値をカスタム値として保存する（要件6）。"""
        save_custom_threshold_rgb(self.values())
        QMessageBox.information(
            self, "カスタム", "現在のRGB値をカスタムに保存しました。"
        )

    def values(self) -> tuple[int, int, int]:
        """現在のスライダー値を (R, G, B) で返す。"""
        return (
            self._sliders["r"].value(),
            self._sliders["g"].value(),
            self._sliders["b"].value(),
        )


class BackgroundRemovalError(RuntimeError):
    """rembg による背景除去に失敗したことを表す例外（要件4）。"""


def _rembg_model_dir() -> Path:
    """同梱した rembg モデル（u2net.onnx）の配置フォルダを返す（要件4）。

    exe では sys._MEIPASS 配下、開発時はリポジトリ直下の assets/rembg を指す。
    """
    return resource_path("assets/rembg")


def _png_bytes_same_size(raw: bytes) -> bytes:
    """rembg の出力を ARGB32 の PNG バイト列へ正規化する。

    画像の寸法はそのまま（rembg は入力と同じ縦横サイズを返す）なので、
    位置・サイズ・倍率は不変のまま透過済み画像へ差し替えられる。
    """
    image = QImage()
    image.loadFromData(bytes(raw))
    if image.isNull():
        # 既に PNG バイト列ならそのまま返す（QImage が解釈できない形式の保険）。
        return bytes(raw)
    return qimage_to_png_bytes(image.convertToFormat(QImage.Format.Format_ARGB32))


def _ensure_pyinstaller_metadata_path() -> None:
    """PyInstaller 展開先を importlib.metadata の探索対象へ入れる。"""
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    base_s = str(base)
    if base_s not in sys.path:
        sys.path.insert(0, base_s)


def _make_transparent_background_bytes_once(image_bytes: bytes) -> bytes:
    if not image_bytes:
        return bytes(image_bytes)
    _ensure_pyinstaller_metadata_path()
    # 同梱モデルのあるフォルダを U2NET_HOME に設定し、オフラインでもダウンロード
    # 不要にする。既にユーザーが設定済みなら尊重する（setdefault）（要件4）。
    model_dir = _rembg_model_dir()
    os.environ.setdefault("U2NET_HOME", str(model_dir))
    # remove() 前にモデルの存在を確認し、不足時は原因が分かるエラーにする（要件5）。
    model_path = model_dir / "u2net.onnx"
    if not model_path.exists():
        raise BackgroundRemovalError(
            f"背景透過モデルが見つかりません: {model_path}"
        )
    try:
        import importlib.metadata as metadata

        metadata.version("pymatting")
        from rembg import remove
    except Exception as exc:  # rembg / onnxruntime 未導入（要件3・4）
        raise BackgroundRemovalError(
            f"rembg の読み込みに失敗しました: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        output = remove(bytes(image_bytes))
        return _png_bytes_same_size(output)
    except Exception as exc:  # rembg 実行失敗（要件4）
        raise BackgroundRemovalError(
            f"背景透過処理に失敗しました: {type(exc).__name__}: {exc}"
        ) from exc


def make_transparent_background_bytes(image_bytes: bytes) -> bytes:
    """rembg で背景を除去した透過済みPNGバイト列を返す（要件1・2）。

    薄いグレー背景・影も含めて被写体以外を透明化する。画像の寸法は変えないため、
    位置・サイズ・倍率は不変。pymatting の metadata は rembg import 前に確認し、
    PyInstaller 環境では展開先を補正して PackageNotFoundError だけ1回再試行する。
    """
    try:
        return _make_transparent_background_bytes_once(image_bytes)
    except BackgroundRemovalError as exc:
        cause = exc.__cause__
        try:
            import importlib.metadata as metadata
            package_missing = isinstance(cause, metadata.PackageNotFoundError)
        except Exception:
            package_missing = False
        if not package_missing:
            raise
        _ensure_pyinstaller_metadata_path()
        return _make_transparent_background_bytes_once(image_bytes)


class RembgWarmupWorker(QObject):
    """rembg / pymatting の初回 metadata/import 解決を別スレッドで温める。"""

    finished = Signal()

    @Slot()
    def run(self) -> None:
        try:
            _ensure_pyinstaller_metadata_path()
            import importlib.metadata as metadata

            metadata.version("pymatting")
            from rembg import remove  # noqa: F401
        except Exception:
            _log.debug("rembg warmup failed", exc_info=True)
        self.finished.emit()


class BackgroundRemovalWorker(QObject):
    """rembg による背景透過を別スレッドで実行する worker（要件1・4）。

    rembg.remove() は重く、UIスレッドで実行すると画面が固まる/白くなる。worker は
    画像バイト列を作るだけで、QGraphicsItem の差し替えは一切行わない（GUIオブジェクト
    はメインスレッドでのみ触る）。結果は finished / failed シグナルで通知する。
    """

    finished = Signal(bytes)
    failed = Signal(str)

    def __init__(self, image_bytes: bytes) -> None:
        super().__init__()
        self._image_bytes = bytes(image_bytes)

    @Slot()
    def run(self) -> None:
        try:
            result = make_transparent_background_bytes(self._image_bytes)
        except Exception as exc:  # noqa: BLE001 - 失敗内容は文字列で通知する
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class _EditImageItem(QGraphicsPixmapItem):
    """挿入/貼り付けした画像。移動・サイズ変更・保存に対応する（要件2-3・2-4）。

    画像実体は PNG バイト列で保持し、保存時に base64 化する。ファイルパスは
    保持しない（元ファイル消失でも復元できるようにするため）。
    """

    def __init__(self, image_bytes: bytes, image_format: str = "png",
                 obj_id: str | None = None,
                 width: float | None = None, height: float | None = None,
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__()
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
        self.image_bytes = bytes(image_bytes)
        self.image_format = (image_format or "png").lower()
        # 画像加工（rembg／二値化／背景を透過（閾値））前の元画像。最初の加工時に
        # 保持し、「背景を戻す」で復元する（要件4・6・7・10）。複数回どの加工をしても
        # 最初の元画像へ戻せるよう、一度だけ保存する。
        self._original_image_bytes: bytes | None = None
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

    def _replace_image_bytes(self, image_bytes: bytes) -> None:
        """画像実体を差し替える。box_w/box_h は維持するため位置・サイズ・倍率は不変。"""
        self.image_bytes = bytes(image_bytes)
        self.image_format = "png"
        pixmap = QPixmap()
        if self.image_bytes:
            pixmap.loadFromData(self.image_bytes)
        self._pixmap = pixmap
        self.setPixmap(pixmap)
        self._apply_scale()

    def has_original_image(self) -> bool:
        """透過前の元画像を保持しているか（「背景を戻す」の有効/無効判定: 要件5）。"""
        return self._original_image_bytes is not None

    def apply_processed_image(self, processed_bytes: bytes) -> None:
        """加工済みバイト列で画像を差し替える（rembg／二値化／閾値透過 共通）。

        位置・サイズ・倍率は box_w/box_h 維持により不変。元画像退避は「成功時だけ」
        確定する（失敗時に退避が残らないようにする: 要件6・7）。複数回どの加工をしても
        最初の元画像へ戻せるよう、退避は一度だけ行う（要件10・11）。
        """
        if self._original_image_bytes is None:
            self._original_image_bytes = bytes(self.image_bytes)
        self._replace_image_bytes(processed_bytes)

    def apply_background_removal_result(self, transparent_bytes: bytes) -> None:
        """rembg worker が生成した透過済みバイト列で画像を差し替える（要件1・6）。"""
        self.apply_processed_image(transparent_bytes)

    def restore_original_image(self) -> None:
        """退避してある透過前の元画像へ復元する（要件4）。元画像が無ければ何もしない。"""
        if self._original_image_bytes is None:
            return
        self._replace_image_bytes(self._original_image_bytes)

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
            "target_vouchers": list(self.target_vouchers),
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
    """選択中アイテムの周囲に表示するサイズ変更ハンドル。

    見た目は HANDLE_SIZE（小）だが、クリック判定は HANDLE_HIT_SIZE（大）にして
    掴みやすくする。boundingRect()/shape() を拡大判定にすることで、押下時に
    画像本体ではなくハンドルがマウスイベントを確実に受け取る（不具合1）。
    """

    SIZE = HANDLE_SIZE
    # 補助アイテムの目印。背景/編集オブジェクトと区別し、保存・全選択対象外にする。
    _IS_HELPER = True
    CORNERS = {"top_left", "top_right", "bottom_left", "bottom_right"}
    EDGES = {"top", "bottom", "left", "right"}

    def __init__(self, target, position: str = "bottom_right") -> None:
        s = HANDLE_SIZE
        super().__init__(-s / 2, -s / 2, s, s)
        self._target = target
        self._position = position if position in self.CORNERS | self.EDGES else "bottom_right"
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
        self.setCursor(self._cursor_for_position())
        self.reposition()

    def _cursor_for_position(self):
        if self._position in {"top_left", "bottom_right"}:
            return Qt.CursorShape.SizeFDiagCursor
        if self._position in {"top_right", "bottom_left"}:
            return Qt.CursorShape.SizeBDiagCursor
        if self._position in {"left", "right"}:
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

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
        points = {
            "top_left": QPointF(br.left(), br.top()),
            "top": QPointF(br.center().x(), br.top()),
            "top_right": QPointF(br.right(), br.top()),
            "right": QPointF(br.right(), br.center().y()),
            "bottom_right": QPointF(br.right(), br.bottom()),
            "bottom": QPointF(br.center().x(), br.bottom()),
            "bottom_left": QPointF(br.left(), br.bottom()),
            "left": QPointF(br.left(), br.center().y()),
        }
        self._suppress = True
        self.setPos(points[self._position])
        self._suppress = False

    def itemChange(self, change, value):  # noqa: N802
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionChange
                and not self._suppress and self.scene() is not None):
            self._resize_target(value)
        return super().itemChange(change, value)

    def _resize_target(self, handle_scene_pos: QPointF) -> None:
        tgt = self._target
        current = self._target_rect()
        new_rect = self._resized_rect(current, handle_scene_pos)
        if isinstance(tgt, (_EditRectItem, _EditEllipseItem)):
            local_tl = tgt.mapFromScene(new_rect.topLeft())
            tgt.setRect(QRectF(local_tl.x(), local_tl.y(),
                               new_rect.width(), new_rect.height()))
        elif isinstance(tgt, _EditTextItem):
            tgt.setPos(new_rect.topLeft())
            tgt.set_manual_box_size(new_rect.width(), new_rect.height())
        elif isinstance(tgt, _EditImageItem):
            tgt.setPos(new_rect.topLeft())
            tgt.set_box_size(new_rect.width(), new_rect.height())

    def _target_rect(self) -> QRectF:
        tgt = self._target
        if isinstance(tgt, (_EditTextItem, _EditImageItem)):
            return tgt.box_rect_scene()
        if isinstance(tgt, (_EditRectItem, _EditEllipseItem)):
            return _scene_rect_from_item_rect(tgt, tgt.rect())
        return tgt.sceneBoundingRect()

    def _resized_rect(self, rect: QRectF, pos: QPointF) -> QRectF:
        min_w = MIN_TEXT_W if isinstance(self._target, _EditTextItem) else MIN_OBJECT_WIDTH
        min_h = MIN_TEXT_H if isinstance(self._target, _EditTextItem) else MIN_OBJECT_HEIGHT
        p = self._position
        left, right = rect.left(), rect.right()
        top, bottom = rect.top(), rect.bottom()
        if p == "left":
            left = min(pos.x(), right - min_w)
        elif p == "right":
            right = max(pos.x(), left + min_w)
        elif p == "top":
            top = min(pos.y(), bottom - min_h)
        elif p == "bottom":
            bottom = max(pos.y(), top + min_h)
        elif p in self.CORNERS:
            anchor = {
                "top_left": rect.bottomRight(),
                "top_right": rect.bottomLeft(),
                "bottom_left": rect.topRight(),
                "bottom_right": rect.topLeft(),
            }[p]
            sx = -1.0 if "left" in p else 1.0
            sy = -1.0 if "top" in p else 1.0
            raw_w = max(abs(pos.x() - anchor.x()), min_w)
            raw_h = max(abs(pos.y() - anchor.y()), min_h)
            aspect = rect.width() / rect.height() if rect.height() else 1.0
            if raw_h <= 0:
                raw_h = min_h
            if raw_w / raw_h > aspect:
                h = max(raw_w / aspect, min_h)
                w = max(raw_w, min_w)
            else:
                w = max(raw_h * aspect, min_w)
                h = max(raw_h, min_h)
            left = anchor.x() if sx > 0 else anchor.x() - w
            right = anchor.x() + w if sx > 0 else anchor.x()
            top = anchor.y() if sy > 0 else anchor.y() - h
            bottom = anchor.y() + h if sy > 0 else anchor.y()
        return QRectF(QPointF(left, top), QPointF(right, bottom)).normalized()

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
        # 作成中の手書きフリーハンドアイテム（ペンツール）。
        self._freehand_item: "_EditFreehandItem | None" = None
        # 消しゴムでドラッグ中か。True の間、なぞった位置の手書き線を削除する。
        self._erasing: bool = False

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
        # 作成中の手書きストロークも破棄する（確定前なので履歴・保存対象外）。
        if self._freehand_item is not None:
            if self._freehand_item.scene() is not None:
                self.removeItem(self._freehand_item)
            self._freehand_item = None
        self._erasing = False
        self._press_target = None
        self._press_multi = False
        self._press_pos = None
        self._press_handle = False

    def mousePressEvent(self, event) -> None:
        tool = self._window.current_tool
        pos = event.scenePos()
        # 掴むモード: ビューの ScrollHandDrag に任せてパンする（描画・選択はしない）。
        if tool == TOOL_GRAB:
            super().mousePressEvent(event)
            return
        # 手書きペン: 押下で新しいフリーハンドのストロークを開始する。
        if tool == TOOL_PEN and event.button() == Qt.MouseButton.LeftButton:
            self.begin_freehand(pos)
            event.accept()
            return
        # 消しゴム: 押下位置に近い手書き線を削除し、ドラッグ中も消し続ける。
        if tool == TOOL_ERASER and event.button() == Qt.MouseButton.LeftButton:
            self._erasing = True
            self._window.erase_freehand_at(pos)
            event.accept()
            return
        if (tool in (TOOL_TEXT, TOOL_RECT, TOOL_ELLIPSE) + LINE_TOOLS
                and event.button() == Qt.MouseButton.LeftButton
                and not self._hits_existing_object(pos)):
            # 空白部分での新規作成。既存選択は解除し、対象を絞る（要件6・7・10）。
            self.clearSelection()
            self._start = pos
            if tool in LINE_TOOLS:
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
        # 掴むモード: ビューの ScrollHandDrag に任せる。
        if self._window.current_tool == TOOL_GRAB:
            super().mouseMoveEvent(event)
            return
        # 手書きペン描画中: 移動座標を連続追加する。
        if self._freehand_item is not None:
            self._freehand_item.add_point(event.scenePos())
            self._window.ensure_background_visible()
            event.accept()
            return
        # 消しゴムドラッグ中: なぞった位置の手書き線を消し続ける。
        if self._erasing:
            self._window.erase_freehand_at(event.scenePos())
            event.accept()
            return
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

    def begin_freehand(self, scene_pos: QPointF) -> None:
        """手書きフリーハンドのストロークを開始する。"""
        self.clearSelection()
        item = self._window.add_freehand(
            [(scene_pos.x(), scene_pos.y())],
            pen_width=self._window.current_pen_width,
            stroke_color=self._window.current_pen_color,
            target_vouchers=list(self._window.current_target_vouchers),
            register_id=False,
        )
        self._freehand_item = item
        self._window.ensure_background_visible()

    def end_freehand(self) -> None:
        """ドラッグ終了で手書きストロークを確定する。

        タブレット編集モードでは現在の手書きレイヤーへ stroke として追加し、
        1ストロークごとに独立オブジェクトを作らない（要件2）。通常モードでは
        従来どおり1ストローク=1 freehand オブジェクトとして確定する。
        """
        item = self._freehand_item
        self._freehand_item = None
        if item is None:
            return
        pts = item.points()
        if self._window.tablet_mode:
            # プレビューを破棄し、ストロークを現在レイヤーへ追加する（要件2）。
            if item.scene() is not None:
                self.removeItem(item)
            if len(pts) >= 2:
                self._window.add_stroke_to_current_layer(
                    [(p.x(), p.y()) for p in pts],
                    pen_width=item.pen_width, stroke_color=item.stroke_color)
            return
        # 点が少なすぎる単なるタップはオブジェクト化しない。
        if len(pts) < 2:
            if item.scene() is not None:
                self.removeItem(item)
            return
        self._window.loaded_object_ids.add(item.obj_id)
        self._window.mark_dirty()
        self._window.commit_history()
        self._window.ensure_background_visible()

    def mouseReleaseEvent(self, event) -> None:
        # 掴むモード: ビューの ScrollHandDrag に任せる。
        if self._window.current_tool == TOOL_GRAB:
            super().mouseReleaseEvent(event)
            return
        # 手書きペン: ストローク確定（1ストローク=1Undo単位）。
        if self._freehand_item is not None:
            self.end_freehand()
            event.accept()
            return
        # 消しゴム: ドラッグ終了。削除があれば履歴へ積む。
        if self._erasing:
            self._erasing = False
            self._window.commit_eraser_if_changed()
            event.accept()
            return
        if self._temp_item is not None and self._start is not None:
            temp = self._temp_item
            start = self._start
            end = event.scenePos()
            self.removeItem(temp)
            self._temp_item = None
            self._start = None
            tool = self._window.current_tool
            created = False
            if tool in LINE_TOOLS:
                if self._manhattan_distance(start, end) >= 2.0:
                    self._window.add_line(
                        start, end, line_type=TOOL_TO_LINE_TYPE[tool])
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

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        target = self._resolve_edit_object(event.scenePos())
        if target is None:
            super().contextMenuEvent(event)
            return
        self._window._select_only(target)
        self._window._show_object_context_menu(target, event.screenPos())
        event.accept()


class _EditGraphicsView(QGraphicsView):
    """Ctrl+V をウィンドウへ確実に届ける編集用ビュー（要件2-4）。

    キーイベントはフォーカスのある QGraphicsView に届くため、ウィンドウ側の
    keyPressEvent だけでは Ctrl+V を取りこぼすことがある。ビューでも Paste を
    拾い、ウィンドウの handle_paste_shortcut() へ委譲する。テキスト編集中は
    handle_paste_shortcut() が False を返すので、通常のテキスト貼り付けに委ねる。
    """

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            window = self.window()
            handler = getattr(window, "copy_selected_objects", None)
            if callable(handler) and handler():
                event.accept()
                return
        if event.matches(QKeySequence.StandardKey.Paste):
            window = self.window()
            handler = getattr(window, "handle_paste_shortcut", None)
            if callable(handler) and handler():
                event.accept()
                return
        super().keyPressEvent(event)


class _TemplateRegisterDialog(QDialog):
    """反映先テンプレートを登録するダイアログ（要件4）。"""

    _COLOR_PRESETS = ["#ff9800", "#1976d2", "#7b1fa2", "#00897b", "#2e7d32", "#c62828"]

    def __init__(self, current_targets: list[str], parent: QWidget | None = None,
                 name: str = "", color: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("反映先テンプレート登録" if not name else "反映先テンプレート編集")
        layout = QVBoxLayout(self)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("テンプレート名:"))
        self._name_edit = QLineEdit(name)
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)

        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("バッヂ色(#rrggbb):"))
        self._color_edit = QLineEdit(color or self._COLOR_PRESETS[0])
        color_row.addWidget(self._color_edit, 1)
        layout.addLayout(color_row)

        group = QGroupBox("反映先伝票")
        group_layout = QVBoxLayout(group)
        self._checks: dict[str, QCheckBox] = {}
        for vid, vname in VOUCHER_TYPES:
            cb = QCheckBox(f"{vid} {vname}")
            cb.setChecked(vid in set(current_targets))
            self._checks[vid] = cb
            group_layout.addWidget(cb)
        layout.addWidget(group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def template(self) -> dict[str, Any] | None:
        name = self._name_edit.text().strip()
        targets = [vid for vid, cb in self._checks.items() if cb.isChecked()]
        if not name or not targets:
            return None
        color = self._color_edit.text().strip() or "#607d8b"
        return {"name": name, "target_vouchers": targets, "color": color, "badge": name[:1]}


class TabletScreenDialog(QDialog):
    """タブレット編集モードの表示先ディスプレイを選択するダイアログ（要件7）。

    QGuiApplication.screens() で取得した画面一覧をラジオボタンで表示する。
    前回選択した画面名（saved_name）があれば初期選択にする。「開始」で accept、
    「キャンセル」で reject する。選択結果は selected_screen() で取得する。
    """

    def __init__(self, screens: list, saved_name: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("表示先ディスプレイの選択")
        self._screens = list(screens)
        self._buttons: list[QRadioButton] = []
        self._group = QButtonGroup(self)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("表示先ディスプレイを選択してください。"))
        try:
            primary = QGuiApplication.primaryScreen()
        except Exception:  # pragma: no cover
            primary = None
        selected_index = 0
        for i, screen in enumerate(self._screens):
            name = self._screen_name(screen)
            geo = self._screen_size(screen)
            is_primary = screen is primary
            label = f"{i + 1}: {name} {geo}"
            if is_primary:
                label += "（メイン）"
            rb = QRadioButton(label)
            self._group.addButton(rb, i)
            self._buttons.append(rb)
            layout.addWidget(rb)
            if saved_name and name == saved_name:
                selected_index = i
        if self._buttons:
            self._buttons[selected_index].setChecked(True)
        buttons = QDialogButtonBox()
        start_btn = buttons.addButton("開始", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
        start_btn.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _screen_name(screen) -> str:
        try:
            return str(screen.name())
        except Exception:  # pragma: no cover
            return ""

    @staticmethod
    def _screen_size(screen) -> str:
        try:
            geo = screen.geometry()
            return f"{geo.width()}x{geo.height()}"
        except Exception:  # pragma: no cover
            return ""

    def selected_index(self) -> int:
        idx = self._group.checkedId()
        return idx if idx >= 0 else 0

    def selected_screen(self):
        if not self._screens:
            return None
        return self._screens[self.selected_index()]


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
        # 手書きペンの太さ・色（タブレット編集モードで使用。初期は太さ「中」・色「黒」）。
        self.current_pen_width = DEFAULT_PEN_WIDTH
        self.current_pen_color = DEFAULT_PEN_COLOR
        # 消しゴムでこのドラッグ中に削除があったか（離した時に1回だけ履歴へ積む）。
        self._eraser_changed = False
        # 反映先テンプレート（要件4・5）。選択中テンプレートの target_vouchers を
        # 新規作成オブジェクトへ付与する。初期は「標準」(03/04/05)。
        self._templates: list[dict[str, Any]] = load_templates()
        self.current_target_vouchers: list[str] = list(DEFAULT_TARGET_VOUCHERS)
        self._current_template_name: str = self._templates[0]["name"] if self._templates else "標準"
        self._template_actions: dict[str, Any] = {}
        self.loaded_object_ids: set[str] = set()
        self._handles: list[QGraphicsItem] = []
        # テンプレートバッヂ（編集画面のみ・保存/PDF/Undo対象外）の補助アイテム（要件6）。
        self._badges: list[QGraphicsItem] = []
        # 背景アイテムへの参照を保持する。scene 全走査だけでなくリストでも管理する（要件3）。
        self._background_items: list[QGraphicsItem] = []
        self._tool_actions: dict[str, Any] = {}
        # Undo/Redo 用のスナップショット履歴（要件1・3）。
        self._history: list[list[dict[str, Any]]] = []
        self._history_index: int = -1
        self._object_clipboard: list[dict[str, Any]] = []
        # 履歴復元中フラグ。復元中の commit_history を抑止しRedo履歴を守る（要件1）。
        self._is_restoring_history = False
        self._updating_property_ui = False
        # 未保存変更フラグ（要件3）。閉じる時に確認ダイアログを出すため使う。
        self._dirty = False
        # デバッグ表示設定は他UI互換のため保持するが、背景透過UIは通常機能として常時表示対象。
        self._debug_visible = is_debug_visible()
        # rembg / onnxruntime は import が重いため、起動時には読み込まない。
        # 利用可否はボタン押下時（make_transparent_background_bytes 内）に判定する（要件1・3）。
        # 背景透過は別スレッド(worker)で実行する。実行中は他操作をロックする（要件1・2）。
        self._background_removal_running = False
        self._background_removal_target: "_EditImageItem | None" = None
        # クローズ進行中フラグ。閉じた後に走るスレッド完了コールバックが、破棄済みの
        # UI／QGraphicsItem へアクセスしてアプリ全体を巻き込んで落ちるのを防ぐ（要件5・11）。
        self._closing = False
        # 非同期クローズ進行中フラグ。スレッド終了待ちの間 True。GUIスレッドで wait() せず、
        # スレッドの finished シグナルで改めて close() する（要件3・5・15）。
        self._close_in_progress = False
        self._closing_overlay: "QWidget | None" = None
        # 閉じる時に「終了を待つ必要がある」画像加工スレッド（背景透過 rembg worker 等、
        # 選択画像へ apply_processed_image() する可能性がある処理）を管理する集合。
        # closeEvent はこの集合だけを待機対象にする（要件3・5・9）。ウィンドウを親にせず
        # QThread() で生成し、ここで参照を保持して GC/破棄を防ぐ（要件8）。
        self._blocking_image_threads: "set[QThread]" = set()
        # 保存内容に影響しない補助スレッド（rembg warmup 等）を管理する集合。
        # 閉じる時に待たない・wait() しない・実行中判定に含めない（要件4〜7・12）。
        # 走行中に GC で破棄されないよう、終了まで参照だけ保持する。
        self._warmup_threads: "set[QThread]" = set()
        # 二値化／背景を透過（閾値）で使う RGB しきい値。保存済み値（既定は中）を読む（要件9）。
        self._threshold_rgb: tuple[int, int, int] = load_threshold_rgb()
        self._bg_thread: "QThread | None" = None
        self._bg_worker: "BackgroundRemovalWorker | None" = None
        self._rembg_warmed_up = False
        self._rembg_warmup_running = False
        self._suppress_rembg_warmup = False
        self._rembg_warmup_thread: "QThread | None" = None
        self._rembg_warmup_worker: "RembgWarmupWorker | None" = None
        # ロック対象の編集アクション。_build_toolbar で実体を格納する（要件2）。
        self._edit_actions: list[Any] = []
        # ── タブレット編集モード（SuperDisplay 外部ディスプレイ運用）─────────────
        # tablet_mode 中は通常ペイン/ツールバーを隠し、大きいボタンの専用ツールバーを
        # 表示して全画面化する。編集データ（scene のオブジェクト）は通常モードと共有する。
        self.tablet_mode = False
        self._main_toolbar: "QToolBar | None" = None
        self._tablet_toolbar: "QToolBar | None" = None
        self._tablet_toolbar_container: "QScrollArea | None" = None
        self._tablet_tool_actions: dict[str, Any] = {}
        # 現在の手書きレイヤーID（タブレット編集の書き込み対象: 要件2・3）。
        self._current_layer_id: str | None = None
        self._tablet_layer_panel: "QWidget | None" = None
        self._tablet_layer_buttons: dict[str, QPushButton] = {}
        # タブレット表示先ディスプレイ名（設定に保存。初回は外部ディスプレイへ自動移動）。
        self._tablet_screen_name: str | None = self._load_tablet_screen_name()
        # タブレット終了時に元の表示状態へ戻すための退避値。
        self._pre_tablet_geometry: "QByteArray | None" = None
        self._pre_tablet_maximized = False
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

        # 反映先テンプレートを左側に縦並びで表示する（要件5）。中央＝[左パネル|プレビュー]。
        self._template_panel = self._build_template_panel()
        # タブレット編集モード用の大きい反映先パネル（通常モードでは隠す）。
        self._tablet_reflect_buttons: dict[str, QPushButton] = {}
        self._tablet_reflect_panel = self._build_tablet_reflect_panel()
        # タブレット編集モード用のレイヤーパネル（通常モードでは隠す: 要件3）。
        self._tablet_layer_panel = self._build_tablet_layer_panel()
        # 反映先パネルとレイヤーパネルを縦に積んだ1列の左ペイン（2列にしない: 要件2）。
        self._tablet_left_pane = self._build_tablet_left_pane()
        # 通常ツールバーを先に作る（findChildren(QToolBar)[0] が通常ツールバーになる
        # よう、タブレット用ツールバーより前に構築する）。
        self._build_toolbar()
        # タブレット用ツールバーを作る（横スクロール領域に載せる: 要件1）。
        self._build_tablet_toolbar()
        central = QWidget()
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        # 上段: 2段折り返しのタブレット用ツールバー（初期は非表示）。
        if self._tablet_toolbar_container is not None:
            outer_layout.addWidget(self._tablet_toolbar_container)
        body = QWidget()
        central_layout = QHBoxLayout(body)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        # タブレット用の左ペインは1列（反映先→レイヤーの縦並び）。通常用は従来の反映先
        # パネル。プレビュー（view）に最大の横幅を割り当てる（要件2）。
        central_layout.addWidget(self._tablet_left_pane)
        central_layout.addWidget(self._template_panel)
        central_layout.addWidget(self._view, 1)
        outer_layout.addWidget(body, 1)
        self.setCentralWidget(central)

        self._add_background(background_pdf_bytes)
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
        self._main_toolbar = bar
        self.addToolBar(bar)
        # ツール選択（チェック可能にしてハイライト表示する: 要件11）。
        for label, tool in (("選択", TOOL_SELECT), ("テキスト", TOOL_TEXT),
                            ("線", TOOL_LINE), ("矢印", TOOL_ARROW),
                            ("両矢印", TOOL_DOUBLE_ARROW),
                            ("二重線", TOOL_DOUBLE_LINE),
                            ("四角", TOOL_RECT),
                            ("丸", TOOL_ELLIPSE)):
            act = bar.addAction(label, lambda t=tool: self.set_tool(t))
            act.setCheckable(True)
            self._tool_actions[tool] = act
            self._style_action_widget(bar, act, "editToolButton", as_property=True)
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
        # 反映先テンプレートは左側の縦並びパネルへ表示する（要件5）。ツールバーには置かない。
        # 画像挿入・貼り付け（要件2-3・2-4）。
        insert_action = bar.addAction("画像挿入", self.insert_image_from_file)
        paste_action = bar.addAction("貼り付け", self.paste_image_from_clipboard)
        bar.addSeparator()
        delete_action = bar.addAction("削除", self.delete_selected)
        bar.addSeparator()
        save_action = bar.addAction("保存", self.save)
        # 「座標マーカー」ボタンは通常UIから削除（add_debug_markers は内部・テスト用に残す）。
        save_close_action = bar.addAction("保存して閉じる", self.save_and_close)
        close_action = bar.addAction("閉じる", self.close)
        bar.addSeparator()
        # 背景透過中にロックする編集アクション（保存/保存して閉じる/閉じる/画像挿入/
        # 貼り付け/削除/ツール選択）をまとめて保持する（要件2）。
        self._edit_actions = [
            insert_action, paste_action, delete_action,
            save_action, save_close_action, close_action,
        ] + list(self._tool_actions.values())
        # 全画面/最大化表示の切り替え（要件2-2）。
        self._fullscreen_action = bar.addAction("全画面", self.toggle_fullscreen)
        # タブレット編集モード（表示先ディスプレイを選んでから大きいUIへ切替）。
        self._tablet_action = bar.addAction("タブレット編集",
                                            self.prompt_and_enter_tablet_mode)

        # 削除ボタンは赤い警告色、保存系ボタンは安全色にする（要件2-6・2-7・3）。
        self._style_action_widget(bar, delete_action, "dangerButton")
        self._style_action_widget(bar, save_action, "successButton")
        self._style_action_widget(bar, save_close_action, "successButton")
        # ツールバー全体のボタン幅・余白を広げ、警告色/安全色を割り当てる（要件2-5）。
        bar.setStyleSheet(EDIT_TOOLBAR_STYLE)

        # 選択ツールを初期ハイライト。
        self._update_tool_highlight()

    @staticmethod
    def _style_action_widget(
        bar: QToolBar,
        action,
        style_name: str,
        *,
        as_property: bool = False,
    ) -> None:
        """ツールバーのアクションへ限定スタイル用の名前または property を付ける。"""
        widget = bar.widgetForAction(action)
        if widget is not None:
            if as_property:
                widget.setProperty(style_name, True)
            else:
                widget.setObjectName(style_name)

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
        self._copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self._copy_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._copy_shortcut.activated.connect(self.copy_selected_objects)
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
        if self._background_removal_running:
            return
        self.current_tool = tool
        # 選択モードはドラッグ選択、掴むモードは手のひらでパン、それ以外は描画優先。
        if tool == TOOL_SELECT:
            self._view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        elif tool == TOOL_GRAB:
            # ScrollHandDrag: ドラッグでプレビューをスクロール／パンする（手のひらカーソル）。
            self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        else:
            self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._update_tool_highlight()
        # ツール切替でも背景は消さない（要件1・4）。
        self.ensure_background_visible()

    def _update_tool_highlight(self) -> None:
        """選択中ツールのボタンだけをハイライトする（要件11）。

        通常ツールバーとタブレット用ツールバーの両方を同期する。
        """
        for actions in (self._tool_actions, getattr(self, "_tablet_tool_actions", {})):
            for tool, act in actions.items():
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

    # ── タブレット編集モード ──────────────────────────────────────────────────
    @staticmethod
    def _load_tablet_screen_name() -> str | None:
        """設定からタブレット表示先ディスプレイ名を読む。失敗しても None で続行。"""
        try:
            from app.voucher_settings import load_tablet_screen_name

            return load_tablet_screen_name()
        except Exception:  # pragma: no cover - 設定読込失敗は致命的でない。
            return None

    def _build_tablet_toolbar(self) -> None:
        """手書きペン中心の大きいボタンを並べた専用ツールバーを作る（初期は非表示）。

        タブレット編集では「手書きで指示を書く」ことを最優先にする。テキスト・線・
        図形・画像など細かいツールは並べず、ペン／消しゴム中心の簡単なUIにする。
        通常モードと同じ scene/編集データを操作する（別保存形式・別データは作らない）。

        縦領域を節約してプレビューを広く使うため、上部メニューは**1段**で構成する
        （要件1）。全ボタンを1本の QToolBar に左から並べ、右端に保存／タブレット終了
        を置く。よく使う解像度では1段に収まるが、横幅が足りない場合の保険として
        QScrollArea に載せて横スクロールできるようにする（2段表示には戻さない: 要件3）。
        """
        bar = QToolBar("タブレット編集ツール")
        bar.setObjectName("tabletToolBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        # 主機能: 手書き(ペン)・掴む(パン)・消しゴム・選択（チェック可能。ハイライト対象）。
        for label, tool in (("手書き", TOOL_PEN), ("掴む", TOOL_GRAB),
                            ("消しゴム", TOOL_ERASER), ("選択", TOOL_SELECT)):
            act = bar.addAction(label, lambda t=tool: self.set_tool(t))
            act.setCheckable(True)
            self._tablet_tool_actions[tool] = act
        bar.addSeparator()
        # 太さ・色（押すたびに 細→中→太 / 黒→赤→青 と切り替わる）。
        self._pen_width_action = bar.addAction(self.pen_width_label(),
                                               self.cycle_pen_width)
        self._pen_color_action = bar.addAction(self.pen_color_label(),
                                               self.cycle_pen_color)
        bar.addSeparator()
        # Undo / Redo（1ストローク=1操作）。
        bar.addAction("戻す", self.undo)
        bar.addAction("やり直し", self.redo)
        bar.addSeparator()
        # 削除（選択中オブジェクト）・全消去（手書きすべて）。
        delete_act = bar.addAction("削除", self.delete_selected)
        clear_act = bar.addAction("全消去", self.clear_freehand_all)
        bar.addSeparator()
        # 表示操作（拡大・縮小・全体表示）。
        bar.addAction("拡大", self.zoom_in)
        bar.addAction("縮小", self.zoom_out)
        bar.addAction("全体表示", self.fit_page_to_view)
        bar.addSeparator()
        # 右端に保存／タブレット終了（常に同じ位置で見えるようにする）。
        save_act = bar.addAction("保存", self.save)
        exit_act = bar.addAction("タブレット終了", self.exit_tablet_mode)

        # 保存=安全色・削除/全消去=警告色・終了=注意色を割り当てる。
        self._style_tablet_button(bar, save_act, "tabletSaveButton")
        self._style_tablet_button(bar, delete_act, "tabletDeleteButton")
        self._style_tablet_button(bar, clear_act, "tabletDeleteButton")
        self._style_tablet_button(bar, exit_act, "tabletExitButton")
        bar.setStyleSheet(TABLET_TOOLBAR_STYLE)

        self._tablet_toolbar = bar
        # 横幅が足りない場合の保険として横スクロール可能な領域に載せる（見た目は1段:
        # 要件3）。縦スクロールは出さず、縦領域を節約する。
        container = QScrollArea()
        container.setObjectName("tabletToolBarContainer")
        container.setWidgetResizable(True)
        container.setWidget(bar)
        container.setFrameShape(QFrame.Shape.NoFrame)
        container.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        container.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 1段ぶんの高さに固定（ボタン高さ44〜48px + 余白）。縦領域を節約する。
        container.setFixedHeight(64)
        container.setStyleSheet(
            "QScrollArea#tabletToolBarContainer { background-color: #23282d; }")
        # 通常モードでは隠しておく。
        container.hide()
        self._tablet_toolbar_container = container

    @staticmethod
    def _style_tablet_button(bar: QToolBar, action, object_name: str) -> None:
        widget = bar.widgetForAction(action)
        if widget is not None:
            widget.setObjectName(object_name)

    def _find_tablet_screen(self) -> "object | None":
        """タブレット表示に使う外部ディスプレイ（primary 以外）を返す。

        - primary 以外の screen を候補にする。
        - 前回選択した画面（_tablet_screen_name）があれば優先する。
        - なければ primary 以外の最初の画面を使う。
        - 外部ディスプレイが無ければ None。
        """
        screens = list(QGuiApplication.screens())
        primary = QGuiApplication.primaryScreen()
        candidates = [s for s in screens if s is not primary]
        if not candidates:
            return None
        if self._tablet_screen_name:
            for s in candidates:
                if s.name() == self._tablet_screen_name:
                    return s
        return candidates[0]

    def _move_to_screen(self, screen) -> None:
        """指定ディスプレイへウィンドウを移動し、その画面で全画面表示する。"""
        if screen is None:
            return
        geo = screen.availableGeometry()
        # 一旦通常表示に戻してから移動しないと、全画面のまま移動できない環境がある。
        self.showNormal()
        self.move(geo.topLeft())
        self.resize(geo.width(), geo.height())
        # 選択した画面を設定へ記憶する（次回優先）。
        try:
            name = screen.name()
            if name and name != self._tablet_screen_name:
                self._tablet_screen_name = name
                from app.voucher_settings import save_tablet_screen_name

                save_tablet_screen_name(name)
        except Exception:  # pragma: no cover - 保存失敗は致命的でない。
            pass

    def _notify_no_external_display(self) -> None:
        """外部ディスプレイが無いことを非モーダルで通知する（処理は止めない）。"""
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("タブレット編集")
            box.setText(
                "外部ディスプレイが見つからないため、現在の画面でタブレット編集モードを開始します。"
            )
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.setModal(False)
            box.show()
        except Exception:  # pragma: no cover - 通知は補助的。失敗しても続行。
            pass

    def _show_tablet_toolbar(self) -> None:
        if self._tablet_toolbar_container is not None:
            self._tablet_toolbar_container.show()
        if self._tablet_toolbar is not None:
            self._tablet_toolbar.show()

    def _hide_tablet_toolbar(self) -> None:
        if self._tablet_toolbar_container is not None:
            self._tablet_toolbar_container.hide()
        if self._tablet_toolbar is not None:
            self._tablet_toolbar.hide()

    def _fit_canvas_to_screen(self) -> None:
        """伝票キャンバスを画面いっぱい（余白最小）に表示する。

        上部メニューを1段化したことで増えた縦領域をプレビューへ回す（要件4）。
        fitInView はビューポート（プレビュー領域）が広いほど大きく収めるので、
        ツールバーが薄い分だけ伝票が自動的に大きく表示される。レイアウト確定後の
        サイズで確実にフィットさせるため、イベント処理後にもう一度フィットする。
        """
        # 余白を最小化しつつページ全体を収める。既存のフィット処理を流用する。
        self.fit_page_to_view()
        # ツールバー表示・全画面化のレイアウトが確定してから再フィットし、広がった
        # プレビュー領域いっぱいに伝票を大きく表示する。
        QTimer.singleShot(0, self.fit_page_to_view)

    def zoom_in(self) -> None:
        """編集ビューを拡大する（タブレット/通常共通）。"""
        view = getattr(self, "_view", None)
        if view is not None:
            view.scale(1.25, 1.25)

    def zoom_out(self) -> None:
        """編集ビューを縮小する（タブレット/通常共通）。"""
        view = getattr(self, "_view", None)
        if view is not None:
            view.scale(0.8, 0.8)

    def enter_tablet_mode(self, screen: "object | None" = None,
                          find_screen: bool = True) -> None:
        """タブレット編集モードを開始する。指定ディスプレイ（無ければ自動検出）へ移動する。

        screen を指定した場合はその画面へ移動する（表示先選択ダイアログの結果）。
        find_screen=False かつ screen=None のときは現在の画面のまま開始する
        （ダイアログで「現在の画面で開始」を選んだ場合: 要件7）。
        """
        if self.tablet_mode:
            return
        self.tablet_mode = True
        # 元の表示状態を退避（終了時に元のPC側へ戻すため）。
        self._pre_tablet_maximized = self.isMaximized()
        try:
            self._pre_tablet_geometry = self.saveGeometry()
        except Exception:  # pragma: no cover
            self._pre_tablet_geometry = None
        # 1〜3. 表示先ディスプレイへ移動・全画面化。
        if screen is None and find_screen:
            screen = self._find_tablet_screen()
        if screen is not None:
            self._move_to_screen(screen)
        elif find_screen:
            self._notify_no_external_display()
        # 4. 通常の細かいUIを隠し、タブレット用ツールバーへ切替（反映先選択は内部状態で維持）。
        if self._main_toolbar is not None:
            self._main_toolbar.hide()
        if getattr(self, "_template_panel", None) is not None:
            self._template_panel.hide()
        # 反映先はタブレット用の大きいパネルで選べるようにする（同じ内部状態を共有: タスク2）。
        # 反映先パネル→レイヤーパネルを縦に積んだ1列の左ペインを表示する（要件2・3）。
        if getattr(self, "_tablet_reflect_panel", None) is not None:
            self._tablet_reflect_panel.setVisible(True)
        if getattr(self, "_tablet_layer_panel", None) is not None:
            self._tablet_layer_panel.setVisible(True)
        if getattr(self, "_tablet_left_pane", None) is not None:
            self._tablet_left_pane.setVisible(True)
        # 手書きレイヤーを1つは用意し、現在レイヤーとして選択する（要件2・3）。
        self._ensure_current_layer()
        self._refresh_layer_panel()
        # 通常モードでの選択状態をタブレットパネルへ引き継ぐ。
        self._update_template_highlight()
        self._show_tablet_toolbar()
        # 3/4. その画面で全画面表示する。
        self.showFullScreen()
        # タブレット編集はペン中心。開始直後すぐ手書きできるよう初期ツールを「ペン」にする。
        self.set_tool(TOOL_PEN)
        self._update_tool_highlight()
        # 5. 伝票キャンバスを画面いっぱいに表示する。
        self._fit_canvas_to_screen()

    def exit_tablet_mode(self) -> None:
        """タブレット編集モードを終了し、通常の指図書編集UIへ戻す。"""
        if not self.tablet_mode:
            return
        self.tablet_mode = False
        # 1. 全画面解除。
        self.showNormal()
        # 2. 通常UIを復帰、タブレット用ツールバー／反映先パネルを隠す。
        self._hide_tablet_toolbar()
        if getattr(self, "_tablet_left_pane", None) is not None:
            self._tablet_left_pane.setVisible(False)
        if getattr(self, "_tablet_reflect_panel", None) is not None:
            self._tablet_reflect_panel.setVisible(False)
        if getattr(self, "_tablet_layer_panel", None) is not None:
            self._tablet_layer_panel.setVisible(False)
        if self._main_toolbar is not None:
            self._main_toolbar.show()
        if getattr(self, "_template_panel", None) is not None:
            self._template_panel.show()
        # タブレットで変更した反映先選択を通常パネルへも反映する（同じ内部状態）。
        self._update_template_highlight()
        # 3. 画面位置を元のPC側へ戻す（退避値があれば復元）。
        restored = False
        if self._pre_tablet_geometry is not None:
            try:
                restored = self.restoreGeometry(self._pre_tablet_geometry)
            except Exception:  # pragma: no cover
                restored = False
        if not restored:
            if self._pre_tablet_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        # 通常モードへ戻ったら既定の「選択」ツールにする（手書きペンのまま残さない）。
        self.set_tool(TOOL_SELECT)
        self._update_tool_highlight()
        # 4. 編集データは保持したままページ全体を再フィット。
        self.fit_page_to_view()

    def prompt_and_enter_tablet_mode(self) -> None:
        """表示先ディスプレイを選んでからタブレット編集モードを開始する（要件7）。

        キャンセル時はタブレット編集モードに入らない。
        """
        if self.tablet_mode:
            return
        ok, screen, find = self._select_tablet_screen()
        if not ok:
            return
        self.enter_tablet_mode(screen=screen, find_screen=find)

    def _select_tablet_screen(self) -> "tuple[bool, object | None, bool]":
        """表示先ディスプレイを選ぶ。戻り値は (開始するか, 画面, find_screen)。

        - 画面が複数: 選択ダイアログを表示。キャンセルで (False, None, False)。
        - 画面が1つだけ: 現在画面で開始するか確認する（要件7）。
        """
        try:
            screens = list(QGuiApplication.screens())
        except Exception:  # pragma: no cover
            screens = []
        if len(screens) <= 1:
            # 外部ディスプレイが無い場合は確認メッセージを表示する。
            reply = QMessageBox.question(
                self, "タブレット編集",
                "外部ディスプレイが見つかりません。\n現在の画面でタブレット編集モードを開始しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                return (True, None, False)
            return (False, None, False)
        dialog = TabletScreenDialog(screens, saved_name=self._tablet_screen_name,
                                    parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return (False, None, False)
        screen = dialog.selected_screen()
        return (True, screen, False)

    def toggle_tablet_mode(self) -> None:
        if self.tablet_mode:
            self.exit_tablet_mode()
        else:
            self.enter_tablet_mode()

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
        self._remove_badges()
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

    def _resolve_target_vouchers(self, target_vouchers: list[str] | None) -> list[str]:
        """指定が無ければ選択中テンプレートの反映先を使う（要件5）。"""
        if target_vouchers is None:
            return list(self.current_target_vouchers)
        return _normalize_target_vouchers(target_vouchers)

    # ── 反映先テンプレート（要件5）──────────────────────────────────────────────
    def _template_for_targets(self, targets: list[str]) -> dict[str, Any] | None:
        """target_vouchers の組み合わせに一致するテンプレートを返す（バッヂ表示用）。"""
        key = frozenset(str(v) for v in targets)
        for tpl in self._templates:
            if frozenset(str(v) for v in tpl["target_vouchers"]) == key:
                return tpl
        return None

    def _on_template_selected(self, template: dict[str, Any]) -> None:
        """テンプレートボタン押下。以後の新規作成へ反映する。

        タブレット編集モードでは現在選択中レイヤーの反映先も更新する（要件4）。
        """
        if template is None:
            return
        self.current_target_vouchers = list(template["target_vouchers"])
        self._current_template_name = template["name"]
        if self.tablet_mode:
            layer = self.current_freehand_layer()
            if layer is not None:
                layer.target_vouchers = list(template["target_vouchers"])
                self.mark_dirty()
                self.commit_history()
        self._update_template_highlight()
        self.ensure_background_visible()

    def _build_template_panel(self) -> QWidget:
        """反映先テンプレートを左側に縦並びで表示するパネルを作る（要件5）。"""
        panel = QWidget()
        panel.setObjectName("templatePanel")
        panel.setFixedWidth(150)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)
        heading = QLabel("反映先")
        hf = heading.font()
        hf.setBold(True)
        heading.setFont(hf)
        layout.addWidget(heading)
        self._template_panel_layout = layout
        self._template_actions = {}
        self._template_button_group = QButtonGroup(panel)
        self._template_button_group.setExclusive(True)
        for tpl in self._templates:
            self._add_template_action(tpl)
        # 任意のテンプレートを登録する（要件4）。
        self._register_template_button = QPushButton("＋ テンプレ登録")
        self._register_template_button.clicked.connect(self._on_register_template)
        layout.addWidget(self._register_template_button)
        # 画像選択中（かつデバッグ表示ON）だけ表示する画像編集ボタン（要件1・2）。
        self._image_actions_label = QLabel("画像処理")
        imgf = self._image_actions_label.font()
        imgf.setBold(True)
        self._image_actions_label.setFont(imgf)
        self._image_actions_label.setStyleSheet("margin-top: 8px;")
        self._image_actions_label.setVisible(False)
        layout.addWidget(self._image_actions_label)
        # 表示順は要件4に従う: 背景を透過（rembg）→二値化→背景を透過（閾値）
        # →背景を戻す→閾値設定。すべて画像選択中のみ表示する。
        self._transparent_bg_button = QPushButton(TRANSPARENT_REMBG_LABEL)
        self._transparent_bg_button.setToolTip("選択中の画像の背景を rembg で透過します")
        self._transparent_bg_button.clicked.connect(self._on_transparent_background)
        self._transparent_bg_button.setVisible(False)
        layout.addWidget(self._transparent_bg_button)
        self._binarize_button = QPushButton("二値化")
        self._binarize_button.setToolTip("選択中の画像を閾値で白黒に二値化します")
        self._binarize_button.clicked.connect(self._on_binarize)
        self._binarize_button.setVisible(False)
        layout.addWidget(self._binarize_button)
        self._threshold_transparent_button = QPushButton(TRANSPARENT_THRESHOLD_LABEL)
        self._threshold_transparent_button.setToolTip(
            "設定した閾値以上の白背景ピクセルを透明化します"
        )
        self._threshold_transparent_button.clicked.connect(self._on_threshold_transparent)
        self._threshold_transparent_button.setVisible(False)
        layout.addWidget(self._threshold_transparent_button)
        self._restore_image_button = QPushButton("背景を戻す")
        self._restore_image_button.setToolTip("加工前の画像へ戻します")
        self._restore_image_button.clicked.connect(self._on_restore_image)
        self._restore_image_button.setVisible(False)
        layout.addWidget(self._restore_image_button)
        self._threshold_settings_button = QPushButton("閾値設定")
        self._threshold_settings_button.setToolTip("二値化／閾値透過で使う RGB 閾値を設定します")
        self._threshold_settings_button.clicked.connect(self._on_threshold_settings)
        self._threshold_settings_button.setVisible(False)
        layout.addWidget(self._threshold_settings_button)
        layout.addStretch(1)
        self._update_template_highlight()
        return panel

    # ── タブレット編集モード用の大きい反映先パネル（タスク2）────────────────────
    def _build_tablet_left_pane(self) -> QScrollArea:
        """反映先パネルとレイヤーパネルを縦に積んだ1列の左ペインを作る（要件2）。

        - 2列にせず、「反映先」の下に「レイヤー」を縦並びで表示する。
        - 反映先とレイヤーの間に少し余白を入れる。
        - 左ペインが縦に長い場合に備え、縦スクロール可能な領域に載せる。
        - プレビュー（view）に横幅を多く割り当てるため、固定幅は控えめにする。
        """
        # 各パネルは内部の addStretch で伸びないよう縦は最大サイズ固定にし、余白は
        # ペイン側の stretch で吸収する（反映先とレイヤーが間延びしないように）。
        for panel in (self._tablet_reflect_panel, self._tablet_layer_panel):
            panel.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Maximum)

        content = QWidget()
        content.setObjectName("tabletLeftPaneContent")
        vbox = QVBoxLayout(content)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(18)  # 反映先とレイヤーの間に少し余白を入れる。
        vbox.addWidget(self._tablet_reflect_panel)
        vbox.addWidget(self._tablet_layer_panel)
        vbox.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("tabletLeftPaneScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 1列固定。横スクロールは出さず、縦に長い場合だけ縦スクロールする。
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFixedWidth(210)
        # 通常モードでは隠しておく（タブレット編集モードでのみ表示）。
        scroll.hide()
        return scroll

    def _build_tablet_reflect_panel(self) -> QWidget:
        """タブレットでも押しやすい大きいボタンで反映先を選ぶパネル（初期は非表示）。

        通常モードの反映先選択と同じ内部状態（current_target_vouchers /
        _current_template_name）を共有する。専用の反映先状態は持たない。
        """
        panel = QWidget()
        panel.setObjectName("tabletReflectPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        heading = QLabel("反映先")
        hf = heading.font()
        hf.setBold(True)
        hf.setPointSize(max(hf.pointSize() + 3, 14))
        heading.setFont(hf)
        layout.addWidget(heading)
        self._tablet_reflect_layout = layout
        for tpl in self._templates:
            self._add_tablet_reflect_button(tpl)
        layout.addStretch(1)
        panel.setVisible(False)
        return panel

    def _add_tablet_reflect_button(self, tpl: dict[str, Any]) -> None:
        """タブレット反映先パネルへ大きいボタンを1つ追加する。"""
        name = tpl["name"]
        locked = is_locked_template(name)
        btn = QPushButton(("🔒 " + name) if locked else name)
        btn.setProperty("reflectTargetButton", True)
        btn.setProperty("reflectTargetSelected", False)
        btn.setCheckable(True)
        # タッチ操作向けに大きく（高さ52px以上・文字大きめ）。
        btn.setMinimumHeight(52)
        f = btn.font()
        f.setPointSize(max(f.pointSize() + 3, 14))
        btn.setFont(f)
        self._apply_reflect_target_button_style(btn, False)
        btn.setToolTip("反映先伝票: " + ", ".join(tpl["target_vouchers"]))
        btn.clicked.connect(
            lambda checked=False, n=name: self._on_template_selected(self._template_by_name(n))
        )
        layout = self._tablet_reflect_layout
        # 末尾の stretch がある場合はその直前へ挿入する。
        insert_at = layout.count()
        last = layout.itemAt(insert_at - 1) if insert_at > 0 else None
        if last is not None and last.spacerItem() is not None:
            layout.insertWidget(insert_at - 1, btn)
        else:
            layout.addWidget(btn)
        self._tablet_reflect_buttons[name] = btn

    def _reload_tablet_reflect_panel(self) -> None:
        """テンプレート変更後にタブレット反映先パネルのボタンを作り直す。"""
        for btn in list(self._tablet_reflect_buttons.values()):
            self._tablet_reflect_layout.removeWidget(btn)
            btn.deleteLater()
        self._tablet_reflect_buttons = {}
        for tpl in self._templates:
            self._add_tablet_reflect_button(tpl)
        self._update_template_highlight()

    # ── タブレット編集モード用のレイヤーパネル（要件3）──────────────────────────
    def _build_tablet_layer_panel(self) -> QWidget:
        """手書きレイヤーの一覧・追加・削除を行うパネル（初期は非表示）。"""
        panel = QWidget()
        panel.setObjectName("tabletLayerPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        heading = QLabel("レイヤー")
        hf = heading.font()
        hf.setBold(True)
        hf.setPointSize(max(hf.pointSize() + 3, 14))
        heading.setFont(hf)
        layout.addWidget(heading)
        # レイヤー一覧（縦並びボタン）。
        self._tablet_layer_list_layout = QVBoxLayout()
        self._tablet_layer_list_layout.setSpacing(5)
        layout.addLayout(self._tablet_layer_list_layout)
        layout.addStretch(1)
        # 追加・削除・名前変更・表示切替ボタン。
        add_btn = QPushButton("＋ レイヤー追加")
        add_btn.setMinimumHeight(44)
        add_btn.clicked.connect(self._on_add_layer_clicked)
        layout.addWidget(add_btn)
        del_btn = QPushButton("レイヤー削除")
        del_btn.setMinimumHeight(44)
        del_btn.clicked.connect(self._on_delete_layer_clicked)
        layout.addWidget(del_btn)
        rename_btn = QPushButton("名前変更")
        rename_btn.setMinimumHeight(40)
        rename_btn.clicked.connect(self._on_rename_layer_clicked)
        layout.addWidget(rename_btn)
        self._tablet_layer_visible_btn = QPushButton("表示／非表示")
        self._tablet_layer_visible_btn.setMinimumHeight(40)
        self._tablet_layer_visible_btn.clicked.connect(self._on_toggle_layer_visible_clicked)
        layout.addWidget(self._tablet_layer_visible_btn)
        panel.setVisible(False)
        return panel

    def _refresh_layer_panel(self) -> None:
        """レイヤー一覧ボタンを現在の手書きレイヤーから作り直す（要件3）。"""
        layout = getattr(self, "_tablet_layer_list_layout", None)
        if layout is None:
            return
        for btn in list(self._tablet_layer_buttons.values()):
            layout.removeWidget(btn)
            btn.deleteLater()
        self._tablet_layer_buttons = {}
        for layer in self.freehand_layers():
            name = layer.layer_name
            if not layer.isVisible():
                name = name + "（非表示）"
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.clicked.connect(
                lambda checked=False, lid=layer.layer_id: self.select_freehand_layer(lid)
            )
            layout.addWidget(btn)
            self._tablet_layer_buttons[layer.layer_id] = btn
        self._update_layer_highlight()

    def _update_layer_highlight(self) -> None:
        """選択中レイヤーを青背景・白文字・太字でハイライトする（要件3）。"""
        for lid, btn in self._tablet_layer_buttons.items():
            checked = lid == self._current_layer_id
            btn.setChecked(checked)
            if checked:
                btn.setStyleSheet(REFLECT_TARGET_SELECTED_STYLE)
            elif current_title_bar_is_dark():
                btn.setStyleSheet(REFLECT_TARGET_DARK_STYLE)
            else:
                btn.setStyleSheet(REFLECT_TARGET_LIGHT_STYLE)
            btn.update()

    # ── 手書きレイヤー管理（要件2・3・4）──────────────────────────────────────
    def freehand_layers(self) -> list["_EditFreehandLayerItem"]:
        """編集レイヤー中の手書きレイヤー一覧を返す（作成順に近い id 安定順）。"""
        layers = [it for it in self.edit_items()
                  if isinstance(it, _EditFreehandLayerItem)]
        layers.sort(key=lambda it: str(it.obj_id))
        return layers

    def current_freehand_layer(self) -> "_EditFreehandLayerItem | None":
        """現在選択中の手書きレイヤーを返す。無ければ None。"""
        if self._current_layer_id is None:
            return None
        for layer in self.freehand_layers():
            if layer.layer_id == self._current_layer_id:
                return layer
        return None

    def _next_layer_name(self) -> str:
        """レイヤー1、レイヤー2... の連番で未使用の名前を返す。"""
        existing = {layer.layer_name for layer in self.freehand_layers()}
        i = 1
        while f"レイヤー{i}" in existing:
            i += 1
        return f"レイヤー{i}"

    def add_freehand_layer(self, layer_name: str | None = None,
                           target_vouchers: list[str] | None = None,
                           pen_width: float | None = None,
                           stroke_color: str | None = None,
                           layer_id: str | None = None,
                           visible: bool = True, locked: bool = False,
                           strokes: list[dict[str, Any]] | None = None,
                           select: bool = True,
                           register_id: bool = True) -> "_EditFreehandLayerItem":
        """新しい手書きレイヤーを作成して編集レイヤーへ追加する（要件3）。"""
        name = layer_name or self._next_layer_name()
        tv = self._resolve_target_vouchers(target_vouchers)
        pw = self.current_pen_width if pen_width is None else pen_width
        sc = self.current_pen_color if stroke_color is None else stroke_color
        item = _EditFreehandLayerItem(
            layer_id=layer_id, layer_name=name, target_vouchers=tv,
            pen_width=pw, stroke_color=sc, visible=visible, locked=locked,
            strokes=strokes)
        if register_id:
            self._register(item)
        else:
            self._scene.addItem(item)
        if select:
            self._current_layer_id = item.layer_id
            self._sync_tools_from_layer(item)
        self._refresh_layer_panel()
        return item

    def _ensure_current_layer(self) -> "_EditFreehandLayerItem":
        """現在の手書きレイヤーを返す。1つも無ければ作成する（要件2・3）。"""
        layer = self.current_freehand_layer()
        if layer is not None:
            return layer
        layers = self.freehand_layers()
        if layers:
            layer = layers[0]
            self._current_layer_id = layer.layer_id
            self._sync_tools_from_layer(layer)
            self._refresh_layer_panel()
            return layer
        # 1つも無ければ既定レイヤーを作る。
        layer = self.add_freehand_layer()
        self.mark_dirty()
        self.commit_history()
        return layer

    def select_freehand_layer(self, layer_id: str) -> None:
        """指定IDのレイヤーを現在の手書き対象にする（要件3・4）。"""
        for layer in self.freehand_layers():
            if layer.layer_id == layer_id:
                self._current_layer_id = layer_id
                self._sync_tools_from_layer(layer)
                self._update_layer_highlight()
                return

    def _sync_tools_from_layer(self, layer: "_EditFreehandLayerItem") -> None:
        """レイヤーの反映先・太さ・色をツールバーへ反映する（要件4）。"""
        self.current_pen_width = float(layer.pen_width)
        self.current_pen_color = _color_name(layer.stroke_color)
        self.current_target_vouchers = list(layer.target_vouchers)
        template = self._template_for_targets(layer.target_vouchers)
        self._current_template_name = template["name"] if template else ""
        act = getattr(self, "_pen_width_action", None)
        if act is not None:
            act.setText(self.pen_width_label())
        act = getattr(self, "_pen_color_action", None)
        if act is not None:
            act.setText(self.pen_color_label())
        self._update_template_highlight()

    def delete_freehand_layer(self, layer_id: str) -> bool:
        """指定IDのレイヤーを削除する（要件3）。"""
        removed = False
        for layer in self.freehand_layers():
            if layer.layer_id == layer_id:
                if layer.scene() is not None:
                    self._scene.removeItem(layer)
                self.loaded_object_ids.discard(layer.obj_id)
                removed = True
                break
        if removed:
            if self._current_layer_id == layer_id:
                self._current_layer_id = None
                remaining = self.freehand_layers()
                if remaining:
                    self.select_freehand_layer(remaining[0].layer_id)
            self.mark_dirty()
            self.commit_history()
            self._refresh_layer_panel()
        return removed

    def add_stroke_to_current_layer(self, points: list,
                                    pen_width: float | None = None,
                                    stroke_color: str | None = None) -> bool:
        """現在の手書きレイヤーへ1ストロークを追加する（要件2）。"""
        if len(points) < 2:
            return False
        layer = self._ensure_current_layer()
        pw = self.current_pen_width if pen_width is None else pen_width
        sc = self.current_pen_color if stroke_color is None else stroke_color
        layer.add_stroke(points, pen_width=pw, stroke_color=sc)
        self.mark_dirty()
        self.commit_history()
        self.ensure_background_visible()
        return True

    def _on_add_layer_clicked(self) -> None:
        self.add_freehand_layer()
        self.mark_dirty()
        self.commit_history()

    def _on_delete_layer_clicked(self) -> None:
        if self._current_layer_id is None:
            return
        self.delete_freehand_layer(self._current_layer_id)

    def _on_rename_layer_clicked(self) -> None:
        layer = self.current_freehand_layer()
        if layer is None:
            return
        name, ok = QInputDialog.getText(self, "レイヤー名変更", "レイヤー名:",
                                        text=layer.layer_name)
        if ok and name.strip():
            layer.layer_name = name.strip()
            self.mark_dirty()
            self.commit_history()
            self._refresh_layer_panel()

    def _on_toggle_layer_visible_clicked(self) -> None:
        layer = self.current_freehand_layer()
        if layer is None:
            return
        layer.setVisible(not layer.isVisible())
        self.mark_dirty()
        self.commit_history()
        self._refresh_layer_panel()

    # ── 画像編集ボタン（背景透過/背景を戻す: 要件1〜5）──────────────────────────
    def _selected_image_item(self) -> "_EditImageItem | None":
        """単一選択中の画像オブジェクトを返す。該当なしなら None。"""
        try:
            selected = self._scene.selectedItems()
        except RuntimeError:
            return None
        edit_selected = [it for it in selected if hasattr(it, "serialize_edit_object")]
        if len(edit_selected) != 1:
            return None
        images = [it for it in selected if isinstance(it, _EditImageItem)]
        if len(images) == 1:
            return images[0]
        return None

    def _update_image_action_buttons(self) -> None:
        """画像編集ボタンの表示/有効状態を更新する（要件1〜5・12）。"""
        transparent_btn = getattr(self, "_transparent_bg_button", None)
        binarize_btn = getattr(self, "_binarize_button", None)
        threshold_btn = getattr(self, "_threshold_transparent_button", None)
        restore_btn = getattr(self, "_restore_image_button", None)
        settings_btn = getattr(self, "_threshold_settings_button", None)
        label = getattr(self, "_image_actions_label", None)
        if (transparent_btn is None or restore_btn is None or label is None
                or binarize_btn is None or threshold_btn is None
                or settings_btn is None):
            return
        image = self._selected_image_item()
        # 画像単一選択中だけ表示する。デバッグ表示ON/OFFには依存しない（要件3・12）。
        show = image is not None
        for widget in (label, transparent_btn, binarize_btn, threshold_btn,
                       restore_btn, settings_btn):
            widget.setVisible(show)
        if show:
            # 加工処理中（rembg）は加工系ボタンを無効化（二重実行・状態破壊を防ぐ: 要件2）。
            running = bool(self._background_removal_running)
            # 背景透過は画像選択中なら有効。rembg の可否はボタン押下時に判定する（要件2・3）。
            transparent_btn.setEnabled(not running)
            transparent_btn.setToolTip("選択中の画像の背景を rembg で透過します")
            binarize_btn.setEnabled(not running)
            threshold_btn.setEnabled(not running)
            # 閾値設定は加工処理に依存しないため常に有効（要件8）。
            settings_btn.setEnabled(True)
            # 背景を戻すは元画像がある時のみ（要件5・10）。
            restore_btn.setEnabled(not running and image.has_original_image())
            if not self._suppress_rembg_warmup:
                self._start_rembg_warmup_if_needed()

    def _register_blocking_image_thread(self, thread: "QThread") -> None:
        """閉じる時に終了を待つ必要がある画像加工スレッドを登録する（要件3・5・8）。

        ウィンドウを親にしない QThread() を保持してGC・誤破棄を防ぎ、finished で集合から
        外す。非同期クローズ中なら、全終了を機に最終クローズへ進める（要件5）。
        """
        self._blocking_image_threads.add(thread)

        def _cleanup() -> None:
            self._blocking_image_threads.discard(thread)
            self._on_image_thread_finished()

        thread.finished.connect(_cleanup)

    def _register_warmup_thread(self, thread: "QThread") -> None:
        """保存内容に影響しない補助スレッド（warmup 等）を登録する（要件7）。

        閉じる時の待機対象には含めない。走行中に GC されないよう終了まで参照を保持し、
        finished で集合から外して deleteLater で C++ オブジェクトを片付ける。
        非同期クローズの進行（_on_image_thread_finished）はトリガしない（要件4・6）。
        """
        self._warmup_threads.add(thread)

        def _cleanup() -> None:
            self._warmup_threads.discard(thread)

        thread.finished.connect(_cleanup)
        thread.finished.connect(thread.deleteLater)

    def _start_rembg_warmup_if_needed(self) -> None:
        """画像選択後に rembg / pymatting の軽い初期化を非同期で開始する。

        これは事前読み込みにすぎず保存内容には影響しないため、閉じる時の待機対象には
        含めない（non-blocking warmup thread として扱う: 要件4〜7）。
        """
        if self._rembg_warmed_up or self._rembg_warmup_running:
            return
        # クローズ中は新しいスレッドを起こさない（要件3・5）。
        if self._closing or self._close_in_progress:
            return
        self._rembg_warmup_running = True
        # ウィンドウを親にしない。WA_DeleteOnClose でウィンドウが破棄されても、
        # 走行中スレッドが道連れに破棄されて落ちるのを防ぐ（要件6）。
        thread = QThread()
        worker = RembgWarmupWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_rembg_warmup_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        # warmup は閉じる時の待機対象にしない（要件4・7）。
        self._register_warmup_thread(thread)
        self._rembg_warmup_thread = thread
        self._rembg_warmup_worker = worker
        thread.start()

    @Slot()
    def _on_rembg_warmup_finished(self) -> None:
        self._rembg_warmup_running = False
        self._rembg_warmup_worker = None
        self._rembg_warmup_thread = None
        # クローズ処理中はUIへ触らない（要件5・11）。
        if self._closing or self._close_in_progress:
            return
        self._rembg_warmed_up = True

    def set_debug_visible(self, visible: bool) -> None:
        """デバッグ表示状態を切り替え、画像編集ボタンの表示を更新する（テスト/設定変更用）。"""
        self._debug_visible = bool(visible)
        self._update_image_action_buttons()

    def _set_edit_controls_enabled(self, enabled: bool) -> None:
        """背景透過中の編集操作ロック（要件2）。

        保存/保存して閉じる/閉じる/画像挿入/貼り付け/削除/ツール選択の各アクションと、
        キャンバス操作（QGraphicsView の操作）を一括で有効/無効にする。透過中フラグ
        (_background_removal_running) と併用し、ショートカット経由の操作も無視させる。
        """
        for action in getattr(self, "_edit_actions", []):
            try:
                action.setEnabled(enabled)
            except RuntimeError:
                # 既に破棄されたアクションは無視する。
                continue
        # キャンバス操作（選択・移動・サイズ変更）をロックする。描画は維持するため
        # setEnabled ではなく setInteractive を使い、白画面化を避ける（要件2・3）。
        view = getattr(self, "_view", None)
        if view is not None:
            view.setInteractive(enabled)

    def _on_transparent_background(self) -> None:
        """選択中画像の背景を rembg で透過する（要件1〜8）。

        rembg.remove() は重いので UIスレッドでは実行せず、QThread + worker で行う。
        透過中は編集操作をロックし、二重実行を防ぐ。画像差し替えはメインスレッド側の
        finished スロットで行う（要件1・2・5）。
        """
        # 透過中・クローズ処理中は再入を無視する（二重実行防止: 要件2・11）。
        if self._background_removal_running or self._closing or self._close_in_progress:
            return
        image = self._selected_image_item()
        if image is None:
            return

        self._background_removal_running = True
        self._background_removal_target = image
        # 元画像退避は成功時に確定するため、ここでは保持しない（要件7）。

        btn = getattr(self, "_transparent_bg_button", None)
        if btn is not None:
            # processEvents は使わず、文言変更だけ行う。イベントループ復帰で再描画される（要件3）。
            btn.setText("準備中..." if self._rembg_warmup_running else "処理中...")
        self._set_edit_controls_enabled(False)
        self._update_image_action_buttons()

        # worker を別スレッドで起動する（要件4・5）。ウィンドウを親にしない QThread() で
        # 生成し、_image_threads で保持する（WA_DeleteOnClose 道連れ破棄の回避: 要件7・8）。
        self._bg_thread = QThread()
        self._bg_worker = BackgroundRemovalWorker(image.image_bytes)
        self._bg_worker.moveToThread(self._bg_thread)

        self._bg_thread.started.connect(self._bg_worker.run)
        self._bg_worker.finished.connect(self._on_background_removal_finished)
        self._bg_worker.failed.connect(self._on_background_removal_failed)

        self._bg_worker.finished.connect(self._bg_thread.quit)
        self._bg_worker.failed.connect(self._bg_thread.quit)
        self._bg_thread.finished.connect(self._bg_worker.deleteLater)
        self._bg_thread.finished.connect(self._bg_thread.deleteLater)

        # rembg は選択画像へ apply_processed_image() する加工なので blocking 扱い（要件3）。
        self._register_blocking_image_thread(self._bg_thread)
        self._bg_thread.start()

    @Slot(bytes)
    def _on_background_removal_finished(self, new_bytes: bytes) -> None:
        """透過成功時の処理（メインスレッド）。画像を差し替え、履歴・再描画を更新する（要件6）。"""
        # 閉じる処理中はUI更新も後始末も行わない（破棄済みオブジェクトへ触れない: 要件5・11）。
        if self._closing or self._close_in_progress:
            return
        try:
            item = self._background_removal_target
            # 対象画像が既に scene から外れている（削除済み）なら触らない（要件5）。
            if item is not None and item.scene() is not None:
                item.apply_background_removal_result(new_bytes)
                # 選択状態を維持したままハンドル・履歴・未保存フラグを更新する。
                self.refresh_handles()
                self.commit_history()
                self.mark_dirty()
                self._scene.update()
                if getattr(self, "_view", None) is not None:
                    self._view.viewport().update()
        except RuntimeError:
            # 破棄済み Qt オブジェクトへのアクセスは無視する（要件6）。
            _log.exception("背景透過の反映中にQtオブジェクト破棄エラー")
        finally:
            self._finish_background_removal()

    @Slot(str)
    def _on_background_removal_failed(self, message: str) -> None:
        """透過失敗時の処理（メインスレッド）。画像は差し替えず通知のみ行う（要件7）。"""
        # 閉じる処理中は通知・後始末を行わない（要件5・11）。
        if self._closing or self._close_in_progress:
            return
        try:
            _log.error("背景透過に失敗しました: %s", message)
            QMessageBox.warning(
                self,
                "背景透過",
                f"背景透過に失敗しました。\n\n{message}",
            )
        finally:
            self._finish_background_removal()

    def _finish_background_removal(self) -> None:
        """透過処理の後始末（成功/失敗共通）。フラグ解除・操作ロック解除・再描画（要件8）。"""
        self._background_removal_running = False
        self._background_removal_target = None
        self._bg_worker = None
        self._bg_thread = None

        btn = getattr(self, "_transparent_bg_button", None)
        if btn is not None:
            btn.setText(TRANSPARENT_REMBG_LABEL)
        self._set_edit_controls_enabled(True)
        self._update_image_action_buttons()
        self._scene.update()
        if getattr(self, "_view", None) is not None:
            self._view.viewport().update()

    def _on_restore_image(self) -> None:
        """選択中画像を加工前へ復元する（要件4・10）。位置・サイズ・選択状態は維持する。"""
        # 加工中・クローズ処理中は復元を無視する（状態破壊防止: 要件2・11）。
        if self._background_removal_running or self._closing or self._close_in_progress:
            return
        image = self._selected_image_item()
        if image is None or not image.has_original_image():
            return
        image.restore_original_image()
        self.refresh_handles()
        self.commit_history()
        self.mark_dirty()
        self._update_image_action_buttons()

    # ── 二値化／背景を透過（閾値）（要件6・7・11）────────────────────────────────
    def _apply_image_processing(self, mode: str) -> None:
        """選択中画像へ同期的な画像加工を適用する共通処理（要件11）。

        mode は "binarize"（二値化）/"threshold_transparent"（閾値透過）。rembg は
        別スレッド実行のため _on_transparent_background 側で扱う。位置・サイズ・倍率・
        選択状態は維持し、加工前の元画像は _EditImageItem 側で一度だけ退避する（要件6・7・10）。
        """
        # rembg 加工中・クローズ処理中は再入を無視する（二重実行・状態破壊防止: 要件2・11）。
        if self._background_removal_running or self._closing or self._close_in_progress:
            return
        image = self._selected_image_item()
        if image is None:
            return
        if mode == "binarize":
            result = make_binarized_bytes(image.image_bytes, self._threshold_rgb)
        elif mode == "threshold_transparent":
            result = make_threshold_transparent_bytes(image.image_bytes, self._threshold_rgb)
        else:  # pragma: no cover - 想定外の mode は無視
            return
        image.apply_processed_image(result)
        # 選択状態を維持したままハンドル・履歴・未保存フラグを更新する（要件6・7）。
        self.refresh_handles()
        self.commit_history()
        self.mark_dirty()
        self._update_image_action_buttons()
        self._scene.update()
        if getattr(self, "_view", None) is not None:
            self._view.viewport().update()

    def _on_binarize(self) -> None:
        """二値化ボタン押下（要件6）。"""
        self._apply_image_processing("binarize")

    def _on_threshold_transparent(self) -> None:
        """背景を透過（閾値）ボタン押下（要件7）。"""
        self._apply_image_processing("threshold_transparent")

    def _on_threshold_settings(self) -> None:
        """閾値設定ボタン押下：RGB 閾値ダイアログを開く（要件8・9）。"""
        dialog = ThresholdSettingsDialog(self._threshold_rgb, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._threshold_rgb = dialog.values()
            save_threshold_rgb(self._threshold_rgb)

    def _update_template_highlight(self) -> None:
        """選択中テンプレートのボタンだけをハイライトする（通常／タブレット両パネル）。"""
        for buttons in (self._template_actions,
                        getattr(self, "_tablet_reflect_buttons", {})):
            for name, btn in buttons.items():
                checked = name == self._current_template_name
                btn.setChecked(checked)
                btn.setProperty("reflectTargetSelected", checked)
                self._apply_reflect_target_button_style(btn, checked)
                btn.update()

    def _apply_reflect_target_button_style(
        self,
        button: QPushButton,
        selected: bool,
    ) -> None:
        """反映先の選択状態をグローバルQSSに依存せずボタンへ直接反映する。"""
        if selected:
            button.setStyleSheet(REFLECT_TARGET_SELECTED_STYLE)
        elif current_title_bar_is_dark():
            button.setStyleSheet(REFLECT_TARGET_DARK_STYLE)
        else:
            button.setStyleSheet(REFLECT_TARGET_LIGHT_STYLE)

    def _refresh_reflect_target_button_styles(self) -> None:
        """テーマ変更後も、現在画面にある全反映先ボタンへ直接スタイルを再適用する。"""
        self._update_template_highlight()

    def _template_by_name(self, name: str) -> dict[str, Any] | None:
        for tpl in self._templates:
            if tpl["name"] == name:
                return tpl
        return None

    def _add_template_action(self, tpl: dict[str, Any]) -> None:
        """テンプレートボタンを1つ縦並びパネルへ追加する（名前ルックアップで上書きにも追従）。"""
        name = tpl["name"]
        locked = is_locked_template(name)
        # 固定テンプレートは表示だけロックバッヂを付ける（内部キー name は不変: 要件3・10）。
        btn = QPushButton(("🔒 " + name) if locked else name)
        btn.setObjectName("reflectTargetButton")
        btn.setProperty("reflectTargetButton", True)
        btn.setProperty("reflectTargetSelected", False)
        btn.setCheckable(True)
        self._apply_reflect_target_button_style(btn, False)
        if locked:
            btn.setToolTip(
                "反映先伝票: " + ", ".join(tpl["target_vouchers"])
                + "（固定テンプレートのため削除・名前変更はできません）")
        else:
            btn.setToolTip(
                "反映先伝票: " + ", ".join(tpl["target_vouchers"]) + "（右クリックで編集/削除）")
        btn.clicked.connect(
            lambda checked=False, n=name: self._on_template_selected(self._template_by_name(n))
        )
        # 右クリックで編集/削除メニューを表示する（固定テンプレートは出さない: 要件1・7）。
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, n=name, b=btn: self._show_template_context_menu(n, b.mapToGlobal(pos))
        )
        self._template_button_group.addButton(btn)
        layout = self._template_panel_layout
        register = getattr(self, "_register_template_button", None)
        if register is not None:
            # 「テンプレ登録」ボタンの直前へ挿入する。
            layout.insertWidget(layout.indexOf(register), btn)
        else:
            # 初期構築ループ中（登録ボタン未生成）は末尾へ追加する。
            layout.addWidget(btn)
        self._template_actions[tpl["name"]] = btn

    # ── 反映先テンプレートの編集/削除（要件1）──────────────────────────────────────
    def _show_template_context_menu(self, name: str, global_pos) -> None:
        """テンプレートボタンの右クリックメニュー（編集/削除）を表示する。

        固定テンプレート（標準/全伝票）はメニューを表示しない（要件7）。
        """
        if is_locked_template(name):
            return
        menu = QMenu(self)
        edit_action = menu.addAction("編集")
        delete_action = menu.addAction("削除")
        chosen = menu.exec(global_pos)
        if chosen is edit_action:
            self._edit_template(name)
        elif chosen is delete_action:
            self._delete_template(name)

    def _reload_templates_panel(self, select_name: str | None = None) -> None:
        """テンプレート一覧を再読み込みしてボタンを作り直す（編集/削除後の再描画）。"""
        self._templates = load_templates()
        for btn in list(self._template_actions.values()):
            self._template_button_group.removeButton(btn)
            self._template_panel_layout.removeWidget(btn)
            btn.deleteLater()
        self._template_actions = {}
        for tpl in self._templates:
            self._add_template_action(tpl)
        # タブレット反映先パネルも同じテンプレート一覧で作り直す（タスク2）。
        self._reload_tablet_reflect_panel()
        if select_name and self._template_by_name(select_name) is not None:
            self._on_template_selected(self._template_by_name(select_name))
        else:
            # 選択中テンプレートが削除されていてもオブジェクトの反映先は保持する。
            if self._template_by_name(self._current_template_name) is None:
                self._current_template_name = ""
            self._update_template_highlight()
        # テンプレート変更（バッヂ色・テンプレ名）を反映するため常に再描画する。
        self.refresh_badges()

    def _edit_template(self, name: str) -> None:
        """対象テンプレートを編集ダイアログで更新し、保存・再描画する（要件1）。"""
        if is_locked_template(name):
            # 固定テンプレートは名前変更・編集不可（要件3・7）。
            return
        tpl = self._template_by_name(name)
        if tpl is None:
            return
        dialog = _TemplateRegisterDialog(list(tpl["target_vouchers"]), self,
                                         name=tpl["name"], color=tpl.get("color"))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.template()
        if updated is None:
            QMessageBox.warning(self, "テンプレート編集", "テンプレート名と反映先伝票を入力してください。")
            return
        try:
            user = load_user_templates()
            # 旧名・新名の重複を除いてから保存する（リネームにも対応）。
            user = [t for t in user if t["name"] not in (name, updated["name"])]
            user.append(updated)
            save_user_templates(user)
        except Exception as exc:
            QMessageBox.critical(self, "テンプレート編集エラー", f"テンプレートの保存に失敗しました:\n{exc}")
            return
        self._reload_templates_panel(select_name=updated["name"])

    def _delete_template(self, name: str) -> None:
        """対象テンプレートを確認のうえ削除し、再描画する（要件6〜8）。

        - 固定テンプレート（標準/全伝票）は削除しない（要件7）。
        - 確認ダイアログで「はい」のときだけ削除する（要件6）。
        - 削除したテンプレートを使っていたオブジェクトの反映先は「標準」へ置き換える（要件8）。
        - 削除対象が選択中だった場合は選択を「標準」へ戻す（要件8）。
        """
        if is_locked_template(name):
            # 内部から呼ばれても固定テンプレートは削除しない（要件7）。警告ログのみ。
            logging.warning("固定テンプレート「%s」は削除できません。", name)
            return
        tpl = self._template_by_name(name)
        if tpl is None:
            return
        reply = QMessageBox.question(
            self, "テンプレート削除", f"テンプレート「{name}」を削除しますか？"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted_targets = list(tpl["target_vouchers"])
        try:
            if not delete_template(name):
                logging.warning("テンプレート「%s」は削除できませんでした。", name)
                return
        except Exception as exc:
            QMessageBox.critical(self, "テンプレート削除エラー", f"テンプレートの削除に失敗しました:\n{exc}")
            return
        # 削除テンプレートを反映先に持つオブジェクトは「標準」へ置き換える（要件8）。
        self._reassign_objects_from_deleted_template(deleted_targets)
        # 削除対象が選択中なら選択を「標準」へ戻す（要件8）。
        if self._current_template_name == name:
            self._current_template_name = "標準"
            self.current_target_vouchers = list(DEFAULT_TARGET_VOUCHERS)
        self._reload_templates_panel(select_name=self._current_template_name)

    def _reassign_objects_from_deleted_template(self, deleted_targets: list[str]) -> None:
        """削除テンプレートを反映先に持つオブジェクトを「標準」へ置き換える（要件8）。"""
        key = frozenset(str(v) for v in deleted_targets)
        changed = False
        for it in self.edit_items():
            if frozenset(str(v) for v in getattr(it, "target_vouchers", [])) == key:
                it.target_vouchers = list(DEFAULT_TARGET_VOUCHERS)
                changed = True
        if changed:
            self.mark_dirty()

    # ── オブジェクト右クリックメニュー ───────────────────────────────────────
    def _show_object_context_menu(self, item: QGraphicsItem, global_pos) -> None:
        menu = self._build_object_context_menu(item)
        menu.exec(global_pos)

    def _build_object_context_menu(self, item: QGraphicsItem) -> QMenu:
        """編集オブジェクト用の右クリックメニューを生成する。"""
        menu = QMenu(self)
        menu._submenus = []  # type: ignore[attr-defined]
        if isinstance(item, (_EditLineItem, _EditRectItem, _EditEllipseItem)):
            width_menu = QMenu("線幅", menu)
            menu.addMenu(width_menu)
            menu._submenus.append(width_menu)  # type: ignore[attr-defined]
            width_menu.setObjectName("line_width_menu")
            for width in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0):
                act = width_menu.addAction(f"{width:g}")
                act.setCheckable(True)
                act.setChecked(abs(float(getattr(item, "line_width", 0.0)) - width) < 0.01)
                act.triggered.connect(
                    lambda checked=False, it=item, w=width: self._set_object_line_width(it, w)
                )
        if isinstance(item, (_EditTextItem, _EditSymbolTextItem,
                             _EditRectItem, _EditEllipseItem, _EditLineItem)):
            font_menu = QMenu("文字サイズ", menu)
            menu.addMenu(font_menu)
            menu._submenus.append(font_menu)  # type: ignore[attr-defined]
            font_menu.setObjectName("font_size_menu")
            for size in (8, 10, 12, 14, 18, 24, 36, 48):
                act = font_menu.addAction(str(size))
                act.setCheckable(True)
                act.setChecked(abs(float(getattr(item, "font_size", 0.0)) - size) < 0.01)
                act.triggered.connect(
                    lambda checked=False, it=item, s=size: self._set_object_font_size(it, s)
                )

        delete_action = menu.addAction("削除")
        delete_action.setObjectName("delete_action")
        delete_action.triggered.connect(lambda checked=False, it=item: self._delete_object(it))

        target_menu = QMenu("反映先", menu)
        menu.addMenu(target_menu)
        menu._submenus.append(target_menu)  # type: ignore[attr-defined]
        target_menu.setObjectName("target_menu")
        current = list(getattr(item, "target_vouchers", DEFAULT_TARGET_VOUCHERS))
        current_key = frozenset(current)
        for tpl in self._templates:
            act = target_menu.addAction(str(tpl["name"]))
            act.setCheckable(True)
            act.setChecked(frozenset(str(v) for v in tpl["target_vouchers"]) == current_key)
            act.triggered.connect(
                lambda checked=False, it=item, t=tpl: self._set_object_target_vouchers(
                    it, list(t["target_vouchers"])
                )
            )

        if isinstance(item, _EditImageItem):
            transparent_action = menu.addAction("背景を透過")
            transparent_action.setObjectName("transparent_background_action")
            transparent_action.setEnabled(not self._background_removal_running)
            transparent_action.triggered.connect(
                lambda checked=False, it=item: self._run_threshold_transparency_for_item(it)
            )
            restore_action = menu.addAction("背景を戻す")
            restore_action.setObjectName("restore_background_action")
            restore_action.setEnabled(
                not self._background_removal_running and item.has_original_image()
            )
            restore_action.triggered.connect(
                lambda checked=False, it=item: self._restore_image_item(it)
            )
        return menu

    def _set_object_line_width(self, item: QGraphicsItem, width: float) -> None:
        if not hasattr(item, "apply_line_width"):
            return
        item.apply_line_width(float(width))
        self._select_only(item)
        self.refresh_handles()
        self.commit_history()
        self.mark_dirty()

    def _set_object_font_size(self, item: QGraphicsItem, size: float) -> None:
        if not hasattr(item, "apply_font_size"):
            return
        item.apply_font_size(float(size))
        self._select_only(item)
        self.refresh_handles()
        self.commit_history()
        self.mark_dirty()

    def _set_object_target_vouchers(self, item: QGraphicsItem, targets: list[str]) -> None:
        if not hasattr(item, "target_vouchers"):
            return
        item.target_vouchers = _normalize_target_vouchers(targets)
        self._select_only(item)
        self.refresh_badges()
        self.commit_history()
        self.mark_dirty()

    def _delete_object(self, item: QGraphicsItem) -> None:
        if not hasattr(item, "serialize_edit_object"):
            return
        self._select_only(item)
        self.delete_selected()

    def _run_threshold_transparency_for_item(self, item: "_EditImageItem") -> None:
        """右クリックの「背景を透過」は保存済み閾値による透過を行う。"""
        self._suppress_rembg_warmup = True
        try:
            self._select_only(item)
        finally:
            self._suppress_rembg_warmup = False
        self._on_threshold_transparent()

    def _restore_image_item(self, item: "_EditImageItem") -> None:
        self._select_only(item)
        self._on_restore_image()

    def _on_register_template(self) -> None:
        """反映先テンプレートを新規登録/上書き保存する（要件4）。"""
        dialog = _TemplateRegisterDialog(list(self.current_target_vouchers), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        template = dialog.template()
        if template is None:
            QMessageBox.warning(self, "テンプレート登録", "テンプレート名と反映先伝票を入力してください。")
            return
        try:
            user = load_user_templates()
            user = [t for t in user if t["name"] != template["name"]]
            user.append(template)
            save_user_templates(user)
        except Exception as exc:
            QMessageBox.critical(self, "テンプレート登録エラー", f"テンプレートの保存に失敗しました:\n{exc}")
            return
        # テンプレート一覧を再読み込みし、ボタンを最新化する。
        self._templates = load_templates()
        if template["name"] not in self._template_actions:
            self._add_template_action(self._template_by_name(template["name"]) or template)
        else:
            btn = self._template_actions[template["name"]]
            btn.setToolTip("反映先伝票: " + ", ".join(template["target_vouchers"]))
        # 登録したテンプレートを選択状態にする。
        self._on_template_selected(self._template_by_name(template["name"]) or template)

    # ── テンプレートバッヂ（編集画面のみ表示・保存/PDF/Undo対象外: 要件6）────────────
    def _remove_badges(self) -> None:
        for b in self._badges:
            if b.scene() is not None:
                self._scene.removeItem(b)
        self._badges = []

    def refresh_badges(self) -> None:
        """各編集オブジェクトの左上へテンプレート色バッヂを描き直す（要件6）。"""
        self._remove_badges()
        for item in self.edit_items():
            targets = list(getattr(item, "target_vouchers", DEFAULT_TARGET_VOUCHERS))
            tpl = self._template_for_targets(targets)
            color = tpl["color"] if tpl else "#607d8b"
            label = tpl["badge"] if tpl else "他"
            self._add_badge_for_item(item, color, label)

    def _badge_anchor_scene(self, item: QGraphicsItem) -> QPointF:
        if isinstance(item, (_EditTextItem, _EditImageItem)):
            r = item.box_rect_scene()
            return QPointF(r.left(), r.top())
        if isinstance(item, (_EditRectItem, _EditEllipseItem)):
            r = _scene_rect_from_item_rect(item, item.rect())
            return QPointF(r.left(), r.top())
        return QPointF(item.sceneBoundingRect().left(), item.sceneBoundingRect().top())

    def _add_badge_for_item(self, item: QGraphicsItem, color: str, label: str) -> None:
        anchor = self._badge_anchor_scene(item)
        w, h = 15.0, 11.0
        badge = QGraphicsRectItem(0.0, 0.0, w, h)
        badge.setBrush(QBrush(QColor(color)))
        badge.setPen(QPen(QColor(255, 255, 255)))
        # オブジェクトより前面・ハンドルより背面。クリックは透過させて選択を妨げない。
        badge.setZValue(50)
        badge.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        badge.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        badge.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        # 左上に少し被せる位置（バッヂ右下をオブジェクト左上付近へ）。
        badge.setPos(anchor.x(), anchor.y() - h)
        # 保存・PDF・全選択・Undo対象外の目印（要件6）。
        badge._IS_BADGE = True  # type: ignore[attr-defined]
        badge._IS_HELPER = True  # type: ignore[attr-defined]
        badge.setData(_DATA_TYPE, _IS_BADGE)
        text = QGraphicsSimpleTextItem(label, badge)
        text.setBrush(QBrush(QColor(255, 255, 255)))
        tf = QFont()
        tf.setPointSizeF(6.0)
        tf.setBold(True)
        text.setFont(tf)
        text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        tr = text.boundingRect()
        text.setPos((w - tr.width()) / 2.0, (h - tr.height()) / 2.0)
        self._scene.addItem(badge)
        self._badges.append(badge)

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
                      manual_resized: bool = False,
                      target_vouchers: list[str] | None = None) -> _EditTextItem:
        fs = self.current_font_size if font_size is None else font_size
        lw = self.current_line_width if line_width is None else line_width
        tv = self._resolve_target_vouchers(target_vouchers)
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
                             manual_resized=manual_resized,
                             target_vouchers=tv)
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
                        anchor: str = "center",
                        target_vouchers: list[str] | None = None) -> _EditSymbolTextItem:
        fs = self.current_font_size if font_size is None else font_size
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditSymbolTextItem(text=text, obj_id=obj_id, font_size=fs,
                                   font_family=font_family,
                                   font_bold=font_bold,
                                   font_italic=font_italic,
                                   text_color=text_color,
                                   anchor=anchor,
                                   target_vouchers=tv)
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
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 font_size: float | None = None,
                 line_type: str = LINE_TYPE_LINE,
                 target_vouchers: list[str] | None = None) -> _EditLineItem:
        lw = self.current_line_width if line_width is None else line_width
        fs = self.current_font_size if font_size is None else font_size
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditLineItem(p1.x(), p1.y(), p2.x(), p2.y(),
                             obj_id=obj_id, line_width=lw,
                             stroke_color=stroke_color,
                             font_size=fs,
                             line_type=line_type,
                             target_vouchers=tv)
        self._register(item)
        return item

    # ── 手書きフリーハンド（タブレット編集モード主体）─────────────────────────
    def add_freehand(self, points: list, obj_id: str | None = None,
                     pen_width: float | None = None,
                     stroke_color: str | None = None,
                     target_vouchers: list[str] | None = None,
                     register_id: bool = True) -> _EditFreehandItem:
        """手書きフリーハンドオブジェクトを編集レイヤーへ追加する。

        pen_width / stroke_color を省略すると現在のペン設定を使う。
        register_id=False の場合は作成途中（ドラッグ中）とみなし、scene へは出すが
        loaded_object_ids への登録は確定時まで保留する。
        """
        pw = self.current_pen_width if pen_width is None else pen_width
        stroke_color = self.current_pen_color if stroke_color is None else stroke_color
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditFreehandItem(points, obj_id=obj_id, pen_width=pw,
                                 stroke_color=stroke_color, target_vouchers=tv)
        if register_id:
            self._register(item)
        else:
            self._scene.addItem(item)
        return item

    @staticmethod
    def _freehand_near(item: _EditFreehandItem, scene_pos: QPointF) -> bool:
        """手書き線の構成点のいずれかが消しゴム半径内にあるか判定する。"""
        r = ERASER_RADIUS
        for p in item.points():
            sp = item.mapToScene(p)
            if abs(sp.x() - scene_pos.x()) <= r and abs(sp.y() - scene_pos.y()) <= r:
                return True
        return False

    def erase_freehand_at(self, scene_pos: QPointF) -> bool:
        """指定位置に近い手書き線を削除する（簡易方式の消しゴム）。

        タブレット編集モードでは現在選択中レイヤーの stroke だけを対象にする（要件3）。
        通常モードでは独立した freehand オブジェクトを丸ごと削除する（従来挙動）。
        """
        if self.tablet_mode:
            layer = self.current_freehand_layer()
            if layer is None:
                return False
            removed = layer.erase_near(scene_pos)
            if removed:
                self._eraser_changed = True
                self.ensure_background_visible()
            return removed
        removed = False
        for item in list(self.edit_items()):
            if item.data(_DATA_TYPE) != "freehand":
                continue
            if self._freehand_near(item, scene_pos):
                if item.scene() is not None:
                    self._scene.removeItem(item)
                self.loaded_object_ids.discard(item.obj_id)
                removed = True
        if removed:
            self._eraser_changed = True
            self.ensure_background_visible()
        return removed

    def commit_eraser_if_changed(self) -> None:
        """消しゴムドラッグで削除があったら、まとめて1操作として履歴へ積む。"""
        if self._eraser_changed:
            self._eraser_changed = False
            self.mark_dirty()
            self.commit_history()
            self.ensure_background_visible()

    def clear_freehand_all(self) -> None:
        """手書き線をすべて消去する（全消去ボタン）。

        タブレット編集モードでは現在選択中レイヤーのストロークだけを消す（要件3）。
        通常モードでは独立した freehand オブジェクトを全削除する（従来挙動）。
        """
        if self.tablet_mode:
            layer = self.current_freehand_layer()
            if layer is None or layer.stroke_count() == 0:
                return
            # 全ストロークを消しゴム対象にするため、巨大半径で erase する代わりに
            # ストロークリストを空にする。
            layer.erase_near(QPointF(0, 0), radius=max(PAGE_W, PAGE_H) * 2)
            self.mark_dirty()
            self.commit_history()
            self.ensure_background_visible()
            self._refresh_layer_panel()
            return
        removed = False
        for item in list(self.edit_items()):
            if item.data(_DATA_TYPE) == "freehand":
                if item.scene() is not None:
                    self._scene.removeItem(item)
                self.loaded_object_ids.discard(item.obj_id)
                removed = True
        if removed:
            self.mark_dirty()
            self.commit_history()
            self.ensure_background_visible()

    # ── 手書きペンの太さ・色 ───────────────────────────────────────────────────
    def _pen_width_name(self) -> str:
        for name, w in PEN_WIDTHS:
            if abs(w - self.current_pen_width) < 1e-6:
                return name
        return "中"

    def pen_width_label(self) -> str:
        return f"太さ:{self._pen_width_name()}"

    def cycle_pen_width(self) -> None:
        widths = [w for _, w in PEN_WIDTHS]
        try:
            idx = widths.index(self.current_pen_width)
        except ValueError:
            idx = 1
        self.current_pen_width = widths[(idx + 1) % len(widths)]
        act = getattr(self, "_pen_width_action", None)
        if act is not None:
            act.setText(self.pen_width_label())
        self._apply_pen_setting_to_current_layer()

    def _pen_color_name(self) -> str:
        for name, c in PEN_COLORS:
            if c.lower() == str(self.current_pen_color).lower():
                return name
        return "黒"

    def pen_color_label(self) -> str:
        return f"色:{self._pen_color_name()}"

    def cycle_pen_color(self) -> None:
        colors = [c for _, c in PEN_COLORS]
        try:
            idx = colors.index(self.current_pen_color)
        except ValueError:
            idx = 0
        self.current_pen_color = colors[(idx + 1) % len(colors)]
        act = getattr(self, "_pen_color_action", None)
        if act is not None:
            act.setText(self.pen_color_label())
        self._apply_pen_setting_to_current_layer()

    def _apply_pen_setting_to_current_layer(self) -> None:
        """タブレット編集モードで太さ・色を現在レイヤーへ反映する（要件4）。"""
        if not self.tablet_mode:
            return
        layer = self.current_freehand_layer()
        if layer is None:
            return
        layer.pen_width = float(self.current_pen_width)
        layer.stroke_color = _color_name(self.current_pen_color)

    def add_image(self, image_bytes: bytes, rect: QRectF | None = None,
                  obj_id: str | None = None, image_format: str = "png",
                  width: float | None = None, height: float | None = None,
                  select: bool = True,
                  target_vouchers: list[str] | None = None) -> "_EditImageItem | None":
        """画像オブジェクトを編集レイヤーへ追加する（要件2-3・2-4）。

        rect を渡せばその位置・大きさで配置する。省略時は画像の自然サイズで
        scene 中央に配置する。
        """
        if not image_bytes:
            return None
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditImageItem(image_bytes, image_format=image_format,
                              obj_id=obj_id, width=width, height=height,
                              target_vouchers=tv)
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
        if self._background_removal_running:
            return None
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
        if self._background_removal_running:
            return None
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
                 vertical_align: str = "middle",
                 target_vouchers: list[str] | None = None) -> _EditRectItem:
        lw = self.current_line_width if line_width is None else line_width
        fs = self.current_font_size if font_size is None else font_size
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditRectItem(rect, obj_id=obj_id, line_width=lw,
                             text=text, font_size=fs,
                             font_family=font_family, font_bold=font_bold,
                             font_italic=font_italic, text_color=text_color,
                             stroke_color=stroke_color, fill_color=fill_color,
                             text_align=text_align,
                             vertical_align=vertical_align,
                             target_vouchers=tv)
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
                    vertical_align: str = "middle",
                    target_vouchers: list[str] | None = None) -> _EditEllipseItem:
        lw = self.current_line_width if line_width is None else line_width
        fs = self.current_font_size if font_size is None else font_size
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditEllipseItem(rect, obj_id=obj_id, line_width=lw,
                                text=text, font_size=fs,
                                font_family=font_family, font_bold=font_bold,
                                font_italic=font_italic,
                                text_color=text_color,
                                stroke_color=stroke_color,
                                fill_color=fill_color,
                                text_align=text_align,
                                vertical_align=vertical_align,
                                target_vouchers=tv)
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
                                      text_color=item.text_color,
                                      target_vouchers=list(
                                          getattr(item, "target_vouchers",
                                                  DEFAULT_TARGET_VOUCHERS)))
        if was_selected:
            self._select_only(symbol)
        self.ensure_background_visible()
        self.commit_history()
        return True

    def delete_selected(self) -> None:
        if self._background_removal_running:
            return
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

    def copy_selected_objects(self) -> bool:
        """選択中編集オブジェクトをアプリ内クリップボードへコピーする。"""
        if self._is_text_editing():
            return False
        selected = self._selected_edit_items()
        if not selected:
            return False
        self._object_clipboard = [
            dict(item.serialize_edit_object())
            for item in selected
        ]
        return True

    def paste_copied_objects(self) -> bool:
        """アプリ内クリップボードの編集オブジェクトを少しずらして複製する。"""
        if self._background_removal_running or self._is_text_editing():
            return False
        if not self._selected_edit_items():
            return False
        if not self._object_clipboard:
            return False
        new_ids: list[str] = []
        for source in self._object_clipboard:
            obj = dict(source)
            obj["id"] = str(uuid.uuid4())
            self._offset_copied_object(obj)
            new_ids.append(obj["id"])
            self._add_loaded_object(obj)
        created = [it for it in self.edit_items()
                   if getattr(it, "obj_id", None) in set(new_ids)]
        self._scene.clearSelection()
        for item in created:
            item.setSelected(True)
        if len(created) == 1:
            self._select_only(created[0])
        self.set_tool(TOOL_SELECT)
        self.refresh_handles()
        self.commit_history()
        self.mark_dirty()
        self.ensure_background_visible()
        return bool(created)

    def _selected_edit_items(self) -> list[QGraphicsItem]:
        try:
            return [it for it in self._scene.selectedItems()
                    if hasattr(it, "serialize_edit_object")]
        except RuntimeError:
            return []

    @staticmethod
    def _offset_copied_object(obj: dict[str, Any]) -> None:
        kind = obj.get("type")
        if kind == "line":
            for key in ("x1", "x2"):
                obj[key] = float(obj.get(key, 0.0)) + PASTE_OFFSET_X
            for key in ("y1", "y2"):
                obj[key] = float(obj.get(key, 0.0)) + PASTE_OFFSET_Y
            return
        if "x" in obj:
            obj["x"] = float(obj.get("x", 0.0)) + PASTE_OFFSET_X
        if "y" in obj:
            obj["y"] = float(obj.get("y", 0.0)) + PASTE_OFFSET_Y

    def select_all(self) -> None:
        """編集オブジェクトだけを全選択する（背景・ハンドルは除く: 要件4）。"""
        self._scene.clearSelection()
        for item in self.edit_items():
            item.setSelected(True)
        self.ensure_background_visible()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # F11 でタブレット編集モードを終了する（要件: 可能であれば F11）。
        if event.key() == Qt.Key.Key_F11 and self.tablet_mode:
            self.exit_tablet_mode()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            # タブレット編集モード中の Esc はタブレット終了を最優先する。
            if self.tablet_mode:
                self.exit_tablet_mode()
                event.accept()
                return
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
        if event.matches(QKeySequence.StandardKey.Copy):
            if self.copy_selected_objects():
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
        if self._selected_edit_items() and self._object_clipboard and self.paste_copied_objects():
            return True
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
        for h in list(self._handles):
            try:
                if h.scene() is not None:
                    self._scene.removeItem(h)
            except RuntimeError:
                # 既に破棄された Qt オブジェクトは無視する（要件6）。
                pass
        self._handles = []

    def _cleanup_selection_handles(self) -> None:
        """選択ハンドルを安全に撤去する（クローズ処理用: 要件4・6）。

        破棄済み QGraphicsItem へのアクセスで Runtime: Internal C++ object already
        deleted が出ないよう、例外を握りつぶしてリストを空にする。
        """
        self._remove_handles()

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
        if len(selected) == 1:
            targets = list(getattr(selected[0], "target_vouchers", []))
            template = self._template_for_targets(targets)
            if template is not None:
                self.current_target_vouchers = list(template["target_vouchers"])
                self._current_template_name = str(template["name"])
            else:
                # 一致するテンプレートが無い反映先では、別ボタンを誤って選択表示しない。
                self.current_target_vouchers = _normalize_target_vouchers(targets)
                self._current_template_name = ""
            self._update_template_highlight()
        # 画像選択状態に応じて画像編集ボタンの表示を切り替える（要件1・2・5）。
        self._update_image_action_buttons()
        # 単一選択時のみハンドル表示。複数選択や未選択では選択枠を出さない（要件10）。
        if len(selected) != 1:
            return
        target = selected[0]
        if isinstance(target, (_EditRectItem, _EditEllipseItem, _EditTextItem, _EditImageItem)):
            for position in (
                "top_left", "top", "top_right", "right",
                "bottom_right", "bottom", "bottom_left", "left",
            ):
                handle = _ResizeHandle(target, position)
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
        # 移動/リサイズ後はバッヂ位置も追従させる（要件6）。
        self.refresh_badges()

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
        # 変更後のバッヂを描き直す（要件6）。
        self.refresh_badges()

    def undo(self) -> None:
        if self._background_removal_running:
            return
        if self._history_index > 0:
            self._history_index -= 1
            self._restore_snapshot(self._history[self._history_index])
            self.mark_dirty()
        self._debug_state("undo")

    def redo(self) -> None:
        if self._background_removal_running:
            return
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
        # Undo/Redo後もバッヂを最新化する（要件6）。
        self.refresh_badges()
        # 手書きレイヤーを再構築したのでパネルと選択状態を最新化する（要件3）。
        if self._current_layer_id is not None and self.current_freehand_layer() is None:
            layers = self.freehand_layers()
            self._current_layer_id = layers[0].layer_id if layers else None
        self._refresh_layer_panel()

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
        # 読み込んだオブジェクトのバッヂを表示する（要件6）。
        self.refresh_badges()
        # 読み込んだ手書きレイヤーをレイヤーパネルへ反映する（要件3）。
        self._refresh_layer_panel()

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
        target_vouchers = _normalize_target_vouchers(obj.get("target_vouchers"))
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
                                 anchor=str(obj.get("anchor") or "center"),
                                 target_vouchers=target_vouchers)
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
                               manual_resized=bool(obj.get("manual_resized", False)),
                               target_vouchers=target_vouchers)
        elif kind == "line":
            x1 = float(obj.get("x1", 0.0)); raw_y1 = float(obj.get("y1", 0.0))
            x2 = float(obj.get("x2", 0.0)); raw_y2 = float(obj.get("y2", 0.0))
            y1 = raw_y1 if is_scene_origin else PAGE_H - raw_y1
            y2 = raw_y2 if is_scene_origin else PAGE_H - raw_y2
            self.add_line(QPointF(x1, y1), QPointF(x2, y2), obj_id=obj_id,
                          line_width=line_width, stroke_color=stroke_color,
                          font_size=font_size,
                          line_type=normalize_line_type(obj.get("line_type")),
                          target_vouchers=target_vouchers)
        elif kind == "freehand":
            raw_points = obj.get("points") or []
            pts: list[tuple[float, float]] = []
            for p in raw_points:
                try:
                    px = float(p[0])
                    py = float(p[1])
                except (TypeError, ValueError, IndexError):
                    continue
                py_scene = py if is_scene_origin else PAGE_H - py
                pts.append((px, py_scene))
            if pts:
                pen_w = float(obj.get("pen_width") or obj.get("line_width")
                              or DEFAULT_PEN_WIDTH)
                self.add_freehand(pts, obj_id=obj_id, pen_width=pen_w,
                                  stroke_color=stroke_color,
                                  target_vouchers=target_vouchers)
        elif kind == "freehand_layer":
            pen_w = float(obj.get("pen_width") or obj.get("line_width")
                          or DEFAULT_PEN_WIDTH)
            raw_strokes = obj.get("strokes") or []
            strokes: list[dict[str, Any]] = []
            for s in raw_strokes:
                if not isinstance(s, dict):
                    continue
                raw_pts = s.get("points") or []
                pts2: list[list[float]] = []
                for p in raw_pts:
                    try:
                        px = float(p[0]); py = float(p[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    py_scene = py if is_scene_origin else PAGE_H - py
                    pts2.append([px, py_scene])
                if len(pts2) < 2:
                    continue
                strokes.append({
                    "points": pts2,
                    "pen_width": float(s.get("pen_width") or pen_w),
                    "stroke_color": str(s.get("stroke_color") or stroke_color),
                })
            self.add_freehand_layer(
                layer_name=str(obj.get("layer_name") or "レイヤー"),
                target_vouchers=target_vouchers,
                pen_width=pen_w, stroke_color=stroke_color,
                layer_id=str(obj.get("layer_id") or obj_id or "") or None,
                visible=bool(obj.get("visible", True)),
                locked=bool(obj.get("locked", False)),
                strokes=strokes, select=False)
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
                           select=False, target_vouchers=target_vouchers)
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
                  vertical_align=str(obj.get("vertical_align") or "middle"),
                  target_vouchers=target_vouchers)

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
        if self._background_removal_running:
            return
        if not self._persist():
            return
        QMessageBox.information(self, "保存完了", "保存しました")

    def save_and_close(self) -> None:
        """保存に成功したら画面を閉じる。失敗時は閉じない（要件5）。"""
        if self._background_removal_running:
            return
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

    # ── 実行中スレッド管理／非同期クローズ（要件3・5・6・9・10・15）─────────────
    def _has_running_blocking_image_threads(self) -> bool:
        """閉じる時に終了を待つ必要がある画像加工スレッドが走行中か（要件5・9）。

        warmup（補助）スレッドは含めない。破棄済みスレッドへのアクセスは無視する。
        """
        for thread in list(self._blocking_image_threads):
            try:
                if thread is not None and thread.isRunning():
                    return True
            except RuntimeError:
                continue
        return False

    def _request_image_threads_stop(self) -> None:
        """実行中の blocking スレッドへ停止を要求する（要件10）。

        quit() は呼ぶが即時停止は期待しない。worker.run() の重い処理は割り込めないため、
        ここでは wait() しない（GUIスレッドを固めないため: 要件3・6・12）。warmup スレッドは
        待機対象でないため、ここでは触らない（要件4・12）。
        """
        for thread in list(self._blocking_image_threads):
            try:
                thread.requestInterruption()
                thread.quit()
            except RuntimeError:
                pass

    def _show_closing_overlay(self, message: str) -> None:
        """クローズ待ち中、画面中央へ簡単なメッセージを出す（グレーアウト対策: 要件14）。"""
        try:
            if self._closing_overlay is None:
                overlay = QLabel(message, self)
                overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
                overlay.setStyleSheet(
                    "background-color: rgba(0, 0, 0, 140); color: white;"
                    " font-size: 18px;"
                )
                self._closing_overlay = overlay
            else:
                self._closing_overlay.setText(message)
            self._closing_overlay.setGeometry(self.rect())
            self._closing_overlay.show()
            self._closing_overlay.raise_()
        except Exception:  # pragma: no cover - 表示は補助的。失敗してもクローズは継続。
            _log.exception("クローズ待ちオーバーレイの表示でエラー")

    def _begin_async_close(self) -> None:
        """実行中スレッドの終了を待ってから閉じる非同期クローズを開始する（要件3・5・15）。"""
        if self._close_in_progress:
            return
        self._close_in_progress = True
        self._closing = True
        # 待機中は操作を止めるが、固まって見えないよう待機表示を出す（要件14）。
        self._show_closing_overlay("画像処理の終了を待っています...")
        self._request_image_threads_stop()
        # 既に blocking スレッドが終わっていれば即クローズへ進む（warmup は待たない: 要件4）。
        if not self._has_running_blocking_image_threads():
            self._finish_async_close()

    def _on_image_thread_finished(self) -> None:
        """blocking 画像加工スレッドの終了通知。非同期クローズ中で全終了なら閉じる（要件5）。"""
        if self._close_in_progress and not self._has_running_blocking_image_threads():
            self._finish_async_close()

    def _finish_async_close(self) -> None:
        """全スレッド終了後の最終クローズ（要件5）。"""
        self._cleanup_selection_handles()
        self._close_in_progress = False
        # _closing=True のまま close() すると closeEvent は即 accept する（再確認しない）。
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        """未保存変更を確認し、実行中スレッドがあれば非同期で閉じる（要件3・4・5・15）。

        GUIスレッドで wait() しない。スレッド実行中は一旦 ignore() して非同期クローズへ進め、
        全スレッド終了後に改めて close() する。例外が起きてもアプリ全体は終了させない。
        """
        try:
            # 非同期クローズ完了後の最終 close（確認・スレッド待ちは済んでいる）。
            if self._closing and not self._close_in_progress:
                self._cleanup_selection_handles()
                event.accept()
                return
            # 非同期クローズ待機中の再入は閉じない（終了シグナルで閉じる）。
            if self._close_in_progress:
                event.ignore()
                return
            if self.is_dirty():
                choice = self._prompt_unsaved_changes()
                if choice == "cancel":
                    # キャンセル時は閉じない。加工済み表示もそのまま維持する（要件3・13）。
                    event.ignore()
                    return
                if choice == "save":
                    if not self._persist():
                        # 保存失敗時は閉じない。
                        event.ignore()
                        return
            # 確認OK。blocking 画像加工スレッドがあれば同期 wait() せず非同期クローズへ。
            # warmup だけが残っていてもここでは待たず即閉じる（要件3・4・9・12）。
            if self._has_running_blocking_image_threads():
                event.ignore()
                self._begin_async_close()
                return
            # blocking スレッドなし → そのまま閉じる。
            self._closing = True
            self._cleanup_selection_handles()
            event.accept()
            super().closeEvent(event)
        except Exception:
            # 後始末で何が起きてもアプリ全体は終了させない（sys.exit/quit は呼ばない: 要件4）。
            _log.exception("指図書編集画面のクローズ処理でエラー")
            self._closing = True
            event.accept()

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
