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

import atexit
import base64
import copy
import hashlib
import io
import json
import logging
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QLineF,
    QMimeData,
    QPointF,
    QRectF,
    QSettings,
    QObject,
    QThread,
    QSignalBlocker,
    QSize,
    Signal,
    Slot,
    QTimer,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QDoubleValidator,
    QFontMetricsF,
    QGuiApplication,
    QDrag,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QRawFont,
    QShortcut,
    QStandardItem,
    QTextCursor,
    QTextOption,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
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
    QListWidget,
    QListWidgetItem,
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from app.text_style_resolver import TextStyle, line_height_pt

from app.config import resource_path
from app.path_utils import get_app_data_dir
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
from app.voucher_edit_objects import (
    COMMON_EDIT_KEY,
    DEFAULT_FONT_FAMILY,
    clone_edit_objects,
    edit_objects_sha256,
    load_edit_document_metadata,
    load_voucher_edit_document,
    normalize_voucher_no,
    save_voucher_edit_document,
    unique_voucher_numbers,
    voucher_key_for,
)
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
from app.window_geometry import get_display_scale, left_pane_width_for_scale
from app.voucher_templates import PAGE_H, PAGE_W

# 左ペインの基準幅（100%表示時。125%以上はDPIに応じて広げる・要件9）。
# 左ペイン基準幅。反映先ボタン・お気に入りが窮屈だったため、さらに約1.5cm（+60px）広げた
# （150→190→250）。125%以上は300px、150%以上は320px（left_pane_width_for_scale）。
LEFT_PANE_BASE_WIDTH = 250
# 前回基準幅（今回の拡張量+60pxをログで可視化するため保持）。
LEFT_PANE_PREVIOUS_BASE_WIDTH = 190
LEFT_PANE_WIDTH_INCREASE_PX = LEFT_PANE_BASE_WIDTH - LEFT_PANE_PREVIOUS_BASE_WIDTH
# 左ペイン内ボタンが縦スクロールバーに重ならないよう、内側レイアウト右に確保する
# 余白（スクロールバー幅に加算する視認用ギャップ）。
LEFT_PANE_RIGHT_GAP_PX = 8
# 左ペイン内側レイアウトの基本マージン。
LEFT_PANE_BASE_MARGIN_PX = 8

# 背景消失・選択状態の再発切り分け用ロガー（要件12）。アプリ既定ロガーへ debug 出力する。
_log = logging.getLogger("tks_to_kintone_app")
_FONT_FALLBACK_LOGGED: set[tuple[str, str]] = set()
_FONT_CACHE_LOCK = threading.RLock()
_FONT_FAMILY_CACHE: tuple[str, ...] | None = None
_BACKGROUND_THREADS: set[QThread] = set()
_BACKGROUND_RASTER_CACHE: dict[tuple[object, ...], dict[str, object]] = {}
_BACKGROUND_CACHE_LOCK = threading.RLock()


def _wait_for_editor_threads_at_exit() -> None:
    """テスト／通常終了時に実行中QThreadの破棄警告・abortを防ぐ。"""
    for thread in list(_BACKGROUND_THREADS):
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(5000)
        except RuntimeError:
            pass


atexit.register(_wait_for_editor_threads_at_exit)


def _perf_editor(phase: str, started: float, **fields: object) -> None:
    """Windows実機でも機械集計できる指図書編集の構造化性能ログ。"""
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    _log.info(
        "event=perf_voucher_editor phase=%s elapsed_ms=%s%s",
        phase, elapsed_ms, f" {suffix}" if suffix else "",
    )


def cached_font_families() -> tuple[str, ...]:
    """QFontDatabase列挙をプロセス内で一度だけ行う（呼出しは遅延操作からのみ）。"""
    global _FONT_FAMILY_CACHE
    with _FONT_CACHE_LOCK:
        if _FONT_FAMILY_CACHE is None:
            started = time.perf_counter()
            try:
                _FONT_FAMILY_CACHE = tuple(
                    str(name).strip() for name in QFontDatabase.families()
                    if str(name).strip()
                )
            except Exception:
                _FONT_FAMILY_CACHE = ()
            _perf_editor("qfontdatabase_families", started,
                         count=len(_FONT_FAMILY_CACHE))
        return _FONT_FAMILY_CACHE

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

# 編集画面だけに表示する印刷安全範囲（scene座標=pt）の固定余白。
# プリンター固有の非印字領域ではなく、レイアウト確認用の実用的な目安。
SAFE_MARGIN_LEFT = 24.0
SAFE_MARGIN_TOP = 24.0
SAFE_MARGIN_RIGHT = 24.0
SAFE_MARGIN_BOTTOM = 24.0
_GUIDE_MARK = "_print_safe_area_guide"

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
EDIT_OBJECT_MIME = "application/x-tks-voucher-edit-object"
FAVORITE_OBJECT_MIME = "application/x-tks-voucher-edit-favorite"
LEFT_PANE_ORDER_MIME = "application/x-tks-voucher-edit-list-order"
REFLECTION_TARGET_ORDER_KEY = "voucher_edit/reflection_target_order"
DEFAULT_REFLECTION_TARGET_KEY = "voucher_edit/default_reflection_target"
FALLBACK_REFLECTION_TARGET_KEY = "standard"
FAVORITE_OBJECT_ORDER_KEY = "voucher_edit/favorite_object_order"

# アンドゥ・リドゥ履歴の最大件数（要件1）。無制限に積まないようにする。
HISTORY_LIMIT = 50
# 反映先テンプレートの登録上限（要件2）。
MAX_REFLECT_TEMPLATES = 8
# お気に入りオブジェクトの登録上限（要件3）。
MAX_FAVORITE_OBJECTS = 20
# お気に入り表示枠（要件4）。1件あたりの行高さと、最大数分＋余白の固定高さ。
FAVORITE_LIST_ITEM_HEIGHT = 28
FAVORITE_LIST_FIXED_HEIGHT = FAVORITE_LIST_ITEM_HEIGHT * MAX_FAVORITE_OBJECTS + 8
# 反映先テンプレートの表示枠（要件3）。登録数に関わらず最大数8個分の縦幅を確保する。
REFLECT_LIST_ITEM_HEIGHT = 30
REFLECT_LIST_SPACING = 4
REFLECT_LIST_FIXED_HEIGHT = (
    REFLECT_LIST_ITEM_HEIGHT * MAX_REFLECT_TEMPLATES
    + REFLECT_LIST_SPACING * MAX_REFLECT_TEMPLATES
)


def _clear_edit_object_clipboard_at_exit() -> None:
    try:
        app = QApplication.instance()
        if app is None:
            return
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is not None and mime.hasFormat(EDIT_OBJECT_MIME):
            clipboard.clear()
    except Exception:
        pass


atexit.register(_clear_edit_object_clipboard_at_exit)


def _favorites_dir() -> Path:
    return get_app_data_dir() / "work" / "voucher_edit_favorites"


def _favorites_path() -> Path:
    return _favorites_dir() / "favorites.json"


def _settings_string_list(key: str) -> list[str]:
    """QSettings の QStringList/旧JSON文字列を文字列リストとして安全に読む。"""
    try:
        raw = QSettings(SETTINGS_ORG, SETTINGS_APP).value(key, [])
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                decoded = [raw] if raw else []
            raw = decoded
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(value) for value in raw if str(value).strip()]
    except Exception:
        _log.warning("voucher_edit_order_load_failed key=%s", key, exc_info=True)
        return []


def _save_settings_string_list(key: str, values: list[str]) -> bool:
    """順序を QStringList として保存する。失敗しても画面上の順序は戻さない。"""
    try:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.setValue(key, [str(value) for value in values])
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            _log.warning("voucher_edit_order_save_failed key=%s status=%s",
                         key, settings.status())
            return False
        return True
    except Exception:
        _log.warning("voucher_edit_order_save_failed key=%s", key, exc_info=True)
        return False


def _settings_string(key: str, fallback: str = "") -> str:
    """QSettingsからstable key文字列を安全に読む。"""
    try:
        return str(QSettings(SETTINGS_ORG, SETTINGS_APP).value(key, fallback) or "").strip()
    except Exception:
        _log.warning("voucher_edit_setting_load_failed key=%s", key, exc_info=True)
        return str(fallback)


def _save_settings_string(key: str, value: str) -> bool:
    """stable key文字列をQSettingsへ保存する。"""
    try:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.setValue(key, str(value))
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            _log.warning("voucher_edit_setting_save_failed key=%s status=%s",
                         key, settings.status())
            return False
        return True
    except Exception:
        _log.warning("voucher_edit_setting_save_failed key=%s", key, exc_info=True)
        return False


def _normalized_saved_order(
    saved: list[str],
    known: list[str],
    *,
    setting_key: str,
) -> list[str]:
    """未知・重複を除外し、欠落した既知IDを既定順の末尾へ補完する。"""
    known_set = set(known)
    result: list[str] = []
    invalid = False
    for value in saved:
        if value not in known_set or value in result:
            invalid = True
            continue
        result.append(value)
    missing = [value for value in known if value not in result]
    if saved and (invalid or missing):
        _log.warning(
            "voucher_edit_order_repaired key=%s unknown_or_duplicate=%s missing=%s",
            setting_key, invalid, missing,
        )
    result.extend(missing)
    return result


def load_favorite_objects() -> list[dict[str, Any]]:
    try:
        path = _favorites_path()
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    favorites: list[dict[str, Any]] = []
    migrated = False
    seen_ids: set[str] = set()
    for source_index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        fav_id = str(item.get("id") or "").strip()
        obj = item.get("object")
        if not isinstance(obj, dict):
            continue
        if not fav_id or fav_id in seen_ids:
            fav_id = str(uuid.uuid4())
            migrated = True
        seen_ids.add(fav_id)
        favorite = copy.deepcopy(item)
        favorite["id"] = fav_id
        favorite["name"] = str(item.get("name") or "お気に入り").strip() or "お気に入り"
        favorite["object"] = dict(obj)
        if "registration_order" not in favorite:
            # created_at のない旧形式では、移行時の配列順を登録順として一度だけ固定する。
            favorite["registration_order"] = source_index
            migrated = True
        favorites.append(favorite)
    order = _normalized_saved_order(
        _settings_string_list(FAVORITE_OBJECT_ORDER_KEY),
        [str(item["id"]) for item in favorites],
        setting_key=FAVORITE_OBJECT_ORDER_KEY,
    )
    by_id = {str(item["id"]): item for item in favorites}
    favorites = [by_id[fav_id] for fav_id in order]
    if migrated:
        try:
            save_favorite_objects(favorites)
        except Exception:
            _log.warning("voucher_edit_favorite_id_migration_save_failed", exc_info=True)
    return favorites


def save_favorite_objects(favorites: list[dict[str, Any]]) -> None:
    _favorites_dir().mkdir(parents=True, exist_ok=True)
    payload = [
        copy.deepcopy(item)
        for item in favorites
        if str(item.get("id") or "") and isinstance(item.get("object"), dict)
    ]
    _favorites_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class _SectionReorderListWidget(QListWidget):
    """同一ウィジェット内の stable ID だけを Move する並び替えリスト。"""

    orderChanged = Signal(list)
    dragStarted = Signal(dict)
    HANDLE_WIDTH = 26

    def __init__(self, section: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._section = section
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setAutoScroll(True)
        self.setAutoScrollMargin(24)
        self.viewport().setAcceptDrops(True)
        self._drag_token = str(uuid.uuid4())
        self._indicator_y: int | None = None
        self._handle_press_pos = None
        self._handle_press_item: QListWidgetItem | None = None

    def _log_reorder(self, phase: str, **fields: object) -> None:
        _log.info(
            "event=voucher_edit_reorder section=%s phase=%s %s",
            self._section,
            phase,
            " ".join(f"{key}={value}" for key, value in fields.items()),
        )

    def _ordered_ids(self) -> list[str]:
        return [
            str(self.item(row).data(Qt.ItemDataRole.UserRole) or "")
            for row in range(self.count())
        ]

    def _make_drag(self, item: QListWidgetItem) -> QDrag:
        stable_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        from_row = self.row(item)
        mime = QMimeData()
        payload = json.dumps(
            {
                "section": self._section,
                "stable_id": stable_id,
                "from_row": from_row,
                "token": self._drag_token,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        mime.setData(LEFT_PANE_ORDER_MIME, QByteArray(payload))
        drag = QDrag(self)
        drag.setMimeData(mime)
        return drag

    def start_reorder_drag(self, item: QListWidgetItem) -> None:
        """ハンドル操作だけが呼ぶ、source=self の明示的な並び替えQDrag。"""
        stable_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not stable_id or self.row(item) < 0:
            return
        payload = {
            "section": self._section,
            "stable_id": stable_id,
            "from_row": self.row(item),
        }
        self.setCurrentItem(item)
        self._log_reorder("start_drag", stable_id=stable_id,
                          from_row=self.row(item))
        self.dragStarted.emit(payload)
        drag = self._make_drag(item)
        result = drag.exec(
            Qt.DropAction.MoveAction,
            Qt.DropAction.MoveAction,
        )
        self._indicator_y = None
        self.viewport().update()
        if result != Qt.DropAction.MoveAction:
            self._log_reorder("cancel", stable_id=stable_id)

    def _drag_payload(self, event) -> dict[str, str] | None:
        mime = event.mimeData()
        if not mime.hasFormat(LEFT_PANE_ORDER_MIME):
            return None
        try:
            payload = json.loads(bytes(mime.data(LEFT_PANE_ORDER_MIME)).decode("utf-8"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("section") != self._section
            or payload.get("token") != self._drag_token
        ):
            return None
        return payload

    def _drop_row_and_indicator(self, position) -> tuple[int, int]:
        row = self.indexAt(position.toPoint()).row()
        if row < 0:
            if self.count():
                rect = self.visualItemRect(self.item(self.count() - 1))
                return self.count(), rect.bottom()
            return 0, 1
        rect = self.visualItemRect(self.item(row))
        if position.y() < rect.center().y():
            return row, rect.top()
        return row + 1, rect.bottom()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        payload = self._drag_payload(event)
        accepted = payload is not None
        self._log_reorder(
            "drag_enter",
            mime_section=(payload or {}).get("section", ""),
            accepted=accepted,
        )
        if accepted:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.acceptProposedAction()
        else:
            self._indicator_y = None
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        payload = self._drag_payload(event)
        accepted = payload is not None
        self._log_reorder("drag_move", accepted=accepted)
        if accepted:
            # QAbstractItemView の端スクロールタイマーを動かした後、独自MIMEを明示受理する。
            super().dragMoveEvent(event)
            _, self._indicator_y = self._drop_row_and_indicator(event.position())
            self.viewport().update()
            event.setDropAction(Qt.DropAction.MoveAction)
            event.acceptProposedAction()
        else:
            self._indicator_y = None
            self.viewport().update()
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._indicator_y = None
        self.viewport().update()
        self._log_reorder("cancel", reason="drag_leave")
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        payload = self._drag_payload(event)
        if not payload:
            self._indicator_y = None
            self.viewport().update()
            self._log_reorder("drop", accepted=False)
            event.ignore()
            return
        before = self._ordered_ids()
        stable_id = str(payload.get("stable_id") or "")
        if stable_id not in before:
            self._indicator_y = None
            self.viewport().update()
            self._log_reorder("drop", accepted=False, reason="unknown_id")
            event.ignore()
            return
        target, _ = self._drop_row_and_indicator(event.position())
        source = before.index(stable_id)
        after = list(before)
        after.pop(source)
        if source < target:
            target -= 1
        after.insert(max(0, min(target, len(after))), stable_id)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.acceptProposedAction()
        self._indicator_y = None
        self.viewport().update()
        self._log_reorder(
            "drop", from_row=source, to_row=target, accepted=True)
        if after != before:
            self.orderChanged.emit(after)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._indicator_y is None:
            return
        painter = QPainter(self.viewport())
        color = QColor("#42a5f5" if current_title_bar_is_dark() else "#1565c0")
        painter.setPen(QPen(color, 3))
        painter.drawLine(2, self._indicator_y,
                         max(2, self.viewport().width() - 2), self._indicator_y)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.position().x() <= self.HANDLE_WIDTH:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif not (event.buttons() & Qt.MouseButton.LeftButton):
            self.viewport().unsetCursor()
        super().mouseMoveEvent(event)


class _FavoriteListWidget(_SectionReorderListWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("favorites", parent)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and item is not None
            and event.position().x() <= self.HANDLE_WIDTH
        ):
            self._handle_press_pos = event.position()
            self._handle_press_item = item
            self.setCurrentItem(item)
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            self._log_reorder("mouse_press", row=self.row(item))
            event.accept()
            return
        self._handle_press_pos = None
        self._handle_press_item = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._handle_press_pos is not None
            and self._handle_press_item is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position() - self._handle_press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            item = self._handle_press_item
            self._handle_press_pos = None
            self._handle_press_item = None
            self.start_reorder_drag(item)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._handle_press_pos = None
        self._handle_press_item = None
        self.viewport().unsetCursor()
        super().mouseReleaseEvent(event)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        """本文ドラッグは従来どおりキャンバス配置専用（並び替えMIMEなし）。"""
        item = self.currentItem()
        if item is None:
            return
        fav_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not fav_id:
            return
        window = self.window()
        handler = getattr(window, "_log_favorite_event", None)
        if callable(handler):
            handler("favorite_object_drag_started", favorite_id=fav_id)
        mime = QMimeData()
        mime.setData(FAVORITE_OBJECT_MIME, QByteArray(fav_id.encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)


class _ReflectionTargetListWidget(_SectionReorderListWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("reflection_targets", parent)

    pass


class _ReorderDragHandle(QLabel):
    """setItemWidget行内の、並び替え専用ドラッグハンドル。"""

    def __init__(self, list_widget: _SectionReorderListWidget,
                 list_item: QListWidgetItem) -> None:
        super().__init__("≡")
        self._list_widget = list_widget
        self._list_item = list_item
        self._press_pos = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedWidth(_SectionReorderListWidget.HANDLE_WIDTH)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("ドラッグして並び替え")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_pos = event.position()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._list_widget._log_reorder(
            "mouse_press", row=self._list_widget.row(self._list_item))
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._press_pos = None
            self._list_widget.start_reorder_drag(self._list_item)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._press_pos = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

# リサイズ/端点ハンドルの見た目サイズとクリック判定サイズ（不具合1）。
# 見た目は小さく、クリック判定は大きくして掴みやすくする。
HANDLE_SIZE = 10.0
TEXT_HIT_TARGET_PX = 24.0
LINE_HIT_WIDTH_PX = 16.0
HANDLE_HIT_SIZE_PX = 14.0
# 既存の外部参照・テスト向け互換名。値の意味はview scale 1.0時のscene寸法。
HANDLE_HIT_SIZE = HANDLE_HIT_SIZE_PX
CLICK_SEARCH_RADIUS_PX = 10.0
TEXT_EDIT_MIN_WIDTH_PX = 80.0
TEXT_EDIT_MIN_HEIGHT_PX = 28.0
# view px を scene 単位へ変換した後の安全範囲。極端なzoomで操作領域が
# ページ全体へ広がったり、実質ゼロになったりするのを防ぐ。
HIT_SCENE_MIN = 1.0
HIT_SCENE_MAX = 72.0
RECT_TEXT_PAD = 3.0
SYMBOL_TEXT_MAX_CHARS = 3

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
SETTINGS_FAVORITE_FONTS = "voucher_edit/favorite_fonts"
TRANSPARENT_THRESHOLD_LABEL = "背景を透過\n（閾値）"

TEXT_FONT_CANDIDATES = [
    "Yu Gothic UI",
    "Meiryo",
    "MS Gothic",
]

FAVORITE_FONT_ICON_COLOR = "#F2B705"
FAVORITE_FONT_UNREGISTERED_LIGHT_COLOR = "#333333"
FAVORITE_FONT_UNREGISTERED_DARK_COLOR = "#E6E6E6"
FAVORITE_FONT_DISABLED_COLOR = "#999999"
FAVORITE_FONT_REGISTERED_DISABLED_COLOR = "#B88A00"
FAVORITE_FONT_ICON_SIZE_PX = 18
FAVORITE_FONT_BUTTON_WIDTH_PX = 24

# ツールバーのボタン幅・余白を広げ、削除=警告色/保存=安全色を割り当てる（要件2-5・2-6・2-7・3）。
# ライト/ダーク両モードで文字が読めるよう、警告色・安全色は白文字＋濃色背景にする。
EDIT_TOOLBAR_STYLE = """
QToolBar { spacing: 2px; padding: 2px; }
QToolBar QToolButton {
    border: 1px solid #666666;
    border-radius: 5px;
    padding-left: 2px;
    padding-right: 2px;
    padding-top: 2px;
    padding-bottom: 2px;
    min-height: 24px;
    margin: 0px;
    font-size: 9pt;
}
QToolBar QToolButton:hover {
    border: 1px solid #999999;
}
QToolBar QToolButton:pressed {
    border: 1px solid #2aa8ff;
    background-color: rgba(42, 168, 255, 60);
}
QToolBar QToolButton#favoriteFontButton,
QToolBar QToolButton#favoriteFontButton:hover,
QToolBar QToolButton#favoriteFontButton:pressed,
QToolBar QToolButton#favoriteFontButton:checked,
QToolBar QToolButton#favoriteFontButton:focus {
    background: transparent;
    border: none;
    padding: 0px;
    margin: 0px;
    min-width: 24px;
    max-width: 24px;
    font-size: 18px;
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
QToolBar QToolButton#shapeToolButton[shapeActive="true"] {
    background-color: #0d6efd;
    color: #ffffff;
    border: 2px solid #66b2ff;
    font-weight: bold;
}
QToolBar QToolButton#shapeToolButton::menu-indicator {
    subcontrol-position: right center;
    subcontrol-origin: padding;
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

# ダークテーマで上部メニューの文字が背景と同化して読めない問題への対策（要件6）。
# EDIT_TOOLBAR_STYLE の後段に追記して、通常/hover/disabled の文字色・背景色を
# 明示的に上書きする。checked（青）/danger/success はベース側の配色をそのまま活かす。
EDIT_TOOLBAR_DARK_STYLE = """
QToolBar#mainEditToolBar { background-color: #2b2f33; }
QToolBar QToolButton {
    color: #f0f0f0;
    background-color: #3a4047;
    border: 1px solid #8a939c;
}
QToolBar QToolButton:hover {
    background-color: #4a525a;
    border: 1px solid #b0b9c2;
}
QToolBar QToolButton:disabled {
    color: #9aa3ac;
    background-color: #33383d;
    border: 1px solid #4a525a;
}
QToolButton#favoriteFontButton[favorite="false"],
QToolButton#favoriteFontButton[favorite="false"]:hover,
QToolButton#favoriteFontButton[favorite="false"]:pressed,
QToolButton#favoriteFontButton[favorite="false"]:focus {
    color: #E6E6E6;
    background: transparent;
    border: none;
}
QToolButton#favoriteFontButton[favorite="true"],
QToolButton#favoriteFontButton[favorite="true"]:hover,
QToolButton#favoriteFontButton[favorite="true"]:pressed,
QToolButton#favoriteFontButton[favorite="true"]:focus {
    color: #F2B705;
    background: transparent;
    border: none;
}
QToolButton#favoriteFontButton[favorite="false"]:disabled {
    color: #999999;
    background: transparent;
    border: none;
}
QToolButton#favoriteFontButton[favorite="true"]:disabled {
    color: #B88A00;
    background: transparent;
    border: none;
}
QToolBar QLabel { color: #f0f0f0; }
"""

# ライトテーマ用の上部メニュー配色。以前のダークテーマ配色修正がライトモードでも
# 残って黒っぽく見える不具合への対策として、ライト時にも明示的に白系背景・濃色文字を
# 上書きする（stylesheetを空に戻すだけだと直前のダーク配色が残る場合がある）（要件6）。
# checked（青）/danger/success はベース側の配色をそのまま活かす。
EDIT_TOOLBAR_LIGHT_STYLE = """
QToolBar#mainEditToolBar { background-color: #f5f5f5; }
QToolBar QToolButton {
    color: #202124;
    background-color: #ffffff;
    border: 1px solid #c8c8c8;
}
QToolBar QToolButton:hover {
    background-color: #eef3ff;
    border: 1px solid #9db8e6;
}
QToolBar QToolButton:pressed {
    background-color: #d8e8ff;
    border: 1px solid #2aa8ff;
}
QToolBar QToolButton:disabled {
    color: #9aa0a6;
    background-color: #f0f0f0;
    border: 1px solid #d5d5d5;
}
QToolButton#favoriteFontButton[favorite="false"],
QToolButton#favoriteFontButton[favorite="false"]:hover,
QToolButton#favoriteFontButton[favorite="false"]:pressed,
QToolButton#favoriteFontButton[favorite="false"]:focus {
    color: #333333;
    background: transparent;
    border: none;
}
QToolButton#favoriteFontButton[favorite="true"],
QToolButton#favoriteFontButton[favorite="true"]:hover,
QToolButton#favoriteFontButton[favorite="true"]:pressed,
QToolButton#favoriteFontButton[favorite="true"]:focus {
    color: #F2B705;
    background: transparent;
    border: none;
}
QToolButton#favoriteFontButton[favorite="false"]:disabled {
    color: #999999;
    background: transparent;
    border: none;
}
QToolButton#favoriteFontButton[favorite="true"]:disabled {
    color: #B88A00;
    background: transparent;
    border: none;
}
QToolBar QLabel { color: #202124; }
"""

# 図形メニュー（QMenu）はトップレベルのポップアップのため、ツールバーの
# スタイルシートが効かない。ダークテーマでは個別に文字色・背景色を指定する（要件6）。
EDIT_SHAPE_MENU_DARK_STYLE = """
QMenu { background-color: #2b2f33; color: #f0f0f0; border: 1px solid #555555; }
QMenu::item:selected { background-color: #0d6efd; color: #ffffff; }
QMenu::item:checked { color: #66b2ff; }
"""

# 図形メニューのライト配色。ライトモードで黒っぽくならないよう明示指定する（要件6）。
EDIT_SHAPE_MENU_LIGHT_STYLE = """
QMenu { background-color: #ffffff; color: #202124; border: 1px solid #c8c8c8; }
QMenu::item:selected { background-color: #d8e8ff; color: #202124; }
QMenu::item:checked { color: #0d6efd; }
"""
# 上部メニューのコンテナ（QScrollArea）背景色。ライト/ダークで切り替える（要件6）。
EDIT_TOOLBAR_CONTAINER_LIGHT_BG = "#f5f5f5"
EDIT_TOOLBAR_CONTAINER_DARK_BG = "#2b2f33"

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
    # unittest.mockで差し替えられた列挙はテストごとに独立させる。本番では共有キャッシュ。
    families_callable = QFontDatabase.families
    if type(families_callable).__module__.startswith("unittest.mock"):
        try:
            available = set(families_callable())
        except Exception:
            available = set()
    else:
        available = set(cached_font_families())
    available_normalized = {_normalized_ui_font_family(name) for name in available}
    for name in cands:
        if _normalized_ui_font_family(name) in available_normalized:
            return name
    return ""


def resolve_text_font_family(family: str | None = None) -> str:
    """保存済み名は列挙せず採用する。空値の時だけ候補を遅延解決する。

    Qt自身が存在しないfamilyを安全にフォールバックするため、編集画面の生成時に
    全フォント列挙を行う必要はない。明示指定されたfamilyの存在検証はコンボを開く
    時、またはお気に入り変更時に行う。
    """
    name = (family or "").strip()
    if name:
        return name
    fallback = pick_text_font_family()
    fallback_key = (name, fallback)
    if name and fallback_key not in _FONT_FALLBACK_LOGGED:
        _FONT_FALLBACK_LOGGED.add(fallback_key)
        _log.warning("voucher_edit_text_font_fallback family=%r fallback=%r",
                     name, fallback)
    return fallback


def _normalized_ui_font_family(value: object) -> str:
    """Qt family名の全半角・大小文字・空白差を吸収する。"""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(normalized.split())


def make_text_font(font_size: float, family: str | None = None,
                   bold: bool = False, italic: bool = False,
                   underline: bool = False, strikeout: bool = False) -> QFont:
    """通常テキスト用 QFont を生成する。"""
    style = TextStyle(
        family=resolve_text_font_family(family), size_pt=float(font_size),
        bold=bool(bold), italic=bool(italic), underline=bool(underline),
        strikeout=bool(strikeout))
    font = QFont(style.family) if style.family else QFont()
    font.setPointSizeF(style.size_pt)
    font.setBold(style.bold)
    font.setItalic(style.italic)
    font.setUnderline(style.underline)
    font.setStrikeOut(style.strikeout)
    return font


_PDF_GLYPH_FALLBACK_NOTICE = (
    "選択したフォントは日本語文字に対応していないため、"
    "PDFでは代替フォントを使用します。"
)


def text_font_missing_glyphs(text: str, font: QFont) -> list[str]:
    """Qtが選択fontのprimary raw faceで描けない文字を返す。"""
    try:
        raw_font = QRawFont.fromFont(font)
        indexes = raw_font.glyphIndexesForString(text)
    except Exception:  # noqa: BLE001 - OS/Qt font backend失敗時は通知を控える
        return []
    return list(dict.fromkeys(
        char for char, glyph in zip(text, indexes)
        if not char.isspace() and int(glyph) == 0
    ))


def _update_text_glyph_fallback_tooltip(item: QGraphicsItem, text: str,
                                        font: QFont) -> None:
    missing = text_font_missing_glyphs(text, font)
    if missing:
        item.setToolTip(_PDF_GLYPH_FALLBACK_NOTICE)
        _log.info(
            "event=voucher_edit_font_glyph_fallback_notice object_id=%s "
            "font_family=%r missing_glyphs=%r fallback_reason=missing_glyphs",
            getattr(item, "obj_id", ""), font.family(), missing,
        )
    elif item.toolTip() == _PDF_GLYPH_FALLBACK_NOTICE:
        item.setToolTip("")


def _available_font_families() -> set[str]:
    """OSで利用できるフォントファミリーを返す。取得失敗時は空集合。"""
    try:
        return set(cached_font_families())
    except Exception:  # noqa: BLE001 - フォント列挙失敗で編集画面を止めない
        return set()


def save_favorite_fonts(families: list[str]) -> bool:
    """お気に入りを登録順のQStringListとして保存する。失敗は警告だけに留める。"""
    normalized: list[str] = []
    for family in families:
        name = str(family or "").strip()
        if name and name not in normalized:
            normalized.append(name)
    try:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        settings.setValue(SETTINGS_FAVORITE_FONTS, normalized)
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            _log.warning("voucher_edit_favorite_fonts_save_failed status=%s",
                         settings.status())
            return False
    except Exception:  # noqa: BLE001 - 設定保存失敗で編集操作を止めない
        _log.warning("voucher_edit_favorite_fonts_save_failed", exc_info=True)
        return False
    return True


def load_favorite_fonts(*, available_fonts: set[str] | None = None) -> list[str]:
    """お気に入りを安全に読み込み、存在しないフォントと壊れた値を整理する。"""
    try:
        settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        raw = settings.value(SETTINGS_FAVORITE_FONTS, [])
    except Exception:  # noqa: BLE001 - 設定読み込み失敗時は空一覧
        _log.warning("voucher_edit_favorite_fonts_load_failed", exc_info=True)
        return []

    values: list[object]
    if raw is None or raw == "":
        values = []
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    elif isinstance(raw, str):
        # QStringListが1件だけの場合、環境によって文字列として返る。
        # JSON配列文字列も旧設定・手動編集への耐性として受け入れる。
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                values = list(parsed) if isinstance(parsed, list) else []
            except (TypeError, ValueError, json.JSONDecodeError):
                values = []
        else:
            values = [stripped] if stripped else []
    else:
        values = []

    # 起動時は存在確認のための全フォント列挙をしない。明示的に集合が渡された
    # 設定整理操作だけで欠落フォントを除去する。
    installed = available_fonts
    favorites: list[str] = []
    cleaned = not isinstance(raw, (list, tuple, str)) and raw not in (None, "")
    for value in values:
        if not isinstance(value, str):
            cleaned = True
            continue
        family = value.strip()
        if not family or family in favorites:
            cleaned = True
            continue
        if installed is not None and family not in installed:
            _log.warning("voucher_edit_favorite_font_missing family=%r", family)
            cleaned = True
            continue
        favorites.append(family)
    if cleaned:
        save_favorite_fonts(favorites)
    _log.info("voucher_edit_favorite_fonts_loaded count=%d", len(favorites))
    return favorites


FONT_SECTION_ROLE = int(Qt.ItemDataRole.UserRole) + 1
FONT_SECTION_FAVORITE = "favorite"
FONT_SECTION_ALL = "all"


class _FontFamilyComboBox(QComboBox):
    """お気に入りと全フォントを1つの見出し付き一覧で表示するコンボ。"""

    currentFontChanged = Signal(QFont)

    def __init__(self, favorites: list[str], current_family: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("textFontFamilyCombo")
        self.setToolTip("フォント名")
        self.setMinimumWidth(150)
        self.setMaximumWidth(210)
        self._all_fonts_loaded = False
        self.currentIndexChanged.connect(self._emit_current_font_changed)
        self.rebuild(favorites, current_family=current_family)

    @staticmethod
    def _header(text: str) -> QStandardItem:
        item = QStandardItem(text)
        item.setSelectable(False)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        return item

    def _append_font(self, family: str, section: str) -> None:
        self.addItem(family, family)
        index = self.count() - 1
        self.setItemData(index, QFont(family), Qt.ItemDataRole.FontRole)
        self.setItemData(index, section, FONT_SECTION_ROLE)

    def rebuild(self, favorites: list[str], *, current_family: str = "") -> None:
        """軽量な初期候補を構築する。全一覧はshowPopupまで取得しない。"""
        family = str(current_family or self.currentFont().family() or "").strip()
        all_fonts = list(cached_font_families()) if self._all_fonts_loaded else []
        valid_favorites = list(dict.fromkeys(
            name for name in favorites if str(name).strip()
        ))
        with QSignalBlocker(self):
            self.clear()
            model = self.model()
            if valid_favorites:
                model.appendRow(self._header("★ お気に入り"))
                for name in valid_favorites:
                    self._append_font(name, FONT_SECTION_FAVORITE)
                self.insertSeparator(self.count())
            model.appendRow(self._header("すべてのフォント"))
            initial = all_fonts or ([family] if family and family not in valid_favorites else [])
            for name in initial:
                self._append_font(name, FONT_SECTION_ALL)
            self._set_family_index(family)

    def _ensure_all_fonts_loaded(self) -> None:
        if self._all_fonts_loaded:
            return
        family = self.currentFont().family()
        favorites = [
            str(self.itemData(index) or "")
            for index in range(self.count())
            if self.itemData(index, FONT_SECTION_ROLE) == FONT_SECTION_FAVORITE
        ]
        self._all_fonts_loaded = True
        self.rebuild(favorites, current_family=family)

    def showPopup(self) -> None:  # noqa: N802
        self._ensure_all_fonts_loaded()
        super().showPopup()

    def _set_family_index(self, family: str) -> None:
        target = str(family or "").strip()
        index = self.findData(target) if target else -1
        self.setCurrentIndex(index)

    def setCurrentFont(self, font: QFont) -> None:  # noqa: N802 - QFontComboBox互換
        if self.findData(font.family()) < 0:
            self._ensure_all_fonts_loaded()
        self._set_family_index(font.family())

    def currentFont(self) -> QFont:  # noqa: N802 - QFontComboBox互換
        family = str(self.currentData() or "").strip()
        return QFont(family) if family else QFont()

    def _emit_current_font_changed(self, index: int) -> None:
        family = str(self.itemData(index) or "").strip() if index >= 0 else ""
        if family:
            self.currentFontChanged.emit(QFont(family))


class _FontSizeComboBox(QComboBox):
    """候補選択と4〜200ptの直接入力を両立する文字サイズコンボ。"""

    valueChanged = Signal(float)
    CANDIDATES = (6, 7, 8, 9, 10, 11, 12, 14, 16, 18, 20, 22, 24, 28,
                  32, 36, 48, 72)
    MINIMUM = 4.0
    MAXIMUM = 200.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("textFontSizeCombo")
        self.setEditable(True)
        self.addItems([str(value) for value in self.CANDIDATES])
        self.lineEdit().setValidator(QDoubleValidator(
            self.MINIMUM, self.MAXIMUM, 1, self))
        self.setMinimumWidth(64)
        self._last_valid = DEFAULT_FONT_SIZE
        self.activated.connect(lambda _index: self._commit_text())
        self.lineEdit().editingFinished.connect(self._commit_text)

    def value(self) -> float:
        try:
            value = float(self.currentText().strip())
        except (TypeError, ValueError):
            return self._last_valid
        return value if self.MINIMUM <= value <= self.MAXIMUM else self._last_valid

    def setValue(self, value: float) -> None:  # noqa: N802 - QSpinBox互換API
        normalized = min(self.MAXIMUM, max(self.MINIMUM, float(value)))
        self._last_valid = normalized
        text = f"{normalized:g}"
        if self.currentText() != text:
            self.setEditText(text)
        if not self.signalsBlocked():
            self.valueChanged.emit(normalized)

    def _commit_text(self) -> None:
        try:
            value = float(self.currentText().strip())
        except (TypeError, ValueError):
            value = self._last_valid
        if not self.MINIMUM <= value <= self.MAXIMUM:
            value = self._last_valid
        self.setValue(value)


def _color_name(color: str | QColor | None, default: str = "#000000") -> str:
    qcolor = QColor(color or default)
    return qcolor.name() if qcolor.isValid() else default


def _configure_text_document(item: QGraphicsTextItem) -> None:
    item.document().setDocumentMargin(0)
    item.document().setDefaultStyleSheet("p { margin: 0; line-height: 120%; }")
    option = item.document().defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.NoWrap)
    item.document().setDefaultTextOption(option)


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
    line_h = line_height_pt(font_size)
    return float(width), float(len(lines) * line_h)


TEXT_DRAG_THRESHOLD = 5.0
TEXT_BOX_PADDING = 3.0
def scene_units_for_view_pixels(scene: QGraphicsScene | None, pixels: float,
                                *, minimum: float = HIT_SCENE_MIN,
                                maximum: float = HIT_SCENE_MAX) -> float:
    """view上のpxをscene単位へ変換し、極端なzoomに対してclampする。"""
    scale = 1.0
    if scene is not None and scene.views():
        transform = scene.views()[0].transform()
        scale = abs(float(transform.m11())) or 1.0
    return max(float(minimum), min(float(maximum), float(pixels) / scale))


def fit_font_size_to_text_box(
    text: str, width: float, height: float, *,
    family: str | None = None, bold: bool = False, italic: bool = False,
    underline: bool = False, strikeout: bool = False,
    padding: float = TEXT_BOX_PADDING,
) -> float:
    """実際のQFontを計測し、矩形へ収まる最大ptを0.1pt単位で返す。"""
    available_w = max(float(width) - padding * 2.0, 0.1)
    available_h = max(float(height) - padding * 2.0, 0.1)
    sample = str(text or "テキスト")

    def fits(size: float) -> bool:
        font = make_text_font(size, family, bold, italic, underline, strikeout)
        metrics = QFontMetricsF(font)
        lines = sample.splitlines() or [""]
        # boundingRectはitalic/boldの左右への張り出しも含む。
        measured_w = max(
            (metrics.boundingRect(line or " ").width() for line in lines), default=0.0)
        # 保存される論理サイズはPDF側と同じpt行送りにする。QtのDPI依存
        # lineSpacing()は画面表示用の補助値として幅の計測には使わない。
        measured_h = line_height_pt(size) * max(len(lines), 1)
        return measured_w <= available_w and measured_h <= available_h

    low, high = 4.0, 200.0
    if not fits(low):
        return low
    for _ in range(16):
        mid = (low + high) / 2.0
        if fits(mid):
            low = mid
        else:
            high = mid
    return max(4.0, min(200.0, round(low, 1)))


def is_symbol_text_candidate(text: str) -> bool:
    """短い単独注記を symbol_text として扱うか判定する。"""
    stripped = text.strip()
    return bool(stripped) and "\n" not in stripped and "\r" not in stripped and len(stripped) <= SYMBOL_TEXT_MAX_CHARS


def _scene_rect_from_item_rect(item: QGraphicsItem, rect: QRectF) -> QRectF:
    mapped = item.mapRectToScene(rect)
    return mapped.boundingRect() if hasattr(mapped, "boundingRect") else mapped


def make_themed_svg_icon(rel_path: str, dark: bool) -> QIcon:
    """assets配下のSVGを読み込み、テーマに応じた色で塗り直したQIconを返す（要件1）。

    アイコンは外部フォント等に依存せず、同梱SVGの輪郭シルエットを
    ライト時は濃いグレー、ダーク時は明るいグレーで塗る。disabled表示は
    QIconが自動生成する淡色版に任せる（＝押せないことが分かる表示）。
    """
    base = QIcon(str(resource_path(rel_path)))
    size = QSize(20, 20)
    pixmap = base.pixmap(size)
    if pixmap.isNull():
        return base
    color = QColor("#e6e6e6") if dark else QColor("#333333")
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), color)
    painter.end()
    return QIcon(tinted)


def _undo_redo_colors(dark: bool) -> tuple[QColor, QColor]:
    """アンドゥ/リドゥの enabled/disabled 色を返す（要件8・見やすさ）。

    ライト/ダークどちらでも、有効時は背景と同化しない高コントラスト色、無効時は
    薄いが背景と同化しないグレーにする。OS標準アイコンやUnicodeに依存しない。
    """
    if dark:
        # ダーク背景: 有効=白系、無効=中間グレー（背景と同化させない）。
        return QColor("#f5f5f5"), QColor("#8a8f98")
    # ライト背景: 有効=濃色、無効=グレー。
    return QColor("#1f2733"), QColor("#9aa0a6")


def _draw_undo_redo_fallback_pixmap(
    kind: str, dark: bool, size: int = 24, color: QColor | None = None
) -> QPixmap:
    """SVG読み込みに失敗した端末向けに、曲線矢印のUndo/Redoアイコンを直接描画する。

    フォント/テーマ/標準アイコンに一切依存せず、QPainter だけで曲線＋矢頭を描く。
    kind="undo" は左向き、"redo" は右向き（水平反転）。color 指定時はその色で描く
    （enabled/disabled で色を変えるため・要件8）。
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if kind == "redo":
        # 水平反転して右向きにする。
        painter.translate(size, 0)
        painter.scale(-1, 1)
    if color is None:
        color = QColor("#e6e6e6") if dark else QColor("#333333")
    pen = QPen(color)
    pen.setWidthF(max(2.0, size * 0.09))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    margin = size * 0.18
    arc_rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    # 上部が開いた円弧（左上に開口部を作り、そこへ矢頭を付ける）。
    painter.drawArc(arc_rect, 110 * 16, 250 * 16)
    # 開口部（円弧の始点付近）に矢頭を描く。
    cx = arc_rect.center().x()
    cy = arc_rect.center().y()
    radius = arc_rect.width() / 2.0
    import math

    start_angle = math.radians(110)
    tip_x = cx + radius * math.cos(start_angle)
    tip_y = cy - radius * math.sin(start_angle)
    head = size * 0.22
    painter.setBrush(color)
    arrow = QPainterPath()
    arrow.moveTo(tip_x, tip_y)
    arrow.lineTo(tip_x - head, tip_y - head * 0.2)
    arrow.lineTo(tip_x - head * 0.2, tip_y + head)
    arrow.closeSubpath()
    painter.drawPath(arrow)
    painter.end()
    return pixmap


def _tint_svg_pixmap(rel_path: str, color: QColor, size: QSize) -> QPixmap:
    """同梱SVGを指定色で塗り直した pixmap を返す（読めなければ null pixmap）。"""
    try:
        base = QIcon(str(resource_path(rel_path)))
        pixmap = base.pixmap(size)
    except Exception:  # noqa: BLE001
        return QPixmap()
    if pixmap.isNull():
        return QPixmap()
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(tinted.rect(), color)
    painter.end()
    return tinted


def make_undo_redo_icon(kind: str, dark: bool) -> tuple[QIcon, bool]:
    """Undo/Redo用アイコンを返す（同梱SVG優先、失敗時は描画フォールバック・要件4/8）。

    enabled(Normal) と disabled(Disabled) で明示的に色を分けた QIcon を返す。
    ライト/ダーク両テーマで背景と同化しない高コントラスト色（有効）と、薄いが
    見えるグレー（無効）を割り当てる。戻り値: (QIcon, used_fallback)。
    """
    rel_path = "assets/undo.svg" if kind == "undo" else "assets/redo.svg"
    enabled_color, disabled_color = _undo_redo_colors(dark)
    size = QSize(20, 20)
    enabled_pixmap = _tint_svg_pixmap(rel_path, enabled_color, size)
    used_fallback = False
    if enabled_pixmap.isNull():
        # SVGが読めない端末: 状態別に色を変えて直接描画する（テキストにはしない）。
        used_fallback = True
        enabled_pixmap = _draw_undo_redo_fallback_pixmap(kind, dark, color=enabled_color)
        disabled_pixmap = _draw_undo_redo_fallback_pixmap(kind, dark, color=disabled_color)
    else:
        disabled_pixmap = _tint_svg_pixmap(rel_path, disabled_color, size)
    icon = QIcon()
    icon.addPixmap(enabled_pixmap, QIcon.Mode.Normal)
    icon.addPixmap(enabled_pixmap, QIcon.Mode.Active)
    if not disabled_pixmap.isNull():
        icon.addPixmap(disabled_pixmap, QIcon.Mode.Disabled)
    return icon, used_fallback


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


def pdf_page_size(pdf_bytes: bytes) -> tuple[float, float] | None:
    """背景PDF先頭ページのポイント寸法を返す。"""
    if not pdf_bytes:
        return None
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[0]
        return float(page.rect.width), float(page.rect.height)
    except Exception:
        return None


def rasterize_order_sheet_background(pdf_bytes: bytes, zoom: float = 2.0) -> dict[str, object]:
    """GUIオブジェクトを作らずPDFをPNG bytesへ変換するworker用関数。"""
    from app.voucher_service import PDF_TEXT_RENDERER_REVISION
    from app.processing_display_names import processing_display_names_revision
    started = time.perf_counter()
    cache_key = (
        PDF_TEXT_RENDERER_REVISION, hashlib.sha256(pdf_bytes).hexdigest(),
        processing_display_names_revision(),
        int(round(zoom * 1000)),
    )
    with _BACKGROUND_CACHE_LOCK:
        cached = _BACKGROUND_RASTER_CACHE.get(cache_key)
        if cached is not None:
            _perf_editor("background_cache_hit", started)
            return dict(cached)
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        result = {
            "png_bytes": pix.tobytes("png"),
            "page_w": float(page.rect.width),
            "page_h": float(page.rect.height),
        }
    _perf_editor("background_pdf_rasterize", started,
                 bytes=len(result["png_bytes"]))
    with _BACKGROUND_CACHE_LOCK:
        _BACKGROUND_RASTER_CACHE[cache_key] = dict(result)
    return result


class _BackgroundRasterWorker(QObject):
    ready = Signal(int, str, object)
    failed = Signal(int, str, str)

    def __init__(self, generation: int, voucher_key: str, pdf_bytes: bytes) -> None:
        super().__init__()
        self.generation = generation
        self.voucher_key = voucher_key
        self.pdf_bytes = bytes(pdf_bytes)

    @Slot()
    def run(self) -> None:
        try:
            result = rasterize_order_sheet_background(self.pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.generation, self.voucher_key, str(exc))
            return
        self.ready.emit(self.generation, self.voucher_key, result)


class _EditPreviewWorker(QObject):
    ready = Signal(int, bytes)
    failed = Signal(int, str)
    finished = Signal()

    def __init__(self, generation: int, base_pdf: bytes,
                 objects: list[dict[str, Any]], *,
                 voucher_ids: list[str] | None = None,
                 print_data: dict[str, Any] | None = None,
                 trace_id: str = "") -> None:
        super().__init__()
        self.generation = generation
        self.base_pdf = bytes(base_pdf)
        self.objects = copy.deepcopy(objects)
        self.voucher_ids = list(voucher_ids or [])
        self.print_data = copy.deepcopy(print_data)
        self.trace_id = str(trace_id or "")
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            if self.print_data is None or not self.voucher_ids:
                raise RuntimeError("共通プレビュー用データがありません。")
            from app.voucher_preview_controller import build_voucher_preview_pdf
            result = build_voucher_preview_pdf(
                self.voucher_ids,
                self.print_data,
                edit_render_trace_id=self.trace_id,
                reload_edit_objects=False,
            )
            if not result:
                raise RuntimeError("PDFプレビューの生成結果が空です。")
            if not self._cancelled:
                self.ready.emit(self.generation, result)
        except Exception as exc:  # noqa: BLE001
            _log.exception(
                "指図書プレビューworkerでPDF生成に失敗 generation=%s",
                self.generation,
            )
            self.failed.emit(self.generation, str(exc))
        finally:
            self.finished.emit()


# ── 編集アイテム ──────────────────────────────────────────────────────────────

class _EditTextItem(QGraphicsTextItem):
    """ドラッグ矩形で作成するテキストボックス。ダブルクリックで文字編集。"""

    def __init__(self, text: str = "", obj_id: str | None = None,
                 font_size: float = DEFAULT_FONT_SIZE,
                 box_w: float = MIN_TEXT_W, box_h: float = MIN_TEXT_H,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 font_underline: bool = False, font_strikeout: bool = False,
                 text_color: str = DEFAULT_TEXT_COLOR,
                 line_width: float = DEFAULT_LINE_WIDTH,
                 stroke_color: str = DEFAULT_STROKE_COLOR,
                 fill_color: str | None = None,
                 text_align: str = "left",
                 vertical_align: str = "top",
                 auto_fit: bool = True,
                 manual_resized: bool = False,
                 auto_fit_to_box: bool = False,
                 target_vouchers: list[str] | None = None) -> None:
        super().__init__(text)
        self.obj_id = obj_id or str(uuid.uuid4())
        self.target_vouchers = _normalize_target_vouchers(target_vouchers)
        self.font_family = resolve_text_font_family(font_family)
        self.font_size = float(font_size)
        self.font_bold = bool(font_bold)
        self.font_italic = bool(font_italic)
        self.font_underline = bool(font_underline)
        self.font_strikeout = bool(font_strikeout)
        self.text_color = _color_name(text_color)
        self.line_width = float(line_width)
        self.stroke_color = _color_name(stroke_color)
        self.fill_color = fill_color
        self.text_align = _normalize_text_align(text_align)
        self.vertical_align = _normalize_vertical_align(vertical_align)
        self.auto_fit = bool(auto_fit)
        self.manual_resized = bool(manual_resized)
        self.auto_fit_to_box = bool(auto_fit_to_box)
        self.box_w = float(box_w)
        self.box_h = float(box_h)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic,
                                    self.font_underline, self.font_strikeout))
        _update_text_glyph_fallback_tooltip(self, self.toPlainText(), self.font())
        _configure_text_document(self)
        _apply_text_alignment(self, self.text_align)
        self.setDefaultTextColor(QColor(self.text_color))
        self.setTextWidth(self.box_w)
        self.document().contentsChanged.connect(self._refresh_text_layout)
        self.setData(_DATA_TYPE, "text")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.fit_to_text_if_needed(force=True, reason="initial_create")

    def apply_font_size(self, font_size: float) -> None:
        self.font_size = float(font_size)
        self.auto_fit_to_box = False
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic,
                                    self.font_underline, self.font_strikeout))
        self.fit_to_text_if_needed(force=True, reason="font_size_change")
        self._refresh_text_layout()

    def apply_text_style(self, *, family: str | None = None,
                         font_size: float | None = None,
                         bold: bool | None = None,
                         italic: bool | None = None,
                         underline: bool | None = None,
                         strikeout: bool | None = None) -> None:
        if family is not None:
            self.font_family = resolve_text_font_family(family)
        if font_size is not None:
            self.font_size = float(font_size)
            self.auto_fit_to_box = False
        if bold is not None:
            self.font_bold = bool(bold)
        if italic is not None:
            self.font_italic = bool(italic)
        if underline is not None:
            self.font_underline = bool(underline)
        if strikeout is not None:
            self.font_strikeout = bool(strikeout)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic,
                                    self.font_underline, self.font_strikeout))
        self.fit_to_text_if_needed(force=True, reason="style_change")
        self._refresh_text_layout()

    def fit_to_text_if_needed(self, force: bool = False,
                              reason: str = "content_change") -> None:
        if reason == "manual_resize":
            return
        if not force and not self.auto_fit and reason == "serialize":
            return
        if not self.toPlainText():
            return
        text_w, text_h = _text_content_size(self.toPlainText(), self.font(), self.font_size)
        # NoWrap means this width is the logical width of the longest explicit
        # line; never derive a smaller font size from the current frame.
        new_w = max(text_w + TEXT_BOX_PADDING * 2.0, MIN_TEXT_W)
        new_h = max(text_h + TEXT_BOX_PADDING * 2.0, self.font_size * 1.2,
                    MIN_TEXT_H)
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
        _update_text_glyph_fallback_tooltip(self, self.toPlainText(), self.font())
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
        scene = self.scene()
        window = getattr(scene, "_window", None) if scene is not None else None
        if window is not None and hasattr(window, "begin_text_edit"):
            window.begin_text_edit(self)
        else:
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if (event.key() == Qt.Key.Key_Escape
                and self.textInteractionFlags()
                != Qt.TextInteractionFlag.NoTextInteraction):
            original = getattr(self, "_inline_edit_original_text", self.toPlainText())
            self.setPlainText(str(original))
            self._inline_edit_cancelled = True
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # 編集を抜けたら通常選択モードへ戻し、勝手に入力状態にならないようにする。
        if hasattr(self, "_inline_edit_text_width"):
            self.setTextWidth(float(self._inline_edit_text_width))
            del self._inline_edit_text_width
        if hasattr(self, "_inline_edit_min_rect"):
            del self._inline_edit_min_rect
        self.prepareGeometryChange()
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        super().focusOutEvent(event)
        if self.auto_fit_to_box and self.toPlainText().strip():
            self.refit_font_to_box()
        elif (not self.manual_resized
              or self.toPlainText() != str(
                  getattr(self, "_inline_edit_original_text", self.toPlainText()))):
            self.fit_to_text_if_needed(reason="content_change")
        scene = self.scene()
        if scene is not None and hasattr(scene, "_window"):
            window = scene._window
            # 空文字（空白のみ）の単独テキストボックスは残さない（要件3）。
            if not self.toPlainText().strip():
                window.remove_text_item(self)
            elif window.maybe_convert_text_item_to_symbol(self):
                return
            changed = (
                not bool(getattr(self, "_inline_edit_cancelled", False))
                and self.toPlainText()
                != str(getattr(self, "_inline_edit_original_text", self.toPlainText()))
            )
            if changed:
                window.commit_history()
                window.mark_dirty()
        self._inline_edit_cancelled = False
        self._inline_edit_original_text = self.toPlainText()

    def refit_font_to_box(self) -> None:
        self.font_size = fit_font_size_to_text_box(
            self.toPlainText(), self.box_w, self.box_h,
            family=self.font_family, bold=self.font_bold,
            italic=self.font_italic, underline=self.font_underline,
            strikeout=self.font_strikeout,
        )
        self.setFont(make_text_font(
            self.font_size, self.font_family, self.font_bold, self.font_italic,
            self.font_underline, self.font_strikeout))
        self._refresh_text_layout()

    def shape(self) -> QPainterPath:  # noqa: N802
        """保存矩形の余白と小さい文字の拡張領域を共通のhit対象にする。"""
        path = QPainterPath()
        actual = super().boundingRect().translated(
            0.0, self._vertical_text_offset())
        minimum = scene_units_for_view_pixels(
            self.scene(), TEXT_HIT_TARGET_PX)
        hit_rect = QRectF(actual)
        hit_rect.setWidth(max(actual.width(), minimum))
        hit_rect.setHeight(max(actual.height(), minimum))
        hit_rect.moveCenter(actual.center())
        # manual resize等で実glyphより選択枠が大きい場合も、枠内の余白から編集できる。
        hit_rect = hit_rect.united(QRectF(0.0, 0.0, self.box_w, self.box_h))
        temporary = getattr(self, "_inline_edit_min_rect", None)
        if isinstance(temporary, QRectF):
            hit_rect = hit_rect.united(temporary)
        path.addRect(hit_rect)
        return path

    def serialize_edit_object(self) -> dict[str, Any]:
        self.fit_to_text_if_needed(reason="serialize")
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
            "bold": self.font_bold,
            "font_italic": self.font_italic,
            "font_underline": self.font_underline,
            "font_strikeout": self.font_strikeout,
            "italic": self.font_italic,
            "underline": self.font_underline,
            "strikeout": self.font_strikeout,
            "text_color": self.text_color,
            "text_align": self.text_align,
            "vertical_align": self.vertical_align,
            "line_width": self.line_width,
            "stroke_color": self.stroke_color,
            "fill_color": self.fill_color,
            "auto_fit": self.auto_fit,
            "manual_resized": self.manual_resized,
            "text_box_width": w,
            "text_box_height": h,
            "auto_fit_to_box": self.auto_fit_to_box,
            "target_vouchers": list(self.target_vouchers),
            "color": list(DEFAULT_COLOR),
        }


class _EditSymbolTextItem(QGraphicsSimpleTextItem):
    """短い注記用の点アンカーテキスト。scene座標の中心点を保存する。"""

    def __init__(self, text: str = "", obj_id: str | None = None,
                 font_size: float = DEFAULT_FONT_SIZE,
                 font_family: str | None = None,
                 font_bold: bool = False, font_italic: bool = False,
                 font_underline: bool = False, font_strikeout: bool = False,
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
        self.font_underline = bool(font_underline)
        self.font_strikeout = bool(font_strikeout)
        self.text_color = _color_name(text_color)
        self.anchor = anchor if anchor == "center" else "center"
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic,
                                    self.font_underline, self.font_strikeout))
        _update_text_glyph_fallback_tooltip(self, self.text(), self.font())
        self.setBrush(QBrush(QColor(self.text_color)))
        self.setData(_DATA_TYPE, "symbol_text")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

    def apply_font_size(self, font_size: float) -> None:
        center = self.anchor_scene_pos()
        self.font_size = float(font_size)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic,
                                    self.font_underline, self.font_strikeout))
        _update_text_glyph_fallback_tooltip(self, self.text(), self.font())
        self.set_anchor_scene_pos(center)

    def apply_text_style(self, *, family: str | None = None,
                         font_size: float | None = None,
                         bold: bool | None = None,
                         italic: bool | None = None,
                         underline: bool | None = None,
                         strikeout: bool | None = None) -> None:
        center = self.anchor_scene_pos()
        if family is not None:
            self.font_family = resolve_text_font_family(family)
        if font_size is not None:
            self.font_size = float(font_size)
        if bold is not None:
            self.font_bold = bool(bold)
        if italic is not None:
            self.font_italic = bool(italic)
        if underline is not None:
            self.font_underline = bool(underline)
        if strikeout is not None:
            self.font_strikeout = bool(strikeout)
        self.setFont(make_text_font(self.font_size, self.font_family,
                                    self.font_bold, self.font_italic,
                                    self.font_underline, self.font_strikeout))
        _update_text_glyph_fallback_tooltip(self, self.text(), self.font())
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
            "bold": self.font_bold,
            "font_italic": self.font_italic,
            "font_underline": self.font_underline,
            "font_strikeout": self.font_strikeout,
            "text_color": self.text_color,
            "anchor": self.anchor,
            "italic": self.font_italic,
            "underline": self.font_underline,
            "strikeout": self.font_strikeout,
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
                         font_underline: bool = False,
                         font_strikeout: bool = False,
                         text_color: str = DEFAULT_TEXT_COLOR,
                         text_align: str = "center",
                         vertical_align: str = "middle") -> None:
        self.font_family = resolve_text_font_family(font_family)
        self.font_size = float(font_size)
        self.font_bold = bool(font_bold)
        self.font_italic = bool(font_italic)
        self.font_underline = bool(font_underline)
        self.font_strikeout = bool(font_strikeout)
        self.text_color = _color_name(text_color)
        self.text_align = _normalize_text_align(text_align)
        self.vertical_align = _normalize_vertical_align(vertical_align)
        self._text = _ShapeInnerText(text, self)  # type: ignore[arg-type]
        self._text.setFont(make_text_font(self.font_size, self.font_family,
                                          self.font_bold, self.font_italic,
                                          self.font_underline, self.font_strikeout))
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
                                          self.font_bold, self.font_italic,
                                          self.font_underline, self.font_strikeout))
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
                 font_underline: bool = False, font_strikeout: bool = False,
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
                              font_italic, font_underline, font_strikeout,
                              text_color, text_align,
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
            "font_underline": self.font_underline,
            "font_strikeout": self.font_strikeout,
            "bold": self.font_bold,
            "italic": self.font_italic,
            "underline": self.font_underline,
            "strikeout": self.font_strikeout,
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
                 font_underline: bool = False, font_strikeout: bool = False,
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
                              font_italic, font_underline, font_strikeout,
                              text_color, text_align,
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
            "font_underline": self.font_underline,
            "font_strikeout": self.font_strikeout,
            "bold": self.font_bold,
            "italic": self.font_italic,
            "underline": self.font_underline,
            "strikeout": self.font_strikeout,
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

    def actual_line_path(self) -> QPainterPath:
        """装飾を含む実描画線のlocal path（描画・保存値は変更しない）。"""
        path = QPainterPath()
        ln = self.line()
        for x1, y1, x2, y2 in line_segments(
                self.line_type, ln.x1(), ln.y1(), ln.x2(), ln.y2()):
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)
        return path

    def shape(self) -> QPainterPath:  # noqa: N802
        """描画penとは独立した、view上16pxの透明な操作領域。"""
        hit_width = scene_units_for_view_pixels(
            self.scene(), LINE_HIT_WIDTH_PX)
        stroker = QPainterPathStroker()
        stroker.setWidth(max(float(self.line_width), hit_width))
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return stroker.createStroke(self.actual_line_path())

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
        # 画像加工（二値化／背景を透過（閾値））前の元画像。最初の加工時に
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
        """加工済みバイト列で画像を差し替える（二値化／閾値透過 共通）。

        位置・サイズ・倍率は box_w/box_h 維持により不変。元画像退避は「成功時だけ」
        確定する（失敗時に退避が残らないようにする: 要件6・7）。複数回どの加工をしても
        最初の元画像へ戻せるよう、退避は一度だけ行う（要件10・11）。
        """
        if self._original_image_bytes is None:
            self._original_image_bytes = bytes(self.image_bytes)
        self._replace_image_bytes(processed_bytes)

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
    target = getattr(handle, "_target", None)
    if isinstance(target, _EditTextItem):
        window._log_edit_event(
            "voucher_text_resize_commit",
            object_id=target.obj_id,
            object_type=type(target).__name__,
            handle=getattr(handle, "_position", ""),
            font_size_before=getattr(handle, "_font_size_before",
                                     target.font_size),
            font_size_after=target.font_size)
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
        # helper解決と診断のどの経路でも、現在のgraphics itemを参照する。
        self.owner_item = target
        self.source_item = target
        self._position = position if position in self.CORNERS | self.EDGES else "bottom_right"
        self._suppress = False
        # リサイズ中フラグと、対象が元々移動可能だったか（解放時に戻すため）。
        self._resizing = False
        self._target_was_movable = True
        self._resize_start_rect: QRectF | None = None
        self._font_size_before: float | None = None
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
        h = scene_units_for_view_pixels(self.scene(), HANDLE_HIT_SIZE_PX)
        return QRectF(-h / 2, -h / 2, h, h)

    def shape(self) -> QPainterPath:  # noqa: N802
        # クリック判定を拡大し、画像本体よりハンドルを優先で掴めるようにする。
        path = QPainterPath()
        h = scene_units_for_view_pixels(self.scene(), HANDLE_HIT_SIZE_PX)
        path.addRect(QRectF(-h / 2, -h / 2, h, h))
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
        current = self._resize_start_rect or self._target_rect()
        new_rect = self._resized_rect(current, handle_scene_pos)
        if isinstance(tgt, (_EditRectItem, _EditEllipseItem)):
            local_tl = tgt.mapFromScene(new_rect.topLeft())
            tgt.setRect(QRectF(local_tl.x(), local_tl.y(),
                               new_rect.width(), new_rect.height()))
        elif isinstance(tgt, _EditTextItem):
            tgt.setPos(new_rect.topLeft())
            tgt.set_manual_box_size(new_rect.width(), new_rect.height())
            scene = self.scene()
            window = getattr(scene, "_window", None) if scene is not None else None
            if window is not None:
                window._log_edit_event(
                    "voucher_text_resize_move",
                    object_id=tgt.obj_id,
                    handle=self._position,
                    scale=1.0,
                    font_size=tgt.font_size,
                    reason="manual_resize")
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
        self._resize_start_rect = QRectF(self._target_rect())
        self._font_size_before = (
            float(self._target.font_size)
            if isinstance(self._target, _EditTextItem) else None)
        self._target_was_movable = bool(
            self._target.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self._target.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        scene = self.scene()
        window = getattr(scene, "_window", None) if scene is not None else None
        if window is not None and isinstance(self._target, _EditTextItem):
            window._log_edit_event(
                "voucher_text_resize_start",
                object_id=self._target.obj_id,
                object_type=type(self._target).__name__,
                handle=self._position,
                font_size_before=self._font_size_before,
                target_item_valid=self._target.scene() is scene)
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
        h = scene_units_for_view_pixels(self.scene(), HANDLE_HIT_SIZE_PX)
        return QRectF(-h / 2, -h / 2, h, h)

    def shape(self) -> QPainterPath:  # noqa: N802
        path = QPainterPath()
        h = scene_units_for_view_pixels(self.scene(), HANDLE_HIT_SIZE_PX)
        path.addRect(QRectF(-h / 2, -h / 2, h, h))
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


def resolve_edit_object_from_graphics_item(
    item: QGraphicsItem | None, scene: QGraphicsScene | None = None,
) -> QGraphicsItem | None:
    """hit proxy、ハンドル、子itemから保存対象の実オブジェクトを返す。"""
    seen: set[int] = set()
    current = item
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, "serialize_edit_object"):
            return current
        for attr in ("owner_item", "source_item", "_target"):
            owner = getattr(current, attr, None)
            if owner is not None and hasattr(owner, "serialize_edit_object"):
                return owner
        object_id = getattr(current, "object_id", None)
        if object_id and scene is not None:
            for candidate in scene.items():
                if (getattr(candidate, "obj_id", None) == object_id
                        and hasattr(candidate, "serialize_edit_object")):
                    return candidate
        current = current.parentItem()
    return None


class _PrintSafeAreaGuideItem(QGraphicsRectItem):
    """見た目だけ描画し、scene のヒットテストには参加しないガイド。"""

    def shape(self) -> QPainterPath:
        return QPainterPath()


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
            if getattr(item, "_PRINT_GUIDE", False):
                item = item.parentItem()
                continue
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
                    and h.mapToScene(h.shape()).contains(pos)):
                return True
        return False

    def _resolve_edit_object(self, pos: QPointF):
        """位置に重なる候補から操作対象の実編集オブジェクトを解決する。"""
        from PySide6.QtGui import QTransform

        radius = scene_units_for_view_pixels(
            self, CLICK_SEARCH_RADIUS_PX)
        search_rect = QRectF(
            pos.x() - radius, pos.y() - radius, radius * 2.0, radius * 2.0)
        raw_items = self.items(search_rect, Qt.ItemSelectionMode.IntersectsItemShape,
                               Qt.SortOrder.DescendingOrder, QTransform())
        candidates: list[QGraphicsItem] = []
        for hit in raw_items:
            if getattr(hit, "_PRINT_GUIDE", False):
                continue
            resolved = resolve_edit_object_from_graphics_item(hit, self)
            if resolved is not None and resolved not in candidates:
                candidates.append(resolved)
        if not candidates:
            # 極小テキストの透明な拡張hit領域だけがshape検索に入らない場合の
            # 軽量フォールバック。通常経路ではページ背景等を列挙しない。
            raw_items = self.items(
                search_rect, Qt.ItemSelectionMode.IntersectsItemBoundingRect,
                Qt.SortOrder.DescendingOrder, QTransform())
            for hit in raw_items:
                if getattr(hit, "_PRINT_GUIDE", False):
                    continue
                resolved = resolve_edit_object_from_graphics_item(hit, self)
                if resolved is not None and resolved not in candidates:
                    candidates.append(resolved)
        if not candidates:
            return None

        def line_distance(item: _EditLineItem) -> float:
            best = float("inf")
            ln = item.line()
            for x1, y1, x2, y2 in line_segments(
                    item.line_type, ln.x1(), ln.y1(), ln.x2(), ln.y2()):
                a = item.mapToScene(QPointF(x1, y1))
                b = item.mapToScene(QPointF(x2, y2))
                vx, vy = b.x() - a.x(), b.y() - a.y()
                wx, wy = pos.x() - a.x(), pos.y() - a.y()
                denom = vx * vx + vy * vy
                t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom)) if denom else 0.0
                dx = pos.x() - (a.x() + t * vx)
                dy = pos.y() - (a.y() + t * vy)
                best = min(best, (dx * dx + dy * dy) ** 0.5)
            return best

        def priority(item):
            local = item.mapFromScene(pos)
            precise = isinstance(item, (_EditTextItem, _EditLineItem))
            if isinstance(item, _EditLineItem):
                distance = line_distance(item)
                actual_hit = distance <= max(float(item.line_width), 1.0) / 2.0
            else:
                actual = (super(_EditTextItem, item).boundingRect()
                          if isinstance(item, _EditTextItem)
                          else item.boundingRect())
                actual_hit = bool(actual.contains(local))
                center = item.mapToScene(actual.center())
                distance = ((center.x() - pos.x()) ** 2
                            + (center.y() - pos.y()) ** 2) ** 0.5
            expanded_hit = bool(item.shape().contains(local))
            selected = bool(item.isSelected())
            # 小さい文字・細線の透明shapeは、大きな背景図形の広い内部より優先。
            # 同じ精密オブジェクト同士では実描画への直接hitを最優先する。
            if precise and actual_hit:
                tier = 0
            elif precise and expanded_hit:
                tier = 1
            elif actual_hit:
                tier = 2
            else:
                tier = 3
            return (tier, not selected, distance, -float(item.zValue()))

        return min(candidates, key=priority)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        target = self._resolve_edit_object(event.scenePos())
        if isinstance(target, (_EditTextItem, _EditSymbolTextItem)):
            if self._window.begin_text_edit(target):
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

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
        # ハンドルは既に選択中オブジェクトに属する。ここでclearSelectionを伴う
        # 単一選択処理を行うと、selectionChangedがドラッグ中のハンドルを破棄する。
        if not self._press_handle:
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
            target_vouchers=list(self._window._creation_target_vouchers),
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
                rect = QRectF(start, end).normalized()
                dragged = (
                    abs(end.x() - start.x()) >= TEXT_DRAG_THRESHOLD
                    or abs(end.y() - start.y()) >= TEXT_DRAG_THRESHOLD
                )
                if dragged:
                    fs = fit_font_size_to_text_box(
                        "テキスト", rect.width(), rect.height(),
                        family=self._window.current_font_family,
                        bold=self._window.current_font_bold,
                        italic=self._window.current_font_italic,
                        underline=self._window.current_font_underline,
                        strikeout=self._window.current_font_strikeout,
                    )
                    self._window.add_text_rect(
                        rect, font_size=fs, auto_fit=False,
                        auto_fit_to_box=True)
                else:
                    self._window.add_text_at(start)
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
        if not self._press_handle:
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
        # 右クリックは Qt の仮想オーバーライド上で実行されるため、ここで例外が
        # 送出されると PySide がプロセスごと落とす。対象検証・メニュー生成・表示の
        # すべてを try/except で保護し、失敗してもアプリを落とさずログに残す。
        window = self._window
        try:
            window._log_edit_event("voucher_edit_context_menu_requested")
            target = self._resolve_edit_object(event.scenePos())
            if target is None or not window._object_action_allowed(target):
                if target is not None:
                    window._log_edit_event(
                        "voucher_edit_context_menu_target_invalid",
                        reason="not_actionable")
                window._show_canvas_context_menu(event.scenePos(), event.screenPos())
                event.accept()
                return
            # 既に複数選択に含まれる対象を右クリックした場合は選択を維持し、
            # 一括装飾を可能にする。未選択対象なら従来どおり単一選択する。
            if not target.isSelected():
                window._select_only(target)
            window._show_object_context_menu(target, event.screenPos())
            event.accept()
        except Exception as exc:  # noqa: BLE001 - 右クリックでアプリを落とさない
            try:
                window._log_edit_event(
                    "voucher_edit_context_menu_exception", error=repr(exc))
            except Exception:
                pass
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

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData() is not None and event.mimeData().hasFormat(FAVORITE_OBJECT_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData() is not None and event.mimeData().hasFormat(FAVORITE_OBJECT_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime is not None and mime.hasFormat(FAVORITE_OBJECT_MIME):
            fav_id = bytes(mime.data(FAVORITE_OBJECT_MIME)).decode("utf-8", errors="ignore")
            window = self.window()
            handler = getattr(window, "drop_favorite_object", None)
            if callable(handler) and handler(fav_id, self.mapToScene(event.position().toPoint())):
                event.acceptProposedAction()
                return
        super().dropEvent(event)


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


# 「図形」ボタンへまとめる図形ツール（線/矢印/両矢印/二重線/四角/丸・要件5）。
_SHAPE_TOOLS = (
    ("線", TOOL_LINE),
    ("矢印", TOOL_ARROW),
    ("両矢印", TOOL_DOUBLE_ARROW),
    ("二重線", TOOL_DOUBLE_LINE),
    ("四角", TOOL_RECT),
    ("丸", TOOL_ELLIPSE),
)


class _ShapeToolButton(QToolButton):
    """図形メニュー用ボタン。押下だけでなくカーソルが乗ったときもメニューを開く（要件6）。"""

    def enterEvent(self, event) -> None:  # noqa: N802
        try:
            menu = self.menu()
            if self.isEnabled() and menu is not None and not menu.isVisible():
                self.showMenu()
        except Exception:  # noqa: BLE001 - hover表示失敗でUIを落とさない
            pass
        super().enterEvent(event)


class VoucherEditWindow(QMainWindow):
    """指図書(1)を背景に図形・テキストを編集する全画面ウィンドウ。"""

    voucherEditSaved = Signal(str, str, str, str, int)

    def __init__(self, order_no: str, background_pdf_bytes: bytes = b"",
                 parent: QWidget | None = None, *,
                 voucher_nos: list[object] | tuple[object, ...] | None = None,
                 background_pdf_by_voucher: dict[str, bytes] | None = None,
                 preview_target_voucher: str = "03",
                 defer_background: bool = False,
                 request_started: float | None = None) -> None:
        self._perf_started = request_started or time.perf_counter()
        _perf_editor("window_generation_start", self._perf_started,
                     order_no=order_no)
        super().__init__(parent)
        self.order_no = order_no
        self._preview_generation = 0
        self._preview_thread: QThread | None = None
        self._preview_worker: QObject | None = None
        self._preview_windows: list[QWidget] = []
        self.voucher_nos = unique_voucher_numbers(voucher_nos)
        self.current_voucher_no = self.voucher_nos[0]
        self._current_voucher_key = voucher_key_for(self.current_voucher_no)
        edit_data_started = time.perf_counter()
        document = load_voucher_edit_document(order_no, self.voucher_nos)
        _perf_editor("edit_data_loaded", edit_data_started)
        self._common_objects = document["common_edit"]
        self._voucher_objects = document["voucher_edits"]
        self._edit_mode = "common"
        self._voucher_histories: dict[str, tuple[list[list[dict[str, Any]]], int]] = {}
        self._dirty_voucher_keys: set[str] = set()
        self._switching_voucher = False
        self._background_pdf_by_voucher = {
            voucher_key_for(key): value
            for key, value in (background_pdf_by_voucher or {}).items()
        }
        self._preview_target_voucher = str(preview_target_voucher or "03").strip() or "03"
        self._preview_page_index = 0
        self._preview_pixmap_cache: dict[
            tuple[str, str, str, int, int, str], tuple[QPixmap, float, float]
        ] = {}
        self._active_preview_cache_key: tuple[str, str, str, int, int, str] | None = None
        self._defer_background = bool(defer_background)
        self._background_load_generation = 0
        self._background_ready = False
        # 初期ツールは「テキスト」。開いてすぐドラッグでテキストボックスを作れる（要件2）。
        self.current_tool = TOOL_TEXT
        self.current_font_family = resolve_text_font_family(DEFAULT_FONT_FAMILY)
        self.current_font_size = DEFAULT_FONT_SIZE
        self.current_font_bold = False
        self.current_font_italic = False
        self.current_font_underline = False
        self.current_font_strikeout = False
        self.current_line_width = DEFAULT_LINE_WIDTH
        # 手書きペンの太さ・色（タブレット編集モードで使用。初期は太さ「中」・色「黒」）。
        self.current_pen_width = DEFAULT_PEN_WIDTH
        self.current_pen_color = DEFAULT_PEN_COLOR
        # 消しゴムでこのドラッグ中に削除があったか（離した時に1回だけ履歴へ積む）。
        self._eraser_changed = False
        # 反映先は「保存済み既定」「次回作成用」「選択オブジェクト表示」を分離する。
        # current_* はUI表示互換用で、作成時は _creation_target_* だけを参照する。
        self._templates: list[dict[str, Any]] = self._ordered_templates(load_templates())
        self._default_reflection_target_key = self._load_default_reflection_target_key()
        default_template = self._template_by_key(self._default_reflection_target_key)
        if default_template is None:  # standard は組み込み固定だが、防御的に維持する。
            default_template = self._templates[0] if self._templates else {
                "key": FALLBACK_REFLECTION_TARGET_KEY,
                "name": "標準",
                "target_vouchers": list(DEFAULT_TARGET_VOUCHERS),
            }
        self._creation_target_vouchers = list(default_template["target_vouchers"])
        self._creation_template_key = str(default_template["key"])
        self._creation_template_name = str(default_template["name"])
        self.current_target_vouchers: list[str] = list(self._creation_target_vouchers)
        self._current_template_name: str = self._creation_template_name
        self._template_actions: dict[str, Any] = {}
        self.loaded_object_ids: set[str] = set()
        self._handles: list[QGraphicsItem] = []
        # テンプレートバッヂ（編集画面のみ・保存/PDF/Undo対象外）の補助アイテム（要件6）。
        self._badges: list[QGraphicsItem] = []
        # 背景アイテムへの参照を保持する。scene 全走査だけでなくリストでも管理する（要件3）。
        self._background_items: list[QGraphicsItem] = []
        self._tool_actions: dict[str, Any] = {}
        self._text_decoration_actions: dict[str, QAction] = {}
        self._text_decoration_mixed: set[str] = set()
        # Undo/Redo 用のスナップショット履歴（要件1・3）。
        self._history: list[list[dict[str, Any]]] = []
        self._history_index: int = -1
        self._object_clipboard: list[dict[str, Any]] = []
        self._favorites: list[dict[str, Any]] = load_favorite_objects()
        # フォントのお気に入りは編集データやUndo履歴には含めず、QSettingsだけで保持する。
        favorite_fonts_started = time.perf_counter()
        self._favorite_fonts: list[str] = load_favorite_fonts()
        _perf_editor("favorite_fonts_built", favorite_fonts_started,
                     count=len(self._favorite_fonts))
        # 履歴復元中フラグ。復元中の commit_history を抑止しRedo履歴を守る（要件1）。
        self._is_restoring_history = False
        # 上部ツールバーのアンドゥ・リドゥアクション（要件1）。
        self._undo_action = None
        self._redo_action = None
        # Undo/Redoスタックのログ用に直近のスタック数を保持する（要件1）。
        self._prev_undo_depth = -1
        self._prev_redo_depth = -1
        self._updating_property_ui = False
        # 未保存変更フラグ（要件3）。閉じる時に確認ダイアログを出すため使う。
        self._dirty = False
        # デバッグ表示設定は他UI互換のため保持する。
        self._debug_visible = is_debug_visible()
        # クローズ進行中フラグ。
        self._closing = False
        self._close_in_progress = False
        # 二値化／背景を透過（閾値）で使う RGB しきい値。保存済み値（既定は中）を読む（要件9）。
        self._threshold_rgb: tuple[int, int, int] = load_threshold_rgb()
        # ロック対象の編集アクション。_build_toolbar で実体を格納する（要件2）。
        self._edit_actions: list[Any] = []
        # ── タブレット編集モード（SuperDisplay 外部ディスプレイ運用）─────────────
        # tablet_mode 中は通常ペイン/ツールバーを隠し、大きいボタンの専用ツールバーを
        # 表示して全画面化する。編集データ（scene のオブジェクト）は通常モードと共有する。
        self.tablet_mode = False
        self._main_toolbar: "QToolBar | None" = None
        self._main_toolbar_container: "QScrollArea | None" = None
        self._template_panel_scroll: "QScrollArea | None" = None
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
        self._default_maximize_applied = False
        self.setWindowTitle(f"指図書編集 — 受注No {order_no} — 伝票No {self._voucher_label(self.current_voucher_no)}")

        self._scene = _EditScene(self)
        self._print_guide: QGraphicsRectItem | None = None
        self._print_guide_visible = True
        self._create_print_safe_area_guide()
        _perf_editor("scene_created", self._perf_started)
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self._view = _EditGraphicsView(self._scene)
        self._view.setAcceptDrops(True)
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
        voucher_bar = QWidget()
        self._voucher_bar = voucher_bar
        voucher_layout = QHBoxLayout(voucher_bar)
        voucher_layout.setContentsMargins(8, 4, 8, 4)
        self._editing_voucher_label = QLabel()
        self._preview_voucher_label = QLabel()
        self._multiple_vouchers_notice = QLabel()
        self._multiple_vouchers_notice.setObjectName("multipleVouchersNotice")
        self._multiple_vouchers_notice.setToolTip(
            "全伝票Noへの共通編集、または伝票Noごとの個別編集を選択できます。")
        self._all_vouchers_radio = QRadioButton("全伝票No")
        self._individual_voucher_radio = QRadioButton("個別の伝票No")
        self._edit_scope_group = QButtonGroup(self)
        self._edit_scope_group.addButton(self._all_vouchers_radio)
        self._edit_scope_group.addButton(self._individual_voucher_radio)
        self._all_vouchers_radio.setToolTip(
            "ここで追加した内容は、この受注Noのすべての伝票Noへ反映されます。")
        self._individual_voucher_radio.setToolTip(
            "ここで追加した内容は、選択した伝票Noだけへ反映されます。")
        self._all_vouchers_radio.setChecked(True)
        self._all_vouchers_radio.toggled.connect(self._on_edit_scope_changed)
        self._individual_voucher_radio.toggled.connect(self._on_edit_scope_changed)
        self._voucher_combo = QComboBox()
        for voucher_no in self.voucher_nos:
            self._voucher_combo.addItem(self._voucher_label(voucher_no), voucher_no)
        self._voucher_combo.setEnabled(False)
        self._voucher_combo.setToolTip(f"受注No {order_no} のプレビュー基準／個別編集対象伝票")
        self._voucher_combo.currentIndexChanged.connect(self._on_voucher_combo_changed)
        selected_copy = QPushButton("選択を他伝票へコピー")
        selected_copy.clicked.connect(lambda: self.show_copy_to_vouchers_dialog(selected_only=True))
        all_copy = QPushButton("全体を他伝票へコピー")
        all_copy.clicked.connect(lambda: self.show_copy_to_vouchers_dialog(selected_only=False))
        enabled_copy = len(self.voucher_nos) > 1
        selected_copy.setEnabled(enabled_copy)
        all_copy.setEnabled(enabled_copy)
        voucher_layout.addWidget(QLabel("編集対象:"))
        voucher_layout.addWidget(self._all_vouchers_radio)
        voucher_layout.addWidget(self._individual_voucher_radio)
        voucher_layout.addSpacing(12)
        voucher_layout.addWidget(QLabel("伝票No:"))
        voucher_layout.addWidget(self._voucher_combo)
        voucher_layout.addWidget(self._editing_voucher_label)
        voucher_layout.addWidget(self._preview_voucher_label)
        voucher_layout.addWidget(self._multiple_vouchers_notice)
        voucher_layout.addStretch(1)
        voucher_layout.addWidget(selected_copy)
        voucher_layout.addWidget(all_copy)
        outer_layout.addWidget(voucher_bar)
        self._update_voucher_count_ui()
        self._update_voucher_heading()
        if self._main_toolbar_container is not None:
            outer_layout.addWidget(self._main_toolbar_container)
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
        self._template_panel_scroll = self._build_left_pane_scroll_area(self._template_panel)
        central_layout.addWidget(self._template_panel_scroll)
        central_layout.addWidget(self._view, 1)
        outer_layout.addWidget(body, 1)
        self.setCentralWidget(central)
        _perf_editor("ui_parts_created", self._perf_started)

        initial_background = self._background_pdf_by_voucher.get(self._current_voucher_key)
        if self._defer_background:
            self._add_preview_loading_background()
            if initial_background:
                QTimer.singleShot(
                    0,
                    lambda key=self._current_voucher_key, data=bytes(initial_background):
                    self.set_background_pdf_async(key, data),
                )
        elif initial_background is not None:
            self._install_preview_background(self._current_voucher_key)
        else:
            # 単一伝票で呼ばれる旧APIとの互換。以後の再表示にも使えるようマップへ登録する。
            self._background_pdf_by_voucher[self._current_voucher_key] = background_pdf_bytes
            self._add_background(background_pdf_bytes)
        self._install_shortcuts()
        self.load_edit_layer()
        self._ensure_print_safe_area_guide()
        _perf_editor("saved_objects_restored", self._perf_started,
                     count=len(self.loaded_object_ids))
        # 初期読み込み完了時点は未保存変更なしとする。
        self._dirty = False
        self._debug_state("open")
        _perf_editor("window_generation_complete", self._perf_started)

    @staticmethod
    def _voucher_label(voucher_no: object) -> str:
        value = normalize_voucher_no(voucher_no)
        return value if value else "（伝票Noなし）"

    def _update_voucher_heading(self) -> None:
        label = self._voucher_label(self.current_voucher_no)
        if self._edit_mode == "common":
            self._editing_voucher_label.setText("編集中: 全伝票No共通")
            self._preview_voucher_label.setText(f"プレビュー基準: {label}")
        else:
            self._editing_voucher_label.setText(f"編集中: 伝票No {label}")
            self._preview_voucher_label.setText("共通オブジェクト: 表示のみ")
        self.setWindowTitle(
            f"指図書編集 — 受注No {self.order_no} — 伝票No {label}")

    def _current_edit_key(self) -> str:
        return COMMON_EDIT_KEY if self._edit_mode == "common" else self._current_voucher_key

    def _current_edit_objects(self) -> list[dict[str, Any]]:
        if self._edit_mode == "common":
            return self._common_objects
        return self._voucher_objects.setdefault(self._current_voucher_key, [])

    def _apply_voucher_count_theme(self) -> None:
        """単一時のdisabled文字と複数伝票通知を現在テーマで読みやすくする。"""
        dark = current_title_bar_is_dark()
        if dark:
            radio_color = "#d7dce2"
            notice_style = (
                "QLabel#multipleVouchersNotice {"
                " background-color: #5a4618; color: #fff1b8;"
                " border: 1px solid #e0b84f; border-radius: 4px;"
                " font-weight: bold; padding: 3px 8px; }"
            )
        else:
            radio_color = "#4b4f55"
            notice_style = (
                "QLabel#multipleVouchersNotice {"
                " background-color: #fff3cd; color: #5f4300;"
                " border: 1px solid #e0a800; border-radius: 4px;"
                " font-weight: bold; padding: 3px 8px; }"
            )
        radio_style = f"QRadioButton:disabled {{ color: {radio_color}; }}"
        self._all_vouchers_radio.setStyleSheet(radio_style)
        self._individual_voucher_radio.setStyleSheet(radio_style)
        self._multiple_vouchers_notice.setStyleSheet(notice_style)

    def _update_voucher_count_ui(self) -> None:
        """ユニーク伝票数から編集対象UIを一括更新する。データは変更しない。"""
        self.voucher_nos = unique_voucher_numbers(self.voucher_nos)
        count = len(self.voucher_nos)
        multiple = count >= 2
        single_tooltip = "伝票Noが1件のため、編集対象の切り替えは不要です。"
        if not multiple and self._edit_mode != "common":
            # toggledハンドラが現在個別sceneを元の個別モデルへ退避してから、共通へ切り替える。
            self._all_vouchers_radio.setChecked(True)
        self._all_vouchers_radio.setEnabled(multiple)
        self._individual_voucher_radio.setEnabled(multiple)
        if multiple:
            self._all_vouchers_radio.setToolTip(
                "ここで追加した内容は、この受注Noのすべての伝票Noへ反映されます。")
            self._individual_voucher_radio.setToolTip(
                "ここで追加した内容は、選択した伝票Noだけへ反映されます。")
        else:
            self._all_vouchers_radio.setToolTip(single_tooltip)
            self._individual_voucher_radio.setToolTip(single_tooltip)
        self._voucher_combo.setEnabled(multiple and self._edit_mode == "individual")
        self._multiple_vouchers_notice.setText(
            f"⚠ 複数の伝票Noがあります（{count}件）" if multiple else "")
        self._multiple_vouchers_notice.setVisible(multiple)
        self._apply_voucher_count_theme()

    def set_voucher_numbers(
        self, voucher_nos: list[object] | tuple[object, ...] | None,
    ) -> None:
        """伝票一覧を差し替え、編集モデルを壊さず件数UIと表示対象を更新する。"""
        numbers = unique_voucher_numbers(voucher_nos)
        if numbers == self.voucher_nos:
            self._update_voucher_count_ui()
            return
        self._remember_current_voucher()
        # 単一化では旧個別キーを保持したまま、先に共通モードへ戻す。
        if len(numbers) == 1 and self._edit_mode != "common":
            self._all_vouchers_radio.setChecked(True)
        old_key = self._current_voucher_key
        valid_keys = {voucher_key_for(no) for no in numbers}
        current = (self.current_voucher_no if old_key in valid_keys else numbers[0])
        self.voucher_nos = numbers
        self.current_voucher_no = current
        self._current_voucher_key = voucher_key_for(current)
        for no in numbers:
            self._voucher_objects.setdefault(voucher_key_for(no), [])
        self._switching_voucher = True
        try:
            self._voucher_combo.clear()
            for no in numbers:
                self._voucher_combo.addItem(self._voucher_label(no), no)
            index = next((i for i, no in enumerate(numbers)
                          if voucher_key_for(no) == self._current_voucher_key), 0)
            self._voucher_combo.setCurrentIndex(index)
        finally:
            self._switching_voucher = False
        if old_key != self._current_voucher_key:
            for item in self.background_items():
                self._scene.removeItem(item)
            self._background_items = []
            self._install_preview_background(self._current_voucher_key)
            self.load_edit_layer()
        self._update_voucher_count_ui()
        self._update_voucher_heading()

    def _remember_current_voucher(self) -> None:
        """sceneと履歴を現在の伝票モデルへ同期する。"""
        self._scene.cancel_temp_item()
        self._scene.clearSelection()
        self._remove_handles()
        snapshot = self.serialize_objects()
        if (not self._history or self._history_index < 0
                or snapshot != self._history[self._history_index]):
            del self._history[self._history_index + 1:]
            self._history.append(json.loads(json.dumps(snapshot, ensure_ascii=False)))
            if len(self._history) > HISTORY_LIMIT:
                self._history = self._history[-HISTORY_LIMIT:]
            self._history_index = len(self._history) - 1
            self._dirty = True
            self._dirty_voucher_keys.add(self._current_edit_key())
        scope_key = self._current_edit_key()
        if self._edit_mode == "common":
            self._common_objects = snapshot
        else:
            self._voucher_objects[self._current_voucher_key] = snapshot
        self._voucher_histories[scope_key] = (
            json.loads(json.dumps(self._history, ensure_ascii=False)),
            self._history_index,
        )

    def _on_voucher_combo_changed(self, index: int) -> None:
        if self._switching_voucher or index < 0:
            return
        self.switch_voucher(self._voucher_combo.itemData(index))

    def _on_edit_scope_changed(self, checked: bool) -> None:
        if not checked or not hasattr(self, "_scene"):
            return
        target_mode = "common" if self._all_vouchers_radio.isChecked() else "individual"
        if target_mode == "individual" and len(self.voucher_nos) < 2:
            self._all_vouchers_radio.setChecked(True)
            self._update_voucher_count_ui()
            return
        if target_mode == self._edit_mode:
            return
        self._remember_current_voucher()
        self._edit_mode = target_mode
        self._update_voucher_count_ui()
        self.load_edit_layer()
        stored = self._voucher_histories.get(self._current_edit_key())
        if stored is not None:
            self._history, self._history_index = json.loads(
                json.dumps(stored[0], ensure_ascii=False)), stored[1]
        self._update_voucher_heading()
        self._update_undo_redo_buttons()

    def switch_voucher(self, voucher_no: object) -> None:
        """未保存状態をメモリに保持して、別伝票専用sceneへ安全に切り替える。"""
        normalized = normalize_voucher_no(voucher_no)
        key = voucher_key_for(normalized)
        if key == self._current_voucher_key or key not in {
                voucher_key_for(no) for no in self.voucher_nos}:
            return
        if self._edit_mode == "common":
            self.current_voucher_no = normalized
            self._current_voucher_key = key
            for item in self.background_items():
                self._scene.removeItem(item)
            self._background_items = []
            self._install_preview_background(key)
            self._switching_voucher = True
            try:
                target_index = next(
                    (i for i, no in enumerate(self.voucher_nos)
                     if voucher_key_for(no) == key), 0)
                self._voucher_combo.setCurrentIndex(target_index)
            finally:
                self._switching_voucher = False
            self._update_voucher_heading()
            return
        self._remember_current_voucher()
        self._switching_voucher = True
        self._is_restoring_history = True
        old_transform = self._view.transform()
        old_h_scroll = self._view.horizontalScrollBar().value()
        old_v_scroll = self._view.verticalScrollBar().value()
        self._voucher_bar.setEnabled(False)
        self._view.setUpdatesEnabled(False)
        self._log_edit_event(
            "voucher_edit_preview_switch_started", order_no=self.order_no,
            voucher_no=normalized, target_voucher=self._preview_target_voucher)
        try:
            self.current_voucher_no = normalized
            self._current_voucher_key = key
            self.loaded_object_ids.clear()
            # 旧背景がある間に編集レイヤーだけを消す。clear_edit_layer 内の背景復旧処理が
            # 直前伝票の背景を作り直さない順序にする。
            self.clear_edit_layer()
            for item in self.background_items():
                self._scene.removeItem(item)
            self._background_items = []
            self._install_preview_background(key)
            self._add_readonly_common_objects()
            for obj in self._voucher_objects.get(key, []):
                self._add_loaded_object(obj)
            stored = self._voucher_histories.get(self._current_edit_key())
            if stored is None:
                self._history = [self.serialize_objects()]
                self._history_index = 0
            else:
                self._history, self._history_index = json.loads(
                    json.dumps(stored[0], ensure_ascii=False)), stored[1]
            target_index = next(
                (i for i, no in enumerate(self.voucher_nos)
                 if voucher_key_for(no) == key), 0)
            self._voucher_combo.setCurrentIndex(target_index)
        finally:
            self._is_restoring_history = False
            self._switching_voucher = False
            self._view.setTransform(old_transform)
            self._view.horizontalScrollBar().setValue(old_h_scroll)
            self._view.verticalScrollBar().setValue(old_v_scroll)
            self._view.setUpdatesEnabled(True)
            self._view.viewport().update()
            self._voucher_bar.setEnabled(True)
        self._update_voucher_heading()
        self._update_undo_redo_buttons()
        self.refresh_badges()
        self._refresh_layer_panel()
        self._log_edit_event(
            "voucher_edit_state_loaded_by_voucher_no",
            order_no=self.order_no, voucher_no=normalized)

    def _preview_cache_key(
        self, voucher_key: str,
    ) -> tuple[str, str, str, int, int, str]:
        from app.voucher_service import PDF_TEXT_RENDERER_REVISION
        pdf_bytes = self._background_pdf_by_voucher.get(voucher_key, b"")
        return (
            str(self.order_no), voucher_key, self._preview_target_voucher,
            self._preview_page_index, PDF_TEXT_RENDERER_REVISION,
            hashlib.sha256(pdf_bytes).hexdigest(),
        )

    def _install_preview_background(self, voucher_key: str) -> bool:
        """対象伝票のキャッシュ済み背景をsceneへ設定する。失敗時はエラー背景にする。"""
        cache_key = self._preview_cache_key(voucher_key)
        cached = self._preview_pixmap_cache.get(cache_key)
        if cached is not None:
            self._log_edit_event(
                "voucher_edit_preview_cache_hit", order_no=self.order_no,
                voucher_no=self.current_voucher_no,
                target_voucher=self._preview_target_voucher)
            pixmap, page_w, page_h = cached
        else:
            self._log_edit_event(
                "voucher_edit_preview_cache_miss", order_no=self.order_no,
                voucher_no=self.current_voucher_no,
                target_voucher=self._preview_target_voucher)
            pdf_bytes = self._background_pdf_by_voucher.get(voucher_key)
            if self._defer_background:
                self._add_preview_loading_background()
                if pdf_bytes:
                    QTimer.singleShot(
                        0, lambda key=voucher_key, data=bytes(pdf_bytes):
                        self.set_background_pdf_async(key, data))
                return False
            if not pdf_bytes:
                self._add_preview_error_background("この伝票Noの指図書プレビューを取得できませんでした。")
                self._log_edit_event(
                    "voucher_edit_preview_switch_failed", order_no=self.order_no,
                    voucher_no=self.current_voucher_no, reason="preview_pdf_missing")
                return False
            pixmap = render_order_sheet_background(pdf_bytes)
            size = pdf_page_size(pdf_bytes)
            if pixmap is None or pixmap.isNull() or size is None:
                self._add_preview_error_background("指図書プレビューの表示に失敗しました。")
                self._log_edit_event(
                    "voucher_edit_preview_switch_failed", order_no=self.order_no,
                    voucher_no=self.current_voucher_no, reason="preview_render_failed")
                return False
            page_w, page_h = size
            self._preview_pixmap_cache[cache_key] = (pixmap, page_w, page_h)
        self._add_background_pixmap(pixmap, page_w, page_h)
        self._active_preview_cache_key = cache_key
        self._log_edit_event(
            "voucher_edit_preview_switched_by_voucher_no", order_no=self.order_no,
            voucher_no=self.current_voucher_no,
            target_voucher=self._preview_target_voucher,
            page_index=self._preview_page_index)
        return True

    def invalidate_preview_cache(
        self, voucher_no: object | None = None, *,
        background_pdf_by_voucher: dict[str, bytes] | None = None,
    ) -> None:
        """PDF再生成・元データ/テンプレート/伝票設定変更時の背景キャッシュを破棄する。"""
        if background_pdf_by_voucher is not None:
            self._background_pdf_by_voucher = {
                voucher_key_for(key): value
                for key, value in background_pdf_by_voucher.items()
            }
            if hasattr(self, "_multiple_vouchers_notice"):
                self.set_voucher_numbers(list(background_pdf_by_voucher))
        elif hasattr(self, "_multiple_vouchers_notice"):
            self._update_voucher_count_ui()
        if voucher_no is None:
            self._preview_pixmap_cache.clear()
        else:
            key = voucher_key_for(voucher_no)
            for cache_key in list(self._preview_pixmap_cache):
                if cache_key[1] == key:
                    del self._preview_pixmap_cache[cache_key]
        self._active_preview_cache_key = None

    def set_preview_target_voucher(
        self, target_voucher: str,
        background_pdf_by_voucher: dict[str, bytes],
    ) -> None:
        """編集対象を指図書(1)/(2)等へ変更し、現在伝票の背景を即時更新する。"""
        self._preview_target_voucher = str(target_voucher or "03").strip() or "03"
        self.invalidate_preview_cache(
            background_pdf_by_voucher=background_pdf_by_voucher)
        self._view.setUpdatesEnabled(False)
        try:
            for item in self.background_items():
                self._scene.removeItem(item)
            self._background_items = []
            self._install_preview_background(self._current_voucher_key)
        finally:
            self._view.setUpdatesEnabled(True)
            self._view.viewport().update()

    def _selected_object_models(self) -> list[dict[str, Any]]:
        selected = [item for item in self._scene.selectedItems()
                    if hasattr(item, "serialize_edit_object")]
        return [item.serialize_edit_object() for item in selected]

    def copy_objects_to_vouchers(
        self,
        target_voucher_nos: list[object] | tuple[object, ...],
        *,
        selected_only: bool = True,
        replace: bool = False,
        _source_objects: list[dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        """保存モデルだけを複製する。戻り値は(対象物数, コピー先数)。"""
        if replace and selected_only:
            raise ValueError("置換は全オブジェクトコピー時だけ使用できます。")
        selected_source = (copy.deepcopy(_source_objects)
                           if _source_objects is not None
                           else self._selected_object_models() if selected_only else None)
        self._remember_current_voucher()
        source = selected_source if selected_only else self._current_edit_objects()
        if not source:
            return (0, 0)
        valid_keys = {voucher_key_for(no) for no in self.voucher_nos}
        target_keys: list[str] = []
        for no in target_voucher_nos:
            key = voucher_key_for(no)
            if (key in valid_keys
                    and (self._edit_mode == "common" or key != self._current_voucher_key)
                    and key not in target_keys):
                target_keys.append(key)
        for key in target_keys:
            before = self._voucher_objects.get(key, [])
            additions = clone_edit_objects(source)
            after = additions if replace else json.loads(
                json.dumps(before, ensure_ascii=False)) + additions
            self._voucher_objects[key] = after
            old_history, old_index = self._voucher_histories.get(
                key, ([json.loads(json.dumps(before, ensure_ascii=False))], 0))
            history = old_history[:old_index + 1]
            if not history or history[-1] != after:
                history.append(json.loads(json.dumps(after, ensure_ascii=False)))
            if len(history) > HISTORY_LIMIT:
                history = history[-HISTORY_LIMIT:]
            self._voucher_histories[key] = (history, len(history) - 1)
            self._dirty_voucher_keys.add(key)
        if target_keys:
            self._dirty = True
            self._log_edit_event(
                "voucher_edit_objects_copied_between_vouchers",
                source_voucher_no=self.current_voucher_no,
                object_count=len(source), target_count=len(target_keys),
                mode="replace" if replace else "append")
        return len(source), len(target_keys)

    def copy_objects_to_common(
        self, *, selected_only: bool = True, replace: bool = False,
        _source_objects: list[dict[str, Any]] | None = None,
    ) -> int:
        """個別編集オブジェクトを共通へ独立コピーする。"""
        if self._edit_mode != "individual":
            return 0
        if replace and selected_only:
            raise ValueError("置換は全オブジェクトコピー時だけ使用できます。")
        selected_source = (copy.deepcopy(_source_objects)
                           if _source_objects is not None else None)
        if selected_source is None and selected_only:
            selected_source = self._selected_object_models()
        self._remember_current_voucher()
        source = selected_source if selected_only else self._current_edit_objects()
        if not source:
            return 0
        before = self._common_objects
        additions = clone_edit_objects(source)
        after = additions if replace else json.loads(
            json.dumps(before, ensure_ascii=False)) + additions
        self._common_objects = after
        old_history, old_index = self._voucher_histories.get(
            COMMON_EDIT_KEY, ([json.loads(json.dumps(before, ensure_ascii=False))], 0))
        history = old_history[:old_index + 1]
        if not history or history[-1] != after:
            history.append(json.loads(json.dumps(after, ensure_ascii=False)))
        if len(history) > HISTORY_LIMIT:
            history = history[-HISTORY_LIMIT:]
        self._voucher_histories[COMMON_EDIT_KEY] = (history, len(history) - 1)
        self._dirty = True
        self._dirty_voucher_keys.add(COMMON_EDIT_KEY)
        return len(source)

    def show_copy_to_vouchers_dialog(self, *, selected_only: bool) -> None:
        """複数コピー先と追加/置換を選ぶダイアログを表示する。"""
        if selected_only and not self._selected_object_models():
            QMessageBox.information(self, "伝票間コピー", "コピーするオブジェクトを選択してください。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("コピー先伝票の選択")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("コピー先を選択してください"))
        checks: list[tuple[str, QCheckBox]] = []
        for no in self.voucher_nos:
            if (self._edit_mode == "individual"
                    and voucher_key_for(no) == self._current_voucher_key):
                continue
            check = QCheckBox(self._voucher_label(no))
            checks.append((no, check))
            layout.addWidget(check)
        common_check: QCheckBox | None = None
        if self._edit_mode == "individual":
            common_check = QCheckBox("全伝票No共通")
            layout.addWidget(common_check)
        select_row = QHBoxLayout()
        select_all = QPushButton("全選択")
        clear_all = QPushButton("全解除")
        select_all.clicked.connect(lambda: [check.setChecked(True) for _, check in checks])
        clear_all.clicked.connect(lambda: [check.setChecked(False) for _, check in checks])
        select_row.addWidget(select_all)
        select_row.addWidget(clear_all)
        layout.addLayout(select_row)
        append_radio = QRadioButton("追加")
        replace_radio = QRadioButton("既存内容を置換")
        append_radio.setChecked(True)
        replace_radio.setEnabled(not selected_only)
        layout.addWidget(append_radio)
        layout.addWidget(replace_radio)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("コピー")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        targets = [no for no, check in checks if check.isChecked()]
        copy_to_common = bool(common_check is not None and common_check.isChecked())
        if not targets and not copy_to_common:
            QMessageBox.warning(self, "伝票間コピー", "コピー先を選択してください。")
            return
        replace = replace_radio.isChecked()
        if replace:
            labels = "、".join(self._voucher_label(no) for no in targets)
            if copy_to_common:
                labels = "、".join(filter(None, (labels, "全伝票No共通")))
            answer = QMessageBox.question(
                self, "置換の確認",
                f"{labels} の既存編集内容を削除し、"
                f"{self._voucher_label(self.current_voucher_no)} の内容で置き換えます。よろしいですか？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        source_objects = (self._selected_object_models() if selected_only
                          else self.serialize_objects())
        object_count, target_count = self.copy_objects_to_vouchers(
            targets, selected_only=selected_only, replace=replace,
            _source_objects=source_objects)
        common_count = self.copy_objects_to_common(
            selected_only=selected_only, replace=replace,
            _source_objects=source_objects) if copy_to_common else 0
        QMessageBox.information(
            self, "コピー完了",
            f"{max(object_count, common_count)}個のオブジェクトを"
            f"{target_count + (1 if common_count else 0)}件の編集先へコピーしました。")

    # ── 未保存変更フラグ（要件3）─────────────────────────────────────────────
    def mark_dirty(self) -> None:
        self._dirty = True
        self._dirty_voucher_keys.add(self._current_edit_key())

    def mark_saved(self) -> None:
        self._dirty = False
        self._dirty_voucher_keys.clear()

    def is_dirty(self) -> bool:
        return bool(self._dirty or self._dirty_voucher_keys)

    def _print_safe_area_rect(self) -> QRectF:
        """現在のページ矩形から編集用の印刷安全範囲を算出する。"""
        page = self._scene.sceneRect()
        return QRectF(
            page.left() + SAFE_MARGIN_LEFT,
            page.top() + SAFE_MARGIN_TOP,
            max(0.0, page.width() - SAFE_MARGIN_LEFT - SAFE_MARGIN_RIGHT),
            max(0.0, page.height() - SAFE_MARGIN_TOP - SAFE_MARGIN_BOTTOM),
        )

    def _create_print_safe_area_guide(self) -> None:
        """編集ビュー専用の非選択・非インタラクティブなガイドを作る。"""
        guide = _PrintSafeAreaGuideItem(self._print_safe_area_rect())
        guide._IS_HELPER = True  # type: ignore[attr-defined]
        guide._PRINT_GUIDE = True  # type: ignore[attr-defined]
        guide.setData(_DATA_TYPE, _GUIDE_MARK)
        guide.setPen(QPen(QColor(210, 55, 65, 175), 1.2, Qt.PenStyle.DashLine))
        guide.setBrush(Qt.BrushStyle.NoBrush)
        guide.setZValue(-50.0)  # 背景より前、編集オブジェクトと操作部品より後ろ
        guide.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        guide.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        guide.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        guide.setAcceptHoverEvents(False)
        self._scene.addItem(guide)
        self._print_guide = guide
        _log.debug(
            "voucher_edit_print_guide_created id=%s scene_id=%s scene=%s "
            "visible=%s opacity=%s z=%s rect=%s page=%s safe=%s",
            id(guide), id(self._scene), guide.scene() is self._scene,
            guide.isVisible(), guide.opacity(), guide.zValue(), guide.rect(),
            self._scene.sceneRect(), guide.rect(),
        )

    def _ensure_print_safe_area_guide(self) -> None:
        """現在の編集sceneにガイドが1個だけ存在することを保証する。"""
        guide = getattr(self, "_print_guide", None)
        if guide is None or guide.scene() is not self._scene:
            for item in list(self._scene.items()):
                if getattr(item, "_PRINT_GUIDE", False):
                    self._scene.removeItem(item)
            self._create_print_safe_area_guide()
        self._update_print_safe_area_guide()

    def _update_print_safe_area_guide(self) -> None:
        guide = getattr(self, "_print_guide", None)
        if guide is not None and guide.scene() is self._scene:
            guide.setRect(self._print_safe_area_rect())
            guide.setVisible(self._print_guide_visible)

    def _toggle_print_safe_area_guide(self, checked: bool = False) -> None:
        action = getattr(self, "_print_guide_action", None)
        if action is not None:
            checked = action.isChecked()
        self._print_guide_visible = bool(checked)
        self._ensure_print_safe_area_guide()
        self._update_print_safe_area_guide()
        _log.debug(
            "voucher_edit_print_guide_toggled id=%s scene_id=%s checked=%s visible=%s",
            id(self._print_guide) if self._print_guide is not None else None,
            id(self._scene), checked,
            self._print_guide.isVisible() if self._print_guide is not None else None,
        )

    # ── 背景レイヤー ─────────────────────────────────────────────────────────
    def _clear_background_items(self) -> None:
        for item in list(self.background_items()):
            try:
                self._scene.removeItem(item)
            except RuntimeError:
                pass
        self._background_items = []

    def _add_preview_loading_background(self, message: str = "プレビューを読み込んでいます…") -> None:
        """背景待ちでもウィンドウ全体を操作できる非モーダルplaceholder。"""
        self._clear_background_items()
        page = QGraphicsRectItem(0, 0, PAGE_W, PAGE_H)
        page.setBrush(QBrush(QColor(248, 248, 248)))
        page.setPen(QPen(QColor(210, 210, 210)))
        self._mark_background(page)
        page.setData(1, self._current_voucher_key)
        page.setData(2, "loading")
        self._scene.addItem(page)
        self._background_items.append(page)
        label = QGraphicsSimpleTextItem(message)
        label.setBrush(QBrush(QColor(90, 90, 90)))
        label.setPos(PAGE_W / 2.0 - label.boundingRect().width() / 2.0, PAGE_H / 2.0)
        self._mark_background(label)
        label.setData(1, self._current_voucher_key)
        label.setData(2, "loading")
        label.setZValue(-99)
        self._scene.addItem(label)
        self._background_items.append(label)
        self._scene.setSceneRect(0, 0, PAGE_W, PAGE_H)
        self._update_print_safe_area_guide()
        self._background_ready = False

    def set_background_pdf_async(self, voucher_no_or_key: object, pdf_bytes: bytes) -> None:
        """現在伝票のPDFをworkerでラスタライズし、GUIスレッドでPixmap化する。"""
        raw_key = str(voucher_no_or_key or "")
        key = (
            raw_key if raw_key == self._current_voucher_key
            or raw_key in self._background_pdf_by_voucher
            else voucher_key_for(voucher_no_or_key)
        )
        self._background_pdf_by_voucher[key] = bytes(pdf_bytes)
        if key != self._current_voucher_key or self._closing:
            return
        self._background_load_generation += 1
        generation = self._background_load_generation
        self._add_preview_loading_background()
        started = time.perf_counter()
        _perf_editor("background_raster_worker_requested", self._perf_started,
                     generation=generation, voucher_key=key)
        thread = QThread()
        worker = _BackgroundRasterWorker(generation, key, bytes(pdf_bytes))
        thread._worker = worker  # type: ignore[attr-defined] - run完了までGCを防ぐ
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ready.connect(self._on_background_raster_ready)
        worker.failed.connect(self._on_background_raster_failed)
        worker.ready.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: _BACKGROUND_THREADS.discard(t))
        _BACKGROUND_THREADS.add(thread)
        worker._perf_started = started  # type: ignore[attr-defined] - lifetime/診断用
        thread.start()

    @Slot(int, str, object)
    def _on_background_raster_ready(self, generation: int, key: str, result: object) -> None:
        if (self._closing or generation != self._background_load_generation
                or key != self._current_voucher_key or not isinstance(result, dict)):
            _perf_editor("stale_background_result_discarded", self._perf_started,
                         generation=generation, voucher_key=key)
            return
        image_started = time.perf_counter()
        image = QImage.fromData(bytes(result.get("png_bytes") or b""), "PNG")
        if image.isNull():
            self._on_background_raster_failed(generation, key, "PNG decode failed")
            return
        pixmap = QPixmap.fromImage(image)
        self._clear_background_items()
        self._add_background_pixmap(
            pixmap, float(result.get("page_w") or PAGE_W),
            float(result.get("page_h") or PAGE_H),
        )
        self._preview_pixmap_cache[self._preview_cache_key(key)] = (
            pixmap, float(result.get("page_w") or PAGE_W),
            float(result.get("page_h") or PAGE_H),
        )
        self._background_ready = True
        _perf_editor("qpixmap_created", image_started)
        _perf_editor("current_voucher_ready", self._perf_started,
                     voucher_key=key)

    @Slot(int, str, str)
    def _on_background_raster_failed(self, generation: int, key: str, message: str) -> None:
        if self._closing or generation != self._background_load_generation or key != self._current_voucher_key:
            return
        self._clear_background_items()
        self._add_preview_error_background("指図書プレビューの表示に失敗しました。")
        _log.warning("voucher background load failed: %s", message)

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
        size = pdf_page_size(pdf_bytes) or (PAGE_W, PAGE_H)
        if pixmap is not None and not pixmap.isNull():
            self._add_background_pixmap(pixmap, *size)
            return
        else:
            # フォールバック: 白背景
            item: QGraphicsItem = QGraphicsRectItem(0, 0, *size)
            item.setBrush(QBrush(QColor(255, 255, 255)))
            item.setPen(QPen(QColor(200, 200, 200)))
            self._scene.setSceneRect(0, 0, *size)
        self._mark_background(item)
        item.setData(1, self._current_voucher_key)
        self._scene.addItem(item)
        # 背景リストへ参照を保持する（要件3）。
        self._background_items.append(item)
        # 背景読込後にページ全体を編集領域へフィットする（要件2）。
        if getattr(self, "_view", None) is not None:
            self.fit_page_to_view()

    def _add_background_pixmap(
        self, pixmap: QPixmap, page_w: float, page_h: float
    ) -> None:
        """キャッシュPixmapを実ページ寸法へ合わせて背景アイテム化する。"""
        item = QGraphicsPixmapItem(pixmap)
        scale_x = page_w / pixmap.width()
        scale_y = page_h / pixmap.height()
        _log.debug(
            "voucher_edit_background_pixmap PAGE_W=%s PAGE_H=%s pixmap.width=%s "
            "pixmap.height=%s scale_x=%s scale_y=%s",
            page_w, page_h, pixmap.width(), pixmap.height(), scale_x, scale_y,
        )
        item.setPos(0, 0)
        # PDFレンダリングは縦横同倍率。setScaleを使い既存の座標・テスト仕様も維持する。
        item.setScale(scale_x)
        self._scene.setSceneRect(0, 0, page_w, page_h)
        self._update_print_safe_area_guide()
        self._mark_background(item)
        item.setData(1, self._current_voucher_key)
        self._scene.addItem(item)
        self._background_items.append(item)

    def _add_preview_error_background(self, message: str) -> None:
        """別伝票の背景を残さず、明確なエラー背景を表示する。"""
        self._scene.setSceneRect(0, 0, PAGE_W, PAGE_H)
        self._update_print_safe_area_guide()
        item = QGraphicsRectItem(0, 0, PAGE_W, PAGE_H)
        item.setBrush(QBrush(QColor(250, 250, 250)))
        item.setPen(QPen(QColor(190, 190, 190)))
        text_item = QGraphicsSimpleTextItem(message, item)
        text_item.setBrush(QBrush(QColor(180, 40, 40)))
        bounds = text_item.boundingRect()
        text_item.setPos((PAGE_W - bounds.width()) / 2.0,
                         (PAGE_H - bounds.height()) / 2.0)
        self._mark_background(item)
        item.setData(1, self._current_voucher_key)
        item.setData(2, "error")
        self._scene.addItem(item)
        self._background_items.append(item)

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
        if not self._install_preview_background(self._current_voucher_key):
            return

    # ── ツールバー ───────────────────────────────────────────────────────────
    def _apply_undo_redo_icon_theme(self) -> None:
        """現在テーマに合わせて Undo/Redo アイコン（enabled/disabled別色）を適用する（要件8）。

        テーマ変更後も呼び直すことでアイコンを再適用できる。
        """
        dark = current_title_bar_is_dark()
        undo_icon, undo_fallback = make_undo_redo_icon("undo", dark)
        redo_icon, redo_fallback = make_undo_redo_icon("redo", dark)
        if getattr(self, "_undo_action", None) is not None:
            self._undo_action.setIcon(undo_icon)
        if getattr(self, "_redo_action", None) is not None:
            self._redo_action.setIcon(redo_icon)
        self._log_undo_redo_icon_status(undo_fallback, redo_fallback)
        _log.info("voucher_edit_undo_redo_icon_theme_applied %s", {"dark": dark})
        _log.info("voucher_edit_undo_redo_icon_disabled_state_applied %s", {"dark": dark})

    def _apply_toolbar_theme(self) -> None:
        """上部メニュー（ツールバー・コンテナ・図形メニュー）の配色をテーマに合わせる（要件6）。

        ダークテーマでは文字色・背景色を明示して視認性を確保し、ライトテーマは
        従来の見た目を維持する。テーマ変更後も呼び直して再適用できる。
        """
        dark = current_title_bar_is_dark()
        _log.info("voucher_edit_toolbar_theme_detected %s", {"dark": dark})
        # ライト/ダークを明確に分岐する。ライト時は空文字に戻すのではなく明示的な
        # ライト配色で上書きし、直前のダーク配色が残って黒っぽくなるのを防ぐ（要件6）。
        bar = getattr(self, "_main_toolbar", None)
        if bar is not None:
            if dark:
                bar.setStyleSheet(EDIT_TOOLBAR_STYLE + EDIT_TOOLBAR_DARK_STYLE)
            else:
                bar.setStyleSheet(EDIT_TOOLBAR_STYLE + EDIT_TOOLBAR_LIGHT_STYLE)
                _log.info("voucher_edit_toolbar_dark_style_cleared_for_light")
            # 全体テーマの後に dynamic property を再設定して再 polish し、
            # favorite 専用のテーマ色を確実に反映する。
            self._refresh_favorite_font_button_style()
        container = getattr(self, "_main_toolbar_container", None)
        if container is not None:
            bg = EDIT_TOOLBAR_CONTAINER_DARK_BG if dark else EDIT_TOOLBAR_CONTAINER_LIGHT_BG
            # QScrollArea 本体とビューポートの両方へ背景を指定する。ビューポートを
            # 指定しないとライト切替後もダーク背景が残ることがある（要件6）。
            container.setStyleSheet(
                f"QScrollArea#mainEditToolBarContainer {{ background-color: {bg}; border: none; }}"
                f"QScrollArea#mainEditToolBarContainer > QWidget > QWidget {{ background-color: {bg}; }}"
            )
            viewport = container.viewport()
            if viewport is not None:
                viewport.setStyleSheet(f"background-color: {bg};")
        menu = getattr(self, "_shape_menu", None)
        if menu is not None:
            menu.setStyleSheet(
                EDIT_SHAPE_MENU_DARK_STYLE if dark else EDIT_SHAPE_MENU_LIGHT_STYLE
            )
        if dark:
            _log.info("voucher_edit_toolbar_dark_theme_applied %s", {"dark": dark})
        else:
            _log.info("voucher_edit_toolbar_light_theme_applied %s", {"dark": dark})
        _log.info("voucher_edit_toolbar_theme_applied %s", {"dark": dark})
        if hasattr(self, "_multiple_vouchers_notice"):
            self._apply_voucher_count_theme()

    def changeEvent(self, event) -> None:  # noqa: N802
        """テーマ（パレット）変更時に Undo/Redo アイコンを再適用する（要件8）。"""
        try:
            from PySide6.QtCore import QEvent

            if event is not None and event.type() in (
                QEvent.Type.PaletteChange,
                QEvent.Type.ApplicationPaletteChange,
                QEvent.Type.StyleChange,
            ):
                self._apply_undo_redo_icon_theme()
                self._apply_toolbar_theme()
                _log.info("voucher_edit_toolbar_theme_reapplied")
        except Exception:  # noqa: BLE001 - テーマ再適用失敗でUIを落とさない
            pass
        super().changeEvent(event)

    def _log_undo_redo_icon_status(self, undo_fallback: bool, redo_fallback: bool) -> None:
        """Undo/Redoアイコンの読み込み状態をログへ残す（要件4）。"""
        logger = logging.getLogger("tks_to_kintone_app")
        logger.info(
            "voucher_edit_undo_icon_fallback_used"
            if undo_fallback
            else "voucher_edit_undo_icon_loaded"
        )
        logger.info(
            "voucher_edit_redo_icon_fallback_used"
            if redo_fallback
            else "voucher_edit_redo_icon_loaded"
        )

    def _build_toolbar(self) -> None:
        bar = QToolBar("編集ツール")
        bar.setObjectName("mainEditToolBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        self._main_toolbar = bar
        # アンドゥ・リドゥ（曲がった矢印アイコン・全端末でアイコン表示: 要件4）。
        # OS/theme/フォント非依存。SVGが読めない端末では描画フォールバックを使う。
        self._undo_action = bar.addAction("↶", self.undo)
        self._undo_action.setToolTip("元に戻す (Ctrl+Z)")
        self._redo_action = bar.addAction("↷", self.redo)
        self._redo_action.setToolTip("やり直し (Ctrl+Y)")
        # enabled/disabled で色を分けたアイコンを適用する（テーマ再適用にも使う・要件8）。
        self._apply_undo_redo_icon_theme()
        bar.addSeparator()
        # ツール選択（チェック可能にしてハイライト表示する: 要件11）。
        # 選択/テキストは個別ボタン、図形6種は「図形」ボタン1つへまとめる（要件5/7）。
        for label, tool in (("選択", TOOL_SELECT), ("テキスト", TOOL_TEXT)):
            act = bar.addAction(label, lambda t=tool: self.set_tool(t))
            act.setCheckable(True)
            self._tool_actions[tool] = act
            self._style_action_widget(bar, act, "editToolButton", as_property=True)
        self._build_shape_tool_button(bar)
        bar.addSeparator()

        # テキスト書式（お気に入り切替→統合フォント一覧→サイズ→文字装飾）。
        self._favorite_font_button = QToolButton()
        self._favorite_font_button.setObjectName("favoriteFontButton")
        self._favorite_font_button.setCheckable(True)
        self._favorite_font_button.setAutoRaise(True)
        self._favorite_font_button.setFixedWidth(FAVORITE_FONT_BUTTON_WIDTH_PX)
        self._favorite_font_button.clicked.connect(self._toggle_current_font_favorite)
        bar.addWidget(self._favorite_font_button)

        self._font_family_combo = _FontFamilyComboBox(
            self._favorite_fonts, self.current_font_family)
        self._font_family_combo.setMinimumWidth(100)
        self._font_family_combo.setMaximumWidth(130)
        self._font_family_combo.currentFontChanged.connect(
            self._on_font_family_changed)
        bar.addWidget(self._font_family_combo)
        self._sync_favorite_font_controls()

        self._font_size_spin = _FontSizeComboBox()
        self._font_size_spin.setMinimumWidth(48)
        self._font_size_spin.setMaximumWidth(64)
        self._font_size_spin.setToolTip("文字サイズ (4～200pt)")
        self._font_size_spin.setValue(self.current_font_size)
        self._font_size_spin.valueChanged.connect(self._on_font_size_changed)
        bar.addWidget(self._font_size_spin)

        self._text_decoration_menu = QMenu(self)
        self._text_decoration_menu.setObjectName("textDecorationMenu")
        for key, label, shortcut in (
            ("bold", "太字", "Ctrl+B"),
            ("italic", "斜体", "Ctrl+I"),
            ("underline", "下線", "Ctrl+U"),
            ("strikeout", "取り消し線", "Ctrl+5"),
        ):
            action = QAction(label, self)
            action.setObjectName(f"textDecoration_{key}")
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.setShortcutVisibleInContextMenu(True)
            action.triggered.connect(
                lambda checked=False, decoration=key:
                self._on_text_decoration_triggered(decoration, checked))
            self.addAction(action)
            self._text_decoration_menu.addAction(action)
            self._text_decoration_actions[key] = action
        self._text_decoration_button = QToolButton()
        self._text_decoration_button.setObjectName("textDecorationButton")
        self._text_decoration_button.setText("装飾")
        self._text_decoration_button.setToolTip("文字装飾")
        self._text_decoration_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._text_decoration_button.setMenu(self._text_decoration_menu)
        self._text_decoration_button.setMaximumWidth(64)
        bar.addWidget(self._text_decoration_button)
        self._sync_text_decoration_actions()
        bar.addSeparator()

        # 線幅変更UI（要件9）。
        bar.addWidget(QLabel(" 線幅: "))
        self._line_width_spin = QDoubleSpinBox()
        self._line_width_spin.setRange(0.1, 20.0)
        self._line_width_spin.setMaximumWidth(60)
        self._line_width_spin.setSingleStep(0.5)
        self._line_width_spin.setValue(self.current_line_width)
        self._line_width_spin.valueChanged.connect(self._on_line_width_changed)
        bar.addWidget(self._line_width_spin)

        # 反映先テンプレートは左側の縦並びパネルへ表示する（要件5）。ツールバーには置かない。
        # 画像挿入・貼り付け（要件2-3・2-4）。
        insert_action = bar.addAction("画像挿入", self.insert_image_from_file)
        paste_action = bar.addAction("貼り付け", self.paste_image_from_clipboard)
        bar.addSeparator()
        delete_action = bar.addAction("削除", self.delete_selected)
        bar.addSeparator()
        preview_action = bar.addAction("プレビュー", self.preview_unsaved_edits)
        preview_action.setObjectName("previewUnsavedEditsAction")
        self._preview_action = preview_action
        save_action = bar.addAction("保存", self.save)
        # 「座標マーカー」ボタンは通常UIから削除（add_debug_markers は内部・テスト用に残す）。
        save_close_action = bar.addAction("保存して閉じる", self.save_and_close)
        close_action = bar.addAction("閉じる", self.close)
        bar.addSeparator()
        self._print_guide_action = bar.addAction("ガイド", self._toggle_print_safe_area_guide)
        self._print_guide_action.setCheckable(True)
        self._print_guide_action.setChecked(True)
        self._print_guide_action.setToolTip("印刷範囲ガイドを表示/非表示")
        bar.addSeparator()
        # 背景透過中にロックする編集アクション（保存/保存して閉じる/閉じる/画像挿入/
        # 貼り付け/削除/ツール選択）をまとめて保持する（要件2）。
        self._edit_actions = [
            insert_action, paste_action, delete_action,
            preview_action, save_action, save_close_action, close_action,
        ] + list(self._tool_actions.values())
        # 全画面/最大化表示の切り替え（要件2-2）。
        self._fullscreen_action = bar.addAction("全画面", self.toggle_fullscreen)
        # タブレット編集モード（表示先ディスプレイを選んでから大きいUIへ切替）。
        self._tablet_action = bar.addAction("タブレット",
                                            self.prompt_and_enter_tablet_mode)

        # 削除ボタンは赤い警告色、保存系ボタンは安全色にする（要件2-6・2-7・3）。
        self._style_action_widget(bar, delete_action, "dangerButton")
        self._style_action_widget(bar, save_action, "successButton")
        self._style_action_widget(bar, save_close_action, "successButton")
        # ツールバー全体のボタン幅・余白を広げ、警告色/安全色を割り当てる（要件2-5）。
        bar.setStyleSheet(EDIT_TOOLBAR_STYLE)
        container = QScrollArea()
        container.setObjectName("mainEditToolBarContainer")
        container.setWidgetResizable(False)
        container.setWidget(bar)
        container.setFrameShape(QFrame.Shape.NoFrame)
        container.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bar.setMinimumWidth(0)
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        container.setMinimumHeight(max(72, bar.sizeHint().height() + 22))
        self._main_toolbar_container = container
        # ライト/ダークテーマに合わせて上部メニューの配色を適用する（要件6）。
        self._apply_toolbar_theme()
        bar.setMinimumWidth(0)
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_toolbar_scroll_area_enabled"
        )
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_toolbar_content_width %s",
            {"width": bar.sizeHint().width()},
        )
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_scroll_area_enabled %s", {"area": "main_toolbar"}
        )
        logging.getLogger("tks_to_kintone_app").info(
            "app_window_scroll_area_enabled %s",
            {"class": type(self).__name__, "area": "main_toolbar"},
        )
        QTimer.singleShot(0, self._log_toolbar_scroll_metrics)

        # 選択ツールを初期ハイライト。
        self._update_tool_highlight()
        # アンドゥ・リドゥの有効/無効を初期化する（要件1）。
        self._update_undo_redo_buttons()

    def _build_shape_tool_button(self, bar: QToolBar) -> None:
        """図形6種（線/矢印/両矢印/二重線/四角/丸）を「図形」ボタン1つへまとめる（要件5/6/7）。

        - QToolButton「図形」＋ QMenu。押下（InstantPopup）で図形リストを表示する。
        - カーソルが乗ったときも表示する（_ShapeToolButton.enterEvent）。
        - QActionGroup で排他チェック。選択中の図形が分かるようにする。
        """
        shape_button = _ShapeToolButton()
        shape_button.setObjectName("shapeToolButton")
        shape_button.setText("図形")
        shape_button.setProperty("editToolButton", True)
        shape_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        shape_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(shape_button)
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._shape_menu = menu
        self._shape_tool_button = shape_button
        self._shape_action_group = group
        self._shape_tool_labels: dict[str, str] = {}
        for label, tool in _SHAPE_TOOLS:
            act = QAction(label, menu)
            act.setCheckable(True)
            group.addAction(act)
            menu.addAction(act)
            act.triggered.connect(lambda _checked=False, t=tool: self._on_shape_selected(t))
            self._tool_actions[tool] = act
            self._shape_tool_labels[tool] = label
        menu.aboutToShow.connect(
            lambda: _log.info("voucher_edit_shape_menu_opened")
        )
        shape_button.setMenu(menu)
        shape_button.setToolTip("図形を選択（線/矢印/両矢印/二重線/四角/丸）")
        bar.addWidget(shape_button)
        _log.info("voucher_edit_shape_menu_created %s", {"count": len(_SHAPE_TOOLS)})
        _log.info("voucher_edit_header_buttons_compacted %s", {"merged": len(_SHAPE_TOOLS)})

    def _on_shape_selected(self, tool: str) -> None:
        """図形メニューから図形を選んだときにツールを切り替える（要件6）。"""
        _log.info("voucher_edit_shape_selected %s", {"tool": tool})
        self.set_tool(tool)

    def _update_shape_button_display(self) -> None:
        """図形ボタンの表示/チェック状態を現在ツールに合わせる（要件6）。"""
        button = getattr(self, "_shape_tool_button", None)
        if button is None:
            return
        label = getattr(self, "_shape_tool_labels", {}).get(self.current_tool)
        is_shape = label is not None
        if is_shape:
            button.setText(f"図形: {label}")
            button.setToolTip(f"図形: {label}（クリックで変更）")
        else:
            button.setText("図形")
            button.setToolTip("図形を選択（線/矢印/両矢印/二重線/四角/丸）")
        # 図形ツール選択中はボタンをハイライト表示する。
        button.setProperty("shapeActive", is_shape)
        style = button.style()
        if style is not None:
            style.unpolish(button)
            style.polish(button)

    def _log_toolbar_scroll_metrics(self) -> None:
        scroll = getattr(self, "_main_toolbar_container", None)
        toolbar = getattr(self, "_main_toolbar", None)
        if scroll is None or toolbar is None:
            return
        bar = scroll.horizontalScrollBar()
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_toolbar_viewport_width %s",
            {"width": scroll.viewport().width()},
        )
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_toolbar_scrollbar_range %s",
            {
                "minimum": bar.minimum(),
                "maximum": bar.maximum(),
                "content_width": toolbar.width(),
                "viewport_width": scroll.viewport().width(),
            },
        )

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
        QShortcut(
            QKeySequence("Ctrl+Shift+C"), self,
            activated=lambda: self.show_copy_to_vouchers_dialog(selected_only=True))
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
        # 図形ボタンの表示（図形: 線 等）とハイライトを現在ツールに合わせる（要件6）。
        self._update_shape_button_display()

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
        # Undo / Redo（1ストローク=1操作）。タブレット編集でも全端末でアイコン表示（要件4）。
        dark = current_title_bar_is_dark()
        undo_icon, undo_fallback = make_undo_redo_icon("undo", dark)
        redo_icon, redo_fallback = make_undo_redo_icon("redo", dark)
        self._log_undo_redo_icon_status(undo_fallback, redo_fallback)
        tablet_undo = bar.addAction(undo_icon, "元に戻す", self.undo)
        tablet_undo.setToolTip("元に戻す")
        tablet_redo = bar.addAction(redo_icon, "やり直す", self.redo)
        tablet_redo.setToolTip("やり直す")
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
        bar.setMinimumWidth(bar.sizeHint().width())
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
        if self._main_toolbar_container is not None:
            self._main_toolbar_container.hide()
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
        if self._main_toolbar_container is not None:
            self._main_toolbar_container.show()
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

    # ── 線幅・テキスト書式UI ──────────────────────────────────────────────────
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

    def _selected_text_items(self) -> list[QGraphicsItem]:
        return [item for item in self._scene.selectedItems()
                if isinstance(item, (_EditTextItem, _EditSymbolTextItem))
                and not getattr(item, "_COMMON_READONLY", False)]

    def _apply_selected_text_style(self, *, family: str | None = None,
                                   font_size: float | None = None,
                                   bold: bool | None = None,
                                   italic: bool | None = None,
                                   underline: bool | None = None,
                                   strikeout: bool | None = None,
                                   log_event: str) -> None:
        changed = False
        for item in self._selected_text_items():
            before = (item.font_family, item.font_size, item.font_bold,
                      item.font_italic, item.font_underline, item.font_strikeout)
            item.apply_text_style(
                family=family, font_size=font_size, bold=bold, italic=italic,
                underline=underline, strikeout=strikeout)
            after = (item.font_family, item.font_size, item.font_bold,
                     item.font_italic, item.font_underline, item.font_strikeout)
            changed = changed or before != after
        if changed:
            self.refresh_handles()
            self.commit_history()
            self._log_edit_event(log_event, selected_count=len(self._selected_text_items()))

    def _rebuild_font_family_combo(self) -> None:
        """お気に入り設定変更後に統合フォント一覧を再構築する。"""
        combo = getattr(self, "_font_family_combo", None)
        if combo is None:
            return
        current_family = str(getattr(self, "current_font_family", "") or "").strip()
        with QSignalBlocker(combo):
            combo.rebuild(self._favorite_fonts, current_family=current_family)

    def _sync_favorite_font_controls(self, *, family: str | None = None,
                                     mixed: bool = False) -> None:
        """表示中フォントと☆／★を同期する。"""
        button = getattr(self, "_favorite_font_button", None)
        if button is None:
            return
        if family is None:
            family = str(getattr(self, "current_font_family", "") or "").strip()
        else:
            family = str(family or "").strip()
        usable = bool(family) and not mixed
        registered = usable and family in self._favorite_fonts
        with QSignalBlocker(button):
            button.setChecked(registered)
        button.setText("★" if registered else "☆")
        button.setProperty("favorite", "true" if registered else "false")
        self._refresh_favorite_font_button_style()
        button.setToolTip(
            "このフォントをお気に入りから削除"
            if registered else "このフォントをお気に入りに追加"
        )
        button.setEnabled(usable)

    def _refresh_favorite_font_button_style(self) -> None:
        """favorite property を保ったままテーマ別スタイルを即時反映する。"""
        button = getattr(self, "_favorite_font_button", None)
        if button is None:
            return
        favorite = "true" if button.property("favorite") == "true" else "false"
        button.setProperty("favorite", favorite)
        style = button.style()
        if style is not None:
            style.unpolish(button)
            style.polish(button)
        button.update()

    def _toggle_current_font_favorite(self, _checked: bool = False) -> None:
        """現在のフォントをお気に入りへ追加／削除する（Undo対象外）。"""
        family = str(self.current_font_family or "").strip()
        if not family:
            self._sync_favorite_font_controls(mixed=True)
            return
        if family in self._favorite_fonts:
            self._favorite_fonts.remove(family)
            save_favorite_fonts(self._favorite_fonts)
            _log.info("voucher_edit_favorite_font_removed family=%r", family)
        else:
            if family not in _available_font_families():
                _log.warning("voucher_edit_favorite_font_missing family=%r", family)
                self._sync_favorite_font_controls()
                return
            self._favorite_fonts.append(family)
            save_favorite_fonts(self._favorite_fonts)
            _log.info("voucher_edit_favorite_font_added family=%r", family)
        self._rebuild_font_family_combo()
        self._sync_favorite_font_controls()

    def _on_font_family_changed(self, font: QFont) -> None:
        family = resolve_text_font_family(font.family())
        self.current_font_family = family
        self._sync_favorite_font_controls(family=family)
        if self._updating_property_ui:
            return
        self._apply_selected_text_style(
            family=family, log_event="voucher_edit_text_font_changed")

    def _on_font_size_changed(self, value: float) -> None:
        self.current_font_size = float(value)
        if self._updating_property_ui:
            return
        self._apply_selected_text_style(
            font_size=self.current_font_size,
            log_event="voucher_edit_text_size_changed")

    def _on_font_bold_changed(self, checked: bool) -> None:
        """旧内部API互換。太字QActionと同じ共通処理へ委譲する。"""
        self._on_text_decoration_triggered("bold", checked)

    @staticmethod
    def _decoration_attribute(decoration: str) -> str:
        return {
            "bold": "font_bold",
            "italic": "font_italic",
            "underline": "font_underline",
            "strikeout": "font_strikeout",
        }[decoration]

    @staticmethod
    def _decoration_label(decoration: str) -> str:
        return {
            "bold": "太字",
            "italic": "斜体",
            "underline": "下線",
            "strikeout": "取り消し線",
        }[decoration]

    def _on_text_decoration_triggered(self, decoration: str, checked: bool) -> None:
        """ツールバー・右クリック・ショートカット共通の装飾切替処理。"""
        if decoration not in self._text_decoration_actions:
            return
        # 混在状態では最初のクリックで選択テキストを全件ONにする。
        target = True if decoration in self._text_decoration_mixed else bool(checked)
        setattr(self, f"current_font_{decoration}", target)
        if not self._updating_property_ui:
            kwargs = {decoration: target}
            self._apply_selected_text_style(
                **kwargs,
                log_event=f"voucher_edit_text_{decoration}_changed")
        self._sync_text_decoration_actions()

    def _sync_text_decoration_actions(
        self, text_items: list[QGraphicsItem] | None = None
    ) -> None:
        """現在または選択テキストの装飾状態をQActionへ同期する。"""
        actions = getattr(self, "_text_decoration_actions", {})
        if not actions:
            return
        if text_items is None:
            text_items = self._selected_text_items()
        mixed: set[str] = set()
        for decoration, action in actions.items():
            attr = self._decoration_attribute(decoration)
            if text_items:
                values = {bool(getattr(item, attr, False)) for item in text_items}
                is_mixed = len(values) > 1
                checked = next(iter(values)) if len(values) == 1 else False
            else:
                is_mixed = False
                checked = bool(getattr(self, f"current_font_{decoration}", False))
            if is_mixed:
                mixed.add(decoration)
            with QSignalBlocker(action):
                action.setChecked(checked)
                action.setText(
                    f"－ {self._decoration_label(decoration)}"
                    if is_mixed else self._decoration_label(decoration))
        self._text_decoration_mixed = mixed

    # ── 編集レイヤー操作 ─────────────────────────────────────────────────────
    def edit_items(self) -> list[QGraphicsItem]:
        """編集レイヤー（保存対象）のアイテム一覧。背景・ハンドルは除く（要件7）。"""
        result: list[QGraphicsItem] = []
        for it in self._scene.items():
            # 背景・補助・一時プレビューは絶対に編集レイヤーへ含めない（要件1・5・7・11）。
            if (getattr(it, "_BG_MARK", False) or getattr(it, "_IS_HELPER", False)
                    or getattr(it, "_IS_PREVIEW", False)
                    or getattr(it, "_COMMON_READONLY", False)):
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
            if getattr(it, "_PRINT_GUIDE", False):
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
        """指定が無ければ次回作成用の反映先を使う。"""
        if target_vouchers is None:
            return list(self._creation_target_vouchers)
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
        self._creation_target_vouchers = list(template["target_vouchers"])
        self._creation_template_key = str(template.get("key") or "")
        self._creation_template_name = str(template["name"])
        if self.tablet_mode:
            layer = self.current_freehand_layer()
            if layer is not None:
                layer.target_vouchers = list(template["target_vouchers"])
                self.mark_dirty()
                self.commit_history()
        self._update_template_highlight()
        self.ensure_background_visible()

    @staticmethod
    def _previous_left_pane_width_for_scale(scale: float) -> int:
        """前回（+40px時点）の左ペイン幅マッピング（190/240/260）。ログの拡張量算出用。"""
        try:
            value = float(scale)
        except (TypeError, ValueError):
            value = 1.0
        if value >= 1.5:
            return 260
        if value >= 1.25:
            return 240
        return 190

    def _left_pane_width(self) -> int:
        """左ペイン幅を表示倍率から決める（125%以上で広げる・要件9）。

        今回さらに約1.5cm（+60px）広げた（190/240/260 → 250/300/320）。
        """
        scale = get_display_scale(self)
        width = left_pane_width_for_scale(scale, base_width=LEFT_PANE_BASE_WIDTH)
        previous_width = self._previous_left_pane_width_for_scale(scale)
        _log.info(
            "voucher_edit_left_pane_width_increased_again %s",
            {
                "scale": round(scale, 3),
                "old": previous_width,
                "new": width,
                "delta_px": width - previous_width,
            },
        )
        _log.info("voucher_edit_left_pane_width_old %s", {"width": previous_width})
        _log.info("voucher_edit_left_pane_width_new %s", {"width": width})
        _log.info(
            "voucher_edit_left_pane_width_delta_px %s", {"delta_px": width - previous_width}
        )
        _log.info(
            "voucher_edit_left_pane_width_by_dpi %s",
            {"scale": round(scale, 3), "width": width},
        )
        return width

    def _build_template_panel(self) -> QWidget:
        """反映先テンプレートを左側に縦並びで表示するパネルを作る（要件5）。"""
        panel = QWidget()
        panel.setObjectName("templatePanel")
        width = self._left_pane_width()
        panel.setFixedWidth(width)
        _log.info("voucher_edit_left_pane_width_applied %s", {"width": width})
        layout = QVBoxLayout(panel)
        # 縦スクロールバー（AsNeeded）が出ても左ペイン内ボタンが重ならないよう、
        # 内側レイアウトの右マージンにスクロールバー幅＋視認用ギャップを加える（要件3）。
        # パネル幅（250/300/320）は維持し、ボタンはこの右余白の手前までに収める。
        scrollbar_width = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        right_padding = scrollbar_width + LEFT_PANE_RIGHT_GAP_PX
        right_margin = LEFT_PANE_BASE_MARGIN_PX + right_padding
        layout.setContentsMargins(
            LEFT_PANE_BASE_MARGIN_PX, LEFT_PANE_BASE_MARGIN_PX, right_margin, LEFT_PANE_BASE_MARGIN_PX
        )
        layout.setSpacing(5)
        self._left_pane_content_right_margin = right_margin
        _log.info("voucher_edit_left_pane_scrollbar_width %s", {"scrollbar_width": scrollbar_width})
        _log.info(
            "voucher_edit_left_pane_right_padding_applied %s",
            {"right_padding": right_padding, "right_margin": right_margin},
        )
        _log.info(
            "voucher_edit_left_pane_content_margins %s",
            {
                "left": LEFT_PANE_BASE_MARGIN_PX,
                "top": LEFT_PANE_BASE_MARGIN_PX,
                "right": right_margin,
                "bottom": LEFT_PANE_BASE_MARGIN_PX,
            },
        )
        _log.info(
            "voucher_edit_left_pane_button_width_adjusted %s",
            {"content_width": max(0, width - LEFT_PANE_BASE_MARGIN_PX - right_margin)},
        )
        # 「反映先」見出しの横に 現在数/最大数 を表示する（要件2）。
        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("反映先")
        hf = heading.font()
        hf.setBold(True)
        heading.setFont(hf)
        heading_row.addWidget(heading)
        self._reflect_count_label = QLabel("")
        heading_row.addWidget(self._reflect_count_label)
        heading_row.addStretch(1)
        layout.addLayout(heading_row)
        # パネル全体の縦レイアウト（お気に入り一覧などを含む。既存参照を維持）。
        self._template_panel_layout = layout
        # 反映先ボタンの表示枠。登録数に関わらず最大8個分の固定高さを確保する（要件3）。
        # 0/4/8個いずれでも高さが変わらないよう、末尾に stretch を入れて余白を埋める。
        reflect_container = QWidget(panel)
        reflect_container.setObjectName("reflectListContainer")
        reflect_container.setFixedHeight(REFLECT_LIST_FIXED_HEIGHT)
        reflect_layout = QVBoxLayout(reflect_container)
        reflect_layout.setContentsMargins(0, 0, 0, 0)
        reflect_layout.setSpacing(REFLECT_LIST_SPACING)
        self._reflect_list_container = reflect_container
        self._reflect_list_layout = reflect_layout
        self._reflect_list = _ReflectionTargetListWidget(reflect_container)
        self._reflect_list.setObjectName("reflectionTargetList")
        self._reflect_list.setUniformItemSizes(True)
        self._reflect_list.setSpacing(REFLECT_LIST_SPACING)
        self._reflect_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._reflect_list.customContextMenuRequested.connect(
            self._show_reflection_list_context_menu
        )
        self._reflect_list.orderChanged.connect(self._on_reflection_order_changed)
        reflect_layout.addWidget(self._reflect_list)
        self._template_actions = {}
        self._template_default_labels: dict[str, QLabel] = {}
        self._template_button_group = QButtonGroup(panel)
        self._template_button_group.setExclusive(True)
        for tpl in self._templates:
            self._add_template_action(tpl)
        layout.addWidget(reflect_container)
        # 任意のテンプレートを登録する（要件4）。反映先表示枠の外（下）に置く。
        self._register_template_button = QPushButton("＋ テンプレ登録")
        self._register_template_button.clicked.connect(self._on_register_template)
        layout.addWidget(self._register_template_button)
        self._image_actions_label = None
        self._binarize_button = None
        self._threshold_transparent_button = None
        self._restore_image_button = None
        self._threshold_settings_button = None

        # 「お気に入り」見出しの横に 現在数/最大数 を表示する（要件3）。
        favorite_row = QHBoxLayout()
        favorite_row.setContentsMargins(0, 0, 0, 0)
        favorite_heading = QLabel("お気に入り")
        ff = favorite_heading.font()
        ff.setBold(True)
        favorite_heading.setFont(ff)
        favorite_heading.setStyleSheet("margin-top: 8px;")
        favorite_row.addWidget(favorite_heading)
        self._favorite_count_label = QLabel("")
        self._favorite_count_label.setStyleSheet("margin-top: 8px;")
        favorite_row.addWidget(self._favorite_count_label)
        favorite_row.addStretch(1)
        layout.addLayout(favorite_row)
        self._favorite_list = _FavoriteListWidget(panel)
        self._favorite_list.setObjectName("favoriteObjectList")
        self._favorite_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._favorite_list.customContextMenuRequested.connect(self._show_favorite_context_menu)
        self._favorite_list.orderChanged.connect(self._on_favorite_order_changed)
        self._favorite_list.itemDoubleClicked.connect(
            lambda item: self.drop_favorite_object(
                str(item.data(Qt.ItemDataRole.UserRole) or ""),
                QPointF(PAGE_W / 2.0, PAGE_H / 2.0),
            )
        )
        # お気に入り表示枠の縦幅を固定し、最大20件を常に表示する（要件4）。
        # 1件あたりの高さを小さくし、画像プレビューで縦に伸びないようにする。
        self._favorite_list.setUniformItemSizes(True)
        self._favorite_list.setIconSize(QSize(0, 0))
        self._favorite_list.setFixedHeight(FAVORITE_LIST_FIXED_HEIGHT)
        layout.addWidget(self._favorite_list)
        self._refresh_favorite_list()
        layout.addStretch(1)
        self._update_template_highlight()
        self._update_reflect_count_label()
        return panel

    def _build_left_pane_scroll_area(self, panel: QWidget) -> QScrollArea:
        """通常左ペイン全体を縦スクロール可能にする。"""
        scroll = QScrollArea()
        scroll.setObjectName("templatePanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFixedWidth(panel.width() or self._left_pane_width())
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_left_pane_scroll_area_enabled"
        )
        QTimer.singleShot(0, self._log_left_pane_scroll_range)
        return scroll

    def _log_left_pane_scroll_range(self) -> None:
        scroll = getattr(self, "_template_panel_scroll", None)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        logging.getLogger("tks_to_kintone_app").info(
            "voucher_edit_left_pane_scroll_range %s",
            {
                "minimum": bar.minimum(),
                "maximum": bar.maximum(),
                "page_step": bar.pageStep(),
            },
        )

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
        """画像選択に応じて閾値処理UIの表示状態を更新する。"""
        image = self._selected_image_item()
        widgets = (
            getattr(self, "_image_actions_label", None),
            getattr(self, "_binarize_button", None),
            getattr(self, "_threshold_transparent_button", None),
            getattr(self, "_restore_image_button", None),
            getattr(self, "_threshold_settings_button", None),
        )
        if any(widget is None for widget in widgets):
            return
        show = image is not None
        for widget in widgets:
            widget.setVisible(show)
        if show:
            self._binarize_button.setEnabled(True)
            self._threshold_transparent_button.setEnabled(True)
            self._threshold_settings_button.setEnabled(True)
            self._restore_image_button.setEnabled(image.has_original_image())

    def set_debug_visible(self, visible: bool) -> None:
        """デバッグ表示状態を切り替え、画像編集ボタンの表示を更新する（テスト/設定変更用）。"""
        self._debug_visible = bool(visible)
        self._update_image_action_buttons()

    def _on_restore_image(self) -> None:
        """選択中画像を加工前へ復元する（要件4・10）。位置・サイズ・選択状態は維持する。"""
        if self._closing or self._close_in_progress:
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

        mode は "binarize"（二値化）/"threshold_transparent"（閾値透過）。位置・サイズ・倍率・
        選択状態は維持し、加工前の元画像は _EditImageItem 側で一度だけ退避する（要件6・7・10）。
        """
        if self._closing or self._close_in_progress:
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

    def _template_by_key(self, key: str) -> dict[str, Any] | None:
        for tpl in self._templates:
            if str(tpl.get("key") or "") == str(key or ""):
                return tpl
        return None

    def _load_default_reflection_target_key(self) -> str:
        """保存済みstable keyを検証し、存在しなければstandardへフォールバックする。"""
        saved = _settings_string(
            DEFAULT_REFLECTION_TARGET_KEY, FALLBACK_REFLECTION_TARGET_KEY
        )
        if self._template_by_key(saved) is not None:
            _log.info(
                "event=voucher_edit_default_reflection_target_loaded key=%s", saved
            )
            return saved
        _log.warning(
            "event=voucher_edit_default_reflection_target_missing "
            "missing_key=%s fallback=%s",
            saved, FALLBACK_REFLECTION_TARGET_KEY,
        )
        _log.warning(
            "event=voucher_edit_default_reflection_target_fallback "
            "invalid_key=%s fallback=%s",
            saved, FALLBACK_REFLECTION_TARGET_KEY,
        )
        _save_settings_string(
            DEFAULT_REFLECTION_TARGET_KEY, FALLBACK_REFLECTION_TARGET_KEY
        )
        return FALLBACK_REFLECTION_TARGET_KEY

    def set_default_reflection_target(self, stable_key: str) -> bool:
        """既定stable keyを保存する。編集データとUndo履歴には触れない。"""
        template = self._template_by_key(stable_key)
        if template is None:
            return False
        old_key = self._default_reflection_target_key
        new_key = str(template["key"])
        if old_key != new_key and not _save_settings_string(
                DEFAULT_REFLECTION_TARGET_KEY, new_key):
            return False
        self._default_reflection_target_key = new_key
        # 未選択なら、次回作成用の現在値も新しい既定へ合わせる。
        selected = [
            item for item in self._scene.selectedItems()
            if hasattr(item, "serialize_edit_object")
        ]
        if not selected:
            self._on_template_selected(template)
        self._refresh_default_reflection_indicators()
        _log.info(
            "event=voucher_edit_default_reflection_target_changed "
            "old_key=%s new_key=%s",
            old_key, new_key,
        )
        return True

    def _ordered_templates(
        self, templates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """反映処理の内容を変えず、stable key の保存順だけを表示へ適用する。"""
        default_keys = [str(tpl.get("key") or "") for tpl in templates]
        order = _normalized_saved_order(
            _settings_string_list(REFLECTION_TARGET_ORDER_KEY),
            default_keys,
            setting_key=REFLECTION_TARGET_ORDER_KEY,
        )
        by_key = {str(tpl.get("key") or ""): tpl for tpl in templates}
        return [by_key[key] for key in order]

    def _template_order_keys(self) -> list[str]:
        return [str(tpl.get("key") or "") for tpl in self._templates]

    def _on_reflection_order_changed(self, keys: list[str]) -> None:
        """リストの移動結果を表示モデルとQSettingsへ反映（Undo対象外）。"""
        by_key = {str(tpl.get("key") or ""): tpl for tpl in self._templates}
        normalized = _normalized_saved_order(
            keys, self._template_order_keys(),
            setting_key=REFLECTION_TARGET_ORDER_KEY,
        )
        self._templates = [by_key[key] for key in normalized]
        _save_settings_string_list(REFLECTION_TARGET_ORDER_KEY, normalized)
        self._reflect_list._log_reorder("order_saved", order=normalized)
        self._rebuild_reflection_list()
        self._reload_tablet_reflect_panel()
        self._update_template_highlight()

    def reset_reflection_target_order(self) -> None:
        """反映先を load_templates() の正式な既定順へ戻す。"""
        defaults = load_templates()
        _save_settings_string_list(
            REFLECTION_TARGET_ORDER_KEY,
            [str(tpl.get("key") or "") for tpl in defaults],
        )
        self._templates = defaults
        self._rebuild_reflection_list()
        self._reload_tablet_reflect_panel()
        self._update_template_highlight()

    def _show_reflection_list_context_menu(self, pos) -> None:
        item = self._reflect_list.itemAt(pos)
        stable_key = (
            str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        )
        menu = QMenu(self)
        default_action = None
        if stable_key:
            default_action = self._add_default_reflection_action(menu, stable_key)
            menu.addSeparator()
        reset_action = menu.addAction("反映先を既定順に戻す")
        chosen = menu.exec(self._reflect_list.mapToGlobal(pos))
        if default_action is not None and chosen is default_action:
            self.set_default_reflection_target(stable_key)
        elif chosen is reset_action:
            self.reset_reflection_target_order()

    def _add_default_reflection_action(
        self, menu: QMenu, stable_key: str
    ) -> QAction:
        """反映先一覧専用の「既定に設定」アクションを構築する。"""
        action = menu.addAction("既定に設定")
        action.setCheckable(True)
        is_default = stable_key == self._default_reflection_target_key
        action.setChecked(is_default)
        action.setEnabled(not is_default)
        action.setToolTip(
            "新しく作成するオブジェクトの初期反映先として使用します。"
        )
        return action

    def _add_template_action(self, tpl: dict[str, Any]) -> None:
        """テンプレートボタンを1つ縦並びパネルへ追加する（名前ルックアップで上書きにも追従）。"""
        name = tpl["name"]
        locked = is_locked_template(name)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, str(tpl.get("key") or ""))
        item.setSizeHint(QSize(0, REFLECT_LIST_ITEM_HEIGHT))
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        self._reflect_list.addItem(item)
        # 固定テンプレートは表示だけロックバッヂを付ける（内部キー name は不変: 要件3・10）。
        row_widget = QWidget(self._reflect_list)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)
        handle = _ReorderDragHandle(self._reflect_list, item)
        handle.setObjectName("reflectionReorderHandle")
        row_layout.addWidget(handle)
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
        # 反映先表示枠の高さを一定に保つため、1件あたりの高さを固定する（要件3）。
        btn.setFixedHeight(REFLECT_LIST_ITEM_HEIGHT)
        row_layout.addWidget(btn, 1)
        default_label = QLabel("既定" if (
            str(tpl.get("key") or "") == self._default_reflection_target_key
        ) else "")
        default_label.setObjectName("reflectionDefaultIndicator")
        default_label.setProperty("reflectionDefaultIndicator", True)
        default_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        default_label.setToolTip(
            "新しく作成するオブジェクトの初期反映先として使用します。"
        )
        font = default_label.font()
        font.setPointSize(max(8, font.pointSize() - 1))
        font.setBold(True)
        default_label.setFont(font)
        default_label.setMinimumWidth(30)
        row_layout.addWidget(default_label)
        self._reflect_list.setItemWidget(item, row_widget)
        self._template_actions[tpl["name"]] = btn
        self._template_default_labels[str(tpl.get("key") or "")] = default_label

    def _rebuild_reflection_list(self) -> None:
        selected_name = self._current_template_name
        with QSignalBlocker(self._reflect_list):
            for btn in list(self._template_actions.values()):
                self._template_button_group.removeButton(btn)
                btn.deleteLater()
            self._template_actions = {}
            self._template_default_labels = {}
            self._reflect_list.clear()
            for tpl in self._templates:
                self._add_template_action(tpl)
        self._current_template_name = selected_name
        self._update_template_highlight()

    # ── 反映先テンプレートの編集/削除（要件1）──────────────────────────────────────
    def _show_template_context_menu(self, name: str, global_pos) -> None:
        """反映先行の右クリックメニューを表示する。"""
        tpl = self._template_by_name(name)
        if tpl is None:
            return
        menu = QMenu(self)
        stable_key = str(tpl.get("key") or "")
        default_action = self._add_default_reflection_action(menu, stable_key)
        menu.addSeparator()
        reset_action = menu.addAction("反映先を既定順に戻す")
        edit_action = delete_action = None
        if not is_locked_template(name):
            menu.addSeparator()
            edit_action = menu.addAction("編集")
            delete_action = menu.addAction("削除")
        chosen = menu.exec(global_pos)
        if chosen is default_action:
            self.set_default_reflection_target(stable_key)
        elif chosen is reset_action:
            self.reset_reflection_target_order()
        elif edit_action is not None and chosen is edit_action:
            self._edit_template(name)
        elif delete_action is not None and chosen is delete_action:
            self._delete_template(name)

    def _refresh_default_reflection_indicators(self) -> None:
        """並び替え・テーマ変更後もstable keyに対して既定表示を付ける。"""
        for key, label in getattr(self, "_template_default_labels", {}).items():
            label.setText("既定" if key == self._default_reflection_target_key else "")
            label.update()

    def _reload_templates_panel(self, select_name: str | None = None) -> None:
        """テンプレート一覧を再読み込みしてボタンを作り直す（編集/削除後の再描画）。"""
        self._templates = self._ordered_templates(load_templates())
        if self._template_by_key(self._creation_template_key) is None:
            standard = self._template_by_key(FALLBACK_REFLECTION_TARGET_KEY)
            if standard is not None:
                self._creation_target_vouchers = list(standard["target_vouchers"])
                self._creation_template_key = str(standard["key"])
                self._creation_template_name = str(standard["name"])
                if not self._scene.selectedItems():
                    self.current_target_vouchers = list(standard["target_vouchers"])
                    self._current_template_name = str(standard["name"])
        if self._template_by_key(self._default_reflection_target_key) is None:
            missing_key = self._default_reflection_target_key
            self._default_reflection_target_key = FALLBACK_REFLECTION_TARGET_KEY
            _save_settings_string(
                DEFAULT_REFLECTION_TARGET_KEY, FALLBACK_REFLECTION_TARGET_KEY
            )
            _log.warning(
                "event=voucher_edit_default_reflection_target_missing "
                "missing_key=%s fallback=%s",
                missing_key, FALLBACK_REFLECTION_TARGET_KEY,
            )
            _log.warning(
                "event=voucher_edit_default_reflection_target_fallback "
                "invalid_key=%s fallback=%s",
                missing_key, FALLBACK_REFLECTION_TARGET_KEY,
            )
            if not self._scene.selectedItems():
                standard = self._template_by_key(FALLBACK_REFLECTION_TARGET_KEY)
                if standard is not None:
                    self._creation_target_vouchers = list(standard["target_vouchers"])
                    self._creation_template_key = str(standard["key"])
                    self._creation_template_name = str(standard["name"])
                    self.current_target_vouchers = list(standard["target_vouchers"])
                    self._current_template_name = str(standard["name"])
        self._rebuild_reflection_list()
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
        # 反映先の件数表示を更新する（要件2）。
        self._update_reflect_count_label()

    def _reflect_template_count(self) -> int:
        """登録済みの反映先テンプレート件数（組み込み＋ユーザー定義）を返す（要件2）。"""
        return len(self._templates)

    def _update_reflect_count_label(self) -> None:
        """「反映先」見出し横の N/8 表示を更新する（要件2）。"""
        label = getattr(self, "_reflect_count_label", None)
        if label is not None:
            label.setText(f"{self._reflect_template_count()}/{MAX_REFLECT_TEMPLATES}")

    def _update_favorite_count_label(self) -> None:
        """「お気に入り」見出し横の N/15 表示を更新する（要件3）。"""
        label = getattr(self, "_favorite_count_label", None)
        if label is not None:
            label.setText(f"{len(self._favorites)}/{MAX_FAVORITE_OBJECTS}")

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
        updated["key"] = str(tpl.get("key") or "")
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
        - 削除したテンプレートを使っていた既存オブジェクトは変更しない。
        - 次回作成用または保存済み既定なら標準へフォールバックする。
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
        try:
            if not delete_template(name):
                logging.warning("テンプレート「%s」は削除できませんでした。", name)
                return
        except Exception as exc:
            QMessageBox.critical(self, "テンプレート削除エラー", f"テンプレートの削除に失敗しました:\n{exc}")
            return
        # テンプレート削除は既存オブジェクトの保存値・編集JSON・履歴を変更しない。
        self._reload_templates_panel(select_name=self._creation_template_name)

    # ── オブジェクト右クリックメニュー ───────────────────────────────────────
    @staticmethod
    def _item_is_alive(item: QGraphicsItem | None) -> bool:
        """item の背後の C++ オブジェクトがまだ生きているか安全に判定する。

        Undo/Redo・削除・背景透過スレッド完了などで item が破棄されると、
        メソッド呼び出し時に RuntimeError（wrapped C/C++ object deleted）が
        発生してアプリが落ちる。ここで検証し、死んでいれば処理を中止する。
        """
        if item is None:
            return False
        try:
            import shiboken6

            if not shiboken6.isValid(item):
                return False
        except Exception:
            pass
        try:
            # 破棄済みなら属性アクセスで RuntimeError になる。
            item.scene()
        except RuntimeError:
            return False
        except Exception:
            return False
        return True

    def _object_action_allowed(self, item: QGraphicsItem | None) -> bool:
        """右クリックメニューの対象として扱ってよい item か検証する。"""
        if not self._item_is_alive(item):
            return False
        if not hasattr(item, "serialize_edit_object"):
            return False
        return True

    def _edit_item_by_id(self, obj_id: str | None):
        """obj_id から現在シーン上の編集オブジェクトを再解決する。"""
        if not obj_id:
            return None
        for it in self.edit_items():
            if getattr(it, "obj_id", None) == obj_id:
                return it
        return None

    def _run_object_action(self, obj_id: str | None, func, *, name: str = "") -> None:
        """obj_id を毎回再解決し、生存確認したうえで func(item) を安全に実行する。

        QAction の callback は生成時点の item を掴んだままメニュー表示中に item が
        破棄されうる。実行時に id から再解決し、死んでいれば中止、例外はログして
        握りつぶすことでアプリを落とさない。
        """
        try:
            item = self._edit_item_by_id(obj_id)
            if not self._object_action_allowed(item):
                self._log_edit_event(
                    "voucher_edit_context_menu_target_invalid",
                    action=name, obj_id=obj_id)
                return
            func(item)
        except Exception as exc:  # noqa: BLE001 - メニュー操作でアプリを落とさない
            self._log_edit_event(
                "voucher_edit_context_menu_action_failed",
                action=name, obj_id=obj_id, error=repr(exc))

    def _show_object_context_menu(self, item: QGraphicsItem, global_pos) -> None:
        if not self._object_action_allowed(item):
            self._log_edit_event("voucher_edit_context_menu_target_invalid",
                                 reason="show")
            return
        menu = self._build_object_context_menu(item)
        menu.exec(global_pos)

    def _build_object_context_menu(self, item: QGraphicsItem) -> QMenu:
        """編集オブジェクト用の右クリックメニューを生成する。

        QAction の callback は生成時点の item を直接掴まず、安定した obj_id を
        キャプチャして実行時に再解決する（_run_object_action）。これによりメニュー
        表示中に item が破棄されても（削除・Undo/Redo・背景透過スレッド完了）、
        wrapped C/C++ object deleted によるクラッシュを防ぐ。
        """
        obj_id = getattr(item, "obj_id", None)
        menu = QMenu(self)
        menu._submenus = []  # type: ignore[attr-defined]
        copy_action = menu.addAction("コピー")
        copy_action.setObjectName("copy_action")
        copy_action.triggered.connect(
            lambda checked=False, oid=obj_id: self._run_object_action(
                oid, self.copy_object, name="copy")
        )
        # 複製（コピー＋オフセット貼り付けと同じ結果。Undo/Redo対象: 要件1）。
        duplicate_action = menu.addAction("複製")
        duplicate_action.setObjectName("duplicate_action")
        duplicate_action.setEnabled(hasattr(item, "serialize_edit_object"))
        duplicate_action.triggered.connect(
            lambda checked=False, oid=obj_id: self._run_object_action(
                oid, self.duplicate_object, name="duplicate")
        )
        if isinstance(item, (_EditTextItem, _EditSymbolTextItem)):
            edit_action = menu.addAction("編集")
            edit_action.setObjectName("edit_text_action")
            editable = not (
                bool(getattr(item, "_COMMON_READONLY", False))
                or bool(getattr(item, "locked", False))
            )
            edit_action.setEnabled(editable)
            edit_action.triggered.connect(
                lambda checked=False, oid=obj_id: self._run_object_action(
                    oid, self.begin_text_edit, name="edit_text")
            )
        favorite_action = menu.addAction("オブジェクトをお気に入り登録")
        favorite_action.setObjectName("favorite_add_action")
        favorite_action.triggered.connect(
            lambda checked=False, oid=obj_id: self._run_object_action(
                oid, self.add_object_to_favorites, name="favorite")
        )
        menu.addSeparator()
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
                    lambda checked=False, oid=obj_id, w=width: self._run_object_action(
                        oid, lambda it: self._set_object_line_width(it, w),
                        name="line_width")
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
                    lambda checked=False, oid=obj_id, s=size: self._run_object_action(
                        oid, lambda it: self._set_object_font_size(it, s),
                        name="font_size")
                )

        if isinstance(item, (_EditTextItem, _EditSymbolTextItem)):
            decoration_menu = QMenu("文字装飾", menu)
            decoration_menu.setObjectName("text_decoration_context_menu")
            self._sync_text_decoration_actions()
            decoration_menu.addActions(list(self._text_decoration_actions.values()))
            menu.addMenu(decoration_menu)
            menu._submenus.append(decoration_menu)  # type: ignore[attr-defined]

        delete_action = menu.addAction("削除")
        delete_action.setObjectName("delete_action")
        delete_action.triggered.connect(
            lambda checked=False, oid=obj_id: self._run_object_action(
                oid, self._delete_object, name="delete")
        )

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
                lambda checked=False, oid=obj_id, t=tpl: self._run_object_action(
                    oid, lambda it: self._set_object_target_vouchers(
                        it, list(t["target_vouchers"])),
                    name="target_vouchers")
            )

        if isinstance(item, _EditImageItem):
            image_menu = QMenu("画像処理", menu)
            image_menu.setObjectName("image_processing_menu")
            menu.addMenu(image_menu)
            menu._submenus.append(image_menu)  # type: ignore[attr-defined]

            binarize_action = image_menu.addAction("二値化")
            binarize_action.setObjectName("binarize_action")
            binarize_action.triggered.connect(
                lambda checked=False, oid=obj_id: self._run_object_action(
                    oid, self._run_binarize_for_item, name="binarize")
            )
            transparent_action = image_menu.addAction(TRANSPARENT_THRESHOLD_LABEL.replace("\n", " "))
            transparent_action.setObjectName("transparent_background_action")
            transparent_action.triggered.connect(
                lambda checked=False, oid=obj_id: self._run_object_action(
                    oid, self._run_threshold_transparency_for_item,
                    name="threshold_transparency")
            )
            restore_action = image_menu.addAction("背景を戻す")
            restore_action.setObjectName("restore_background_action")
            restore_action.setEnabled(item.has_original_image())
            restore_action.triggered.connect(
                lambda checked=False, oid=obj_id: self._run_object_action(
                    oid, self._restore_image_item, name="restore_background")
            )
            settings_action = image_menu.addAction("閾値設定")
            settings_action.setObjectName("threshold_settings_action")
            settings_action.triggered.connect(self._on_threshold_settings)
        self._log_edit_event(
            "voucher_edit_context_menu_built",
            obj_id=obj_id,
            object_type=type(item).__name__,
            is_image=isinstance(item, _EditImageItem))
        return menu

    def begin_text_edit(self, item: QGraphicsItem) -> bool:
        """ダブルクリック／右クリックから共用するインライン編集開始処理。"""
        clicked_type = type(item).__name__
        if not isinstance(item, (_EditTextItem, _EditSymbolTextItem)):
            return False
        if (bool(getattr(item, "_COMMON_READONLY", False))
                or bool(getattr(item, "locked", False))):
            return False
        # 3文字以下は保存時に中心アンカー型の symbol_text へ変換される。
        # QGraphicsSimpleTextItem はインライン編集できないため、同じ id・中心・書式を
        # 保った通常テキストへ戻してから、長い文字列と同じ編集経路へ載せる。
        if isinstance(item, _EditSymbolTextItem):
            item = self._convert_symbol_to_editable_text(item)
        self._select_only(item)
        item._inline_edit_original_text = item.toPlainText()
        item._inline_edit_cancelled = False
        # 入力中だけ十分な文書幅と透明なフォーカス領域を確保する。box_w/box_h、
        # 保存座標、font、PDF出力値は一切変更しない。
        edit_w = scene_units_for_view_pixels(
            self._scene, TEXT_EDIT_MIN_WIDTH_PX, maximum=120.0)
        edit_h = scene_units_for_view_pixels(
            self._scene, TEXT_EDIT_MIN_HEIGHT_PX, maximum=60.0)
        item._inline_edit_text_width = item.textWidth()
        item.setTextWidth(max(float(item.textWidth()), edit_w))
        actual = super(_EditTextItem, item).boundingRect()
        edit_rect = QRectF(actual)
        edit_rect.setWidth(max(actual.width(), edit_w))
        edit_rect.setHeight(max(actual.height(), edit_h))
        edit_rect.moveCenter(actual.center())
        item._inline_edit_min_rect = edit_rect
        item.prepareGeometryChange()
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        item.setFocus(Qt.FocusReason.MouseFocusReason)
        self._scene.setFocusItem(item, Qt.FocusReason.MouseFocusReason)
        cursor = item.textCursor()
        cursor.clearSelection()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        item.setTextCursor(cursor)
        # 非表示windowではQtがhasFocus()をFalseのままにするが、focusItemは設定済み。
        # 実画面ではhasFocus()、ヘッドレス/生成直後ではscene.focusItem()で検証する。
        focus_ready = item.hasFocus() or self._scene.focusItem() is item
        ready = bool(
            focus_ready
            and item.textInteractionFlags() & Qt.TextInteractionFlag.TextEditorInteraction
            and not item.textCursor().isNull())
        self._log_edit_event(
            "voucher_text_edit_begin",
            object_id=item.obj_id,
            text=item.toPlainText(),
            text_length=len(item.toPlainText()),
            font_size=item.font_size,
            clicked_item_type=clicked_type,
            resolved_object_type="text",
            begin_text_edit_called=True,
            interaction_flags=item.textInteractionFlags().value,
            has_focus=focus_ready,
            actual_rect=repr(actual),
            hit_rect=repr(item.shape().boundingRect()),
            temporary_editor_rect=repr(edit_rect),
            text_width=item.textWidth(),
            document_ideal_width=item.document().idealWidth(),
            ready=ready)
        return ready

    def _show_canvas_context_menu(self, scene_pos: QPointF, global_pos) -> None:
        menu = self._build_canvas_context_menu(scene_pos)
        menu.exec(global_pos)

    def _build_canvas_context_menu(self, scene_pos: QPointF) -> QMenu:
        """キャンバス空白部用の右クリックメニューを生成する。"""
        menu = QMenu(self)
        paste_action = menu.addAction("貼り付け")
        paste_action.setObjectName("paste_action")
        paste_action.setEnabled(self.has_copied_objects())
        paste_action.triggered.connect(
            lambda checked=False, pos=QPointF(scene_pos): self.paste_copied_objects(pos)
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
        self._select_only(item)
        self._on_threshold_transparent()

    def _run_binarize_for_item(self, item: "_EditImageItem") -> None:
        self._select_only(item)
        self._on_binarize()

    def _restore_image_item(self, item: "_EditImageItem") -> None:
        self._select_only(item)
        self._on_restore_image()

    def _on_register_template(self) -> None:
        """反映先テンプレートを新規登録/上書き保存する（要件4）。"""
        dialog = _TemplateRegisterDialog(list(self._creation_target_vouchers), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        template = dialog.template()
        if template is None:
            QMessageBox.warning(self, "テンプレート登録", "テンプレート名と反映先伝票を入力してください。")
            return
        existing_template = self._template_by_name(template["name"])
        if existing_template is not None:
            template["key"] = str(existing_template.get("key") or "")
        # 新規追加は上限8個まで（既存名の上書きは件数が増えないので許可: 要件2）。
        existing_names = {t["name"] for t in self._templates}
        is_new = template["name"] not in existing_names
        if is_new and self._reflect_template_count() >= MAX_REFLECT_TEMPLATES:
            self._log_edit_event(
                "reflect_template_add_blocked_limit",
                count=self._reflect_template_count(), limit=MAX_REFLECT_TEMPLATES)
            QMessageBox.information(
                self, "反映先テンプレート",
                f"反映先テンプレートは最大{MAX_REFLECT_TEMPLATES}個まで登録できます。")
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
        self._reload_templates_panel(select_name=template["name"])

    # ── ログ出力 ───────────────────────────────────────────────────────────
    def _log_edit_event(self, event: str, **payload: object) -> None:
        try:
            _log.info("%s %s", event, json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            pass

    # ── お気に入りオブジェクト ─────────────────────────────────────────────
    def _log_favorite_event(self, event: str, **payload: object) -> None:
        self._log_edit_event(event, **payload)

    def _favorite_default_name(self, obj: dict[str, Any]) -> str:
        labels = {
            "image": "画像",
            "text": "テキスト",
            "symbol_text": "テキスト",
            "rectangle": "四角",
            "ellipse": "丸",
            "line": "線",
            "freehand": "手書き",
            "freehand_layer": "手書きレイヤー",
        }
        base = labels.get(str(obj.get("type") or ""), "オブジェクト")
        text = str(obj.get("text") or "").strip()
        if text:
            return f"{base}: {text[:12]}"
        return base

    def _refresh_favorite_list(self) -> None:
        # 件数表示（お気に入り N/15）は widget 有無に関わらず更新する（要件3）。
        self._update_favorite_count_label()
        widget = getattr(self, "_favorite_list", None)
        if widget is None:
            return
        selected_id = ""
        if widget.currentItem() is not None:
            selected_id = str(
                widget.currentItem().data(Qt.ItemDataRole.UserRole) or "")
        scroll_value = widget.verticalScrollBar().value()
        with QSignalBlocker(widget):
            widget.clear()
            for fav in self._favorites:
                item = QListWidgetItem("≡  " + str(fav.get("name") or "お気に入り"))
                item.setData(Qt.ItemDataRole.UserRole, str(fav.get("id") or ""))
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled
                )
                obj_type = str((fav.get("object") or {}).get("type") or "")
                item.setToolTip(obj_type)
                # 1件あたりの高さを小さく固定し、縦幅の固定枠に全件収める（要件4）。
                item.setSizeHint(QSize(0, FAVORITE_LIST_ITEM_HEIGHT))
                widget.addItem(item)
                if str(fav.get("id") or "") == selected_id:
                    widget.setCurrentItem(item)
        widget.verticalScrollBar().setValue(scroll_value)

    def _save_favorites(self) -> None:
        save_favorite_objects(self._favorites)
        _save_settings_string_list(
            FAVORITE_OBJECT_ORDER_KEY,
            [str(fav.get("id") or "") for fav in self._favorites],
        )
        self._refresh_favorite_list()

    def _on_favorite_order_changed(self, favorite_ids: list[str]) -> None:
        """stable ID の表示順だけを更新し、編集履歴やオブジェクトへ触れない。"""
        known = [str(fav.get("id") or "") for fav in self._favorites]
        normalized = _normalized_saved_order(
            favorite_ids, known, setting_key=FAVORITE_OBJECT_ORDER_KEY)
        by_id = {str(fav.get("id") or ""): fav for fav in self._favorites}
        self._favorites = [by_id[fav_id] for fav_id in normalized]
        _save_settings_string_list(FAVORITE_OBJECT_ORDER_KEY, normalized)
        self._favorite_list._log_reorder("order_saved", order=normalized)
        self._refresh_favorite_list()

    def reset_favorite_object_order(self) -> None:
        """created_at、なければ移行時 registration_order の登録順へ戻す。"""
        def registration_key(pair: tuple[int, dict[str, Any]]) -> tuple[str, int, int]:
            index, favorite = pair
            created = str(favorite.get("created_at") or "")
            try:
                registration = int(favorite.get("registration_order", index))
            except (TypeError, ValueError):
                registration = index
            return ("0" if created else "1", created or "", registration)

        self._favorites = [
            favorite for _, favorite in sorted(
                enumerate(self._favorites), key=registration_key)
        ]
        self._save_favorites()

    def add_object_to_favorites(self, item: QGraphicsItem) -> bool:
        if not hasattr(item, "serialize_edit_object"):
            return False
        # お気に入りは最大20個まで（要件3）。上限到達時はメッセージを出して追加しない。
        if len(self._favorites) >= MAX_FAVORITE_OBJECTS:
            self._log_favorite_event(
                "favorite_object_add_blocked_limit",
                count=len(self._favorites), limit=MAX_FAVORITE_OBJECTS)
            QMessageBox.information(
                self, "お気に入り",
                f"お気に入りは最大{MAX_FAVORITE_OBJECTS}個まで登録できます。")
            return False
        obj = dict(item.serialize_edit_object())
        fav = {
            "id": str(uuid.uuid4()),
            "name": self._favorite_default_name(obj),
            "object": obj,
            "favorite_position": {
                "x": float(obj.get("x", item.scenePos().x())),
                "y": float(obj.get("y", item.scenePos().y())),
            },
            "reference_page_width": float(PAGE_W),
            "reference_page_height": float(PAGE_H),
            "registration_order": max(
                [
                    int(existing.get("registration_order", index))
                    for index, existing in enumerate(self._favorites)
                    if str(existing.get("registration_order", index)).lstrip("-").isdigit()
                ],
                default=-1,
            ) + 1,
        }
        self._favorites.append(fav)
        self._save_favorites()
        self._log_favorite_event("favorite_object_added", favorite_id=fav["id"], name=fav["name"])
        self._log_favorite_event("favorite_object_count_changed", count=len(self._favorites))
        return True

    def _favorite_by_id(self, favorite_id: str) -> dict[str, Any] | None:
        for fav in self._favorites:
            if str(fav.get("id") or "") == str(favorite_id or ""):
                return fav
        return None

    def _show_favorite_context_menu(self, pos) -> None:
        widget = getattr(self, "_favorite_list", None)
        if widget is None:
            return
        item = widget.itemAt(pos)
        fav_id = (
            str(item.data(Qt.ItemDataRole.UserRole) or "")
            if item is not None else ""
        )
        menu = QMenu(self)
        rename_action = menu.addAction("名前変更") if item is not None else None
        delete_action = menu.addAction("削除") if item is not None else None
        if item is not None:
            menu.addSeparator()
        reset_action = menu.addAction("お気に入りを登録順に戻す")
        chosen = menu.exec(widget.mapToGlobal(pos))
        if chosen == rename_action:
            self.rename_favorite_object(fav_id)
        elif chosen == delete_action:
            self.remove_favorite_object(fav_id)
        elif chosen == reset_action:
            self.reset_favorite_object_order()

    def rename_favorite_object(self, favorite_id: str, name: str | None = None) -> bool:
        fav = self._favorite_by_id(favorite_id)
        if fav is None:
            return False
        if name is None:
            current = str(fav.get("name") or "")
            name, ok = QInputDialog.getText(self, "お気に入り名変更", "名前:", text=current)
            if not ok:
                return False
        new_name = str(name or "").strip()
        if not new_name:
            return False
        fav["name"] = new_name
        self._save_favorites()
        self._log_favorite_event("favorite_object_renamed", favorite_id=favorite_id, name=new_name)
        return True

    def remove_favorite_object(self, favorite_id: str) -> bool:
        before = len(self._favorites)
        self._favorites = [
            fav for fav in self._favorites
            if str(fav.get("id") or "") != str(favorite_id or "")
        ]
        if len(self._favorites) == before:
            return False
        self._save_favorites()
        self._log_favorite_event("favorite_object_removed", favorite_id=favorite_id)
        self._log_favorite_event("favorite_object_count_changed", count=len(self._favorites))
        return True

    def drop_favorite_object(self, favorite_id: str, scene_pos: QPointF) -> bool:
        fav = self._favorite_by_id(favorite_id)
        if fav is None:
            self._log_favorite_event("favorite_object_drop_failed", favorite_id=favorite_id)
            return False
        obj = self._clone_object_list([dict(fav.get("object") or {})])[0]
        try:
            favorite_position = fav.get("favorite_position")
            if isinstance(favorite_position, dict):
                source_w = float(fav.get("reference_page_width") or PAGE_W)
                source_h = float(fav.get("reference_page_height") or PAGE_H)
                x = float(favorite_position.get("x", obj.get("x", 0.0)))
                y = float(favorite_position.get("y", obj.get("y", 0.0)))
                if source_w > 0 and source_h > 0 and (
                        abs(source_w - PAGE_W) > 0.1 or abs(source_h - PAGE_H) > 0.1):
                    x *= PAGE_W / source_w
                    y *= PAGE_H / source_h
                # 少なくとも一部がページ内に残るよう、アンカーを最小限クランプする。
                obj["x"] = max(-20.0, min(PAGE_W - 2.0, x))
                obj["y"] = max(-20.0, min(PAGE_H - 2.0, y))
                obj["coordinate_origin"] = COORDINATE_ORIGIN
            else:
                # 旧お気に入りは従来どおりドロップ位置へ配置する。
                self._move_copied_objects_to([obj], QPointF(scene_pos))
            # 新形式は登録時の明示値を優先し、反映先の無い旧形式だけ現在の
            # 次回作成用反映先を採用する。
            if not isinstance(obj.get("target_vouchers"), list) or not obj["target_vouchers"]:
                obj["target_vouchers"] = list(self._creation_target_vouchers)
            obj["id"] = str(uuid.uuid4())
            if obj.get("type") == "freehand_layer":
                obj["layer_id"] = obj["id"]
            self._add_loaded_object(obj)
            created = [
                it for it in self.edit_items()
                if getattr(it, "obj_id", None) == obj["id"]
            ]
            if not created:
                raise RuntimeError("favorite object was not created")
            self._select_only(created[0])
            self.set_tool(TOOL_SELECT)
            self.refresh_handles()
            self.commit_history()
            self.mark_dirty()
            self.ensure_background_visible()
            self._log_favorite_event(
                "favorite_object_dropped", favorite_id=favorite_id, object_id=obj["id"]
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._log_favorite_event(
                "favorite_object_drop_failed", favorite_id=favorite_id, error_message=str(exc)
            )
            return False

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
                      font_bold: bool | None = None,
                      font_italic: bool | None = None,
                      font_underline: bool | None = None,
                      font_strikeout: bool | None = None,
                      text_color: str = DEFAULT_TEXT_COLOR,
                      line_width: float | None = None,
                      stroke_color: str = DEFAULT_STROKE_COLOR,
                      fill_color: str | None = None,
                      text_align: str = "left",
                      vertical_align: str = "top",
                      auto_fit: bool = True,
                      manual_resized: bool = False,
                      auto_fit_to_box: bool = False,
                      target_vouchers: list[str] | None = None) -> _EditTextItem:
        fs = self.current_font_size if font_size is None else font_size
        family = self.current_font_family if font_family is None else font_family
        bold = self.current_font_bold if font_bold is None else font_bold
        italic = self.current_font_italic if font_italic is None else font_italic
        underline = (self.current_font_underline if font_underline is None
                     else font_underline)
        strikeout = (self.current_font_strikeout if font_strikeout is None
                     else font_strikeout)
        lw = self.current_line_width if line_width is None else line_width
        tv = self._resolve_target_vouchers(target_vouchers)
        w = max(rect.width(), MIN_TEXT_W)
        h = max(rect.height(), MIN_TEXT_H)
        item = _EditTextItem(text=text, obj_id=obj_id, font_size=fs,
                             box_w=w, box_h=h, font_family=family,
                             font_bold=bold, font_italic=italic,
                             font_underline=underline,
                             font_strikeout=strikeout,
                             text_color=text_color, line_width=lw,
                             stroke_color=stroke_color, fill_color=fill_color,
                             text_align=text_align,
                             vertical_align=vertical_align,
                             auto_fit=auto_fit,
                             manual_resized=manual_resized,
                             auto_fit_to_box=auto_fit_to_box,
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
                        font_bold: bool | None = None,
                        font_italic: bool | None = None,
                        font_underline: bool | None = None,
                        font_strikeout: bool | None = None,
                        text_color: str = DEFAULT_TEXT_COLOR,
                        anchor: str = "center",
                        target_vouchers: list[str] | None = None) -> _EditSymbolTextItem:
        fs = self.current_font_size if font_size is None else font_size
        family = self.current_font_family if font_family is None else font_family
        bold = self.current_font_bold if font_bold is None else font_bold
        italic = self.current_font_italic if font_italic is None else font_italic
        underline = (self.current_font_underline if font_underline is None
                     else font_underline)
        strikeout = (self.current_font_strikeout if font_strikeout is None
                     else font_strikeout)
        tv = self._resolve_target_vouchers(target_vouchers)
        item = _EditSymbolTextItem(text=text, obj_id=obj_id, font_size=fs,
                                   font_family=family,
                                   font_bold=bold,
                                   font_italic=italic,
                                   font_underline=underline,
                                   font_strikeout=strikeout,
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
                 font_underline: bool = False,
                 font_strikeout: bool = False,
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
                             font_italic=font_italic,
                             font_underline=font_underline,
                             font_strikeout=font_strikeout,
                             text_color=text_color,
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
                    font_underline: bool = False,
                    font_strikeout: bool = False,
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
                                font_underline=font_underline,
                                font_strikeout=font_strikeout,
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

    def _convert_symbol_to_editable_text(
            self, item: _EditSymbolTextItem) -> _EditTextItem:
        """symbol_text を中心位置と書式を保った編集可能テキストへ戻す。"""
        center = item.anchor_scene_pos()
        bounds = item.sceneBoundingRect()
        was_selected = item.isSelected()
        self._remove_handles()
        self.loaded_object_ids.discard(item.obj_id)
        if item.scene() is not None:
            self._scene.removeItem(item)
        width = max(float(bounds.width()), MIN_TEXT_W)
        height = max(float(bounds.height()), item.font_size * 1.2, MIN_TEXT_H)
        text_item = self.add_text_rect(
            QRectF(center.x() - width / 2.0, center.y() - height / 2.0,
                   width, height),
            text=item.text(), font_size=item.font_size, obj_id=item.obj_id,
            auto_edit=False, font_family=item.font_family,
            font_bold=item.font_bold, font_italic=item.font_italic,
            font_underline=item.font_underline,
            font_strikeout=item.font_strikeout, text_color=item.text_color,
            auto_fit=True, manual_resized=False,
            target_vouchers=list(item.target_vouchers))
        # auto-fit後の実寸でも保存アンカーが動かないよう中心を再固定する。
        fitted = text_item.sceneBoundingRect()
        text_item.setPos(
            text_item.pos().x() + center.x() - fitted.center().x(),
            text_item.pos().y() + center.y() - fitted.center().y())
        if was_selected:
            self._select_only(text_item)
        self.ensure_background_visible()
        return text_item

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
                                      font_underline=item.font_underline,
                                      font_strikeout=item.font_strikeout,
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
        """選択中編集オブジェクトをアプリ内/QClipboardへコピーする。"""
        if self._is_text_editing():
            return False
        selected = self._selected_edit_items()
        if not selected:
            return False
        return self._copy_items(selected)

    def copy_object(self, item: QGraphicsItem) -> bool:
        """右クリック対象の単一編集オブジェクトをコピーする。"""
        if self._is_text_editing() or not hasattr(item, "serialize_edit_object"):
            return False
        self._select_only(item)
        return self._copy_items([item])

    def duplicate_object(self, item: QGraphicsItem) -> bool:
        """右クリック対象の編集オブジェクトを複製する（要件1）。

        既存のコピー＋オフセット貼り付け処理を再利用するため、Ctrl+C→Ctrl+V と
        同じ結果になる。複製後は新オブジェクトを選択状態にし、1回の履歴として
        Undo/Redo 対象にする（貼り付け側で commit_history を行う）。
        """
        if not self.copy_object(item):
            return False
        return self.paste_copied_objects()

    def _copy_items(self, items: list[QGraphicsItem]) -> bool:
        objects = [dict(item.serialize_edit_object()) for item in items
                   if hasattr(item, "serialize_edit_object")]
        if not objects:
            return False
        self._write_object_clipboard(objects)
        return True

    def _write_object_clipboard(self, objects: list[dict[str, Any]]) -> None:
        self._object_clipboard = self._clone_object_list(objects)
        mime = QMimeData()
        try:
            payload = json.dumps(self._object_clipboard, ensure_ascii=False).encode("utf-8")
            mime.setData(EDIT_OBJECT_MIME, QByteArray(payload))
            mime.setText(payload.decode("utf-8"))
            QApplication.clipboard().setMimeData(mime)
        except Exception:
            # QClipboard が使えない環境でもアプリ内コピーは維持する。
            pass

    def _read_object_clipboard(self) -> list[dict[str, Any]]:
        if self._object_clipboard:
            return self._clone_object_list(self._object_clipboard)
        try:
            mime = QApplication.clipboard().mimeData()
            if mime is None or not mime.hasFormat(EDIT_OBJECT_MIME):
                return []
            raw = bytes(mime.data(EDIT_OBJECT_MIME)).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                return []
            objects = [dict(obj) for obj in data if isinstance(obj, dict)]
            self._object_clipboard = self._clone_object_list(objects)
            return self._clone_object_list(objects)
        except Exception:
            return []

    @staticmethod
    def _clone_object_list(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return json.loads(json.dumps(objects, ensure_ascii=False))
        except Exception:
            return [dict(obj) for obj in objects]

    def has_copied_objects(self) -> bool:
        return bool(self._read_object_clipboard())

    def paste_copied_objects(self, scene_pos: QPointF | None = None) -> bool:
        """コピー済み編集オブジェクトを複製する。位置指定時はそこへ貼り付ける。"""
        if self._is_text_editing():
            return False
        sources = self._read_object_clipboard()
        if not sources:
            return False
        new_ids: list[str] = []
        objects = self._clone_object_list(sources)
        if scene_pos is not None:
            self._move_copied_objects_to(objects, scene_pos)
        for obj in objects:
            if scene_pos is None:
                self._offset_copied_object(obj)
            obj["id"] = str(uuid.uuid4())
            if obj.get("type") == "freehand_layer":
                obj["layer_id"] = obj["id"]
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

    @classmethod
    def _move_copied_objects_to(cls, objects: list[dict[str, Any]], scene_pos: QPointF) -> None:
        origin = cls._objects_origin(objects)
        dx = float(scene_pos.x()) - origin[0]
        dy = float(scene_pos.y()) - origin[1]
        for obj in objects:
            cls._shift_object(obj, dx, dy)

    @staticmethod
    def _objects_origin(objects: list[dict[str, Any]]) -> tuple[float, float]:
        xs: list[float] = []
        ys: list[float] = []
        for obj in objects:
            kind = obj.get("type")
            if kind == "line":
                xs.extend([float(obj.get("x1", 0.0)), float(obj.get("x2", 0.0))])
                ys.extend([float(obj.get("y1", 0.0)), float(obj.get("y2", 0.0))])
            elif kind == "freehand":
                for p in obj.get("points") or []:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        xs.append(float(p[0]))
                        ys.append(float(p[1]))
            elif kind == "freehand_layer":
                for stroke in obj.get("strokes") or []:
                    for p in stroke.get("points") or []:
                        if isinstance(p, (list, tuple)) and len(p) >= 2:
                            xs.append(float(p[0]))
                            ys.append(float(p[1]))
            else:
                xs.append(float(obj.get("x", 0.0)))
                ys.append(float(obj.get("y", 0.0)))
        return (min(xs) if xs else 0.0, min(ys) if ys else 0.0)

    @staticmethod
    def _shift_object(obj: dict[str, Any], dx: float, dy: float) -> None:
        kind = obj.get("type")
        if kind == "line":
            for key in ("x1", "x2"):
                obj[key] = float(obj.get(key, 0.0)) + dx
            for key in ("y1", "y2"):
                obj[key] = float(obj.get(key, 0.0)) + dy
            return
        if kind == "freehand":
            shifted = []
            for p in obj.get("points") or []:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    shifted.append([float(p[0]) + dx, float(p[1]) + dy])
            obj["points"] = shifted
            return
        if kind == "freehand_layer":
            for stroke in obj.get("strokes") or []:
                shifted = []
                for p in stroke.get("points") or []:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        shifted.append([float(p[0]) + dx, float(p[1]) + dy])
                stroke["points"] = shifted
            return
        if "x" in obj:
            obj["x"] = float(obj.get("x", 0.0)) + dx
        if "y" in obj:
            obj["y"] = float(obj.get("y", 0.0)) + dy

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
        if self.has_copied_objects() and self.paste_copied_objects():
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
        else:
            # 選択解除時は既定へ戻さず、最後に明示選択した次回作成用反映先を復元する。
            self.current_target_vouchers = list(self._creation_target_vouchers)
            self._current_template_name = self._creation_template_name
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
            self._sync_favorite_font_controls()
            self._sync_text_decoration_actions([])
            return
        item = selected[0]
        favorite_family = self.current_font_family
        favorite_mixed = False
        self._updating_property_ui = True
        try:
            if hasattr(item, "line_width"):
                self.current_line_width = float(item.line_width)
                with QSignalBlocker(self._line_width_spin):
                    self._line_width_spin.setValue(self.current_line_width)
            text_items = [candidate for candidate in selected
                          if isinstance(candidate, (_EditTextItem, _EditSymbolTextItem))]
            if len(text_items) == 1:
                item = text_items[0]
                self.current_font_family = str(item.font_family)
                favorite_family = self.current_font_family
                self.current_font_size = float(item.font_size)
                self.current_font_bold = bool(item.font_bold)
                self.current_font_italic = bool(item.font_italic)
                self.current_font_underline = bool(item.font_underline)
                self.current_font_strikeout = bool(item.font_strikeout)
                with QSignalBlocker(self._font_family_combo):
                    self._font_family_combo.setCurrentFont(QFont(self.current_font_family))
                with QSignalBlocker(self._font_size_spin):
                    self._font_size_spin.setValue(self.current_font_size)
                self._log_edit_event("voucher_edit_text_style_restored",
                                     object_id=getattr(item, "obj_id", ""))
            elif len(text_items) > 1:
                families = {str(candidate.font_family) for candidate in text_items}
                sizes = {float(candidate.font_size) for candidate in text_items}
                with QSignalBlocker(self._font_family_combo):
                    if len(families) == 1:
                        favorite_family = next(iter(families))
                        self.current_font_family = favorite_family
                        self._font_family_combo.setCurrentFont(QFont(favorite_family))
                    else:
                        favorite_mixed = True
                        self._font_family_combo.setCurrentIndex(-1)
                with QSignalBlocker(self._font_size_spin):
                    if len(sizes) == 1:
                        self._font_size_spin.setValue(next(iter(sizes)))
                    else:
                        self._font_size_spin.setEditText("")
        finally:
            self._updating_property_ui = False
        self._sync_favorite_font_controls(
            family=favorite_family, mixed=favorite_mixed)
        self._sync_text_decoration_actions(text_items)

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
        # 履歴は無制限に積まず、最大 HISTORY_LIMIT 件に保つ（要件1）。
        if len(self._history) > HISTORY_LIMIT:
            overflow = len(self._history) - HISTORY_LIMIT
            del self._history[:overflow]
        self._history_index = len(self._history) - 1
        # オブジェクト追加・移動・サイズ変更・削除などで未保存変更が発生（要件3・6）。
        self.mark_dirty()
        # 変更後のバッヂを描き直す（要件6）。
        self.refresh_badges()
        # アンドゥ・リドゥボタンの有効/無効を更新する（要件1）。
        self._update_undo_redo_buttons()

    def _update_undo_redo_buttons(self) -> None:
        """アンドゥ・リドゥ可否を判定し、ボタンの有効/無効とログを更新する（要件1）。"""
        can_undo = self._history_index > 0
        can_redo = 0 <= self._history_index < len(self._history) - 1
        if self._undo_action is not None:
            self._undo_action.setEnabled(can_undo)
        if self._redo_action is not None:
            self._redo_action.setEnabled(can_redo)
        undo_depth = max(self._history_index, 0)
        redo_depth = max(len(self._history) - 1 - self._history_index, 0)
        if undo_depth != self._prev_undo_depth:
            self._prev_undo_depth = undo_depth
            self._log_edit_event(
                "voucher_edit_undo_stack_changed", depth=undo_depth, can_undo=can_undo)
        if redo_depth != self._prev_redo_depth:
            self._prev_redo_depth = redo_depth
            self._log_edit_event(
                "voucher_edit_redo_stack_changed", depth=redo_depth, can_redo=can_redo)

    def undo(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self._restore_snapshot(self._history[self._history_index])
            self.mark_dirty()
            self._log_edit_event("voucher_edit_undo", history_index=self._history_index)
        self._update_undo_redo_buttons()
        self._debug_state("undo")

    def redo(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._restore_snapshot(self._history[self._history_index])
            self.mark_dirty()
            self._log_edit_event("voucher_edit_redo", history_index=self._history_index)
        self._update_undo_redo_buttons()
        self._debug_state("redo")

    def _restore_snapshot(self, snapshot: list[dict[str, Any]]) -> None:
        """履歴スナップショットから編集レイヤーを再構築する。

        背景は残したまま編集オブジェクトだけを差し替える（要件1・2）。復元中は
        履歴追加を抑止し、Redoスタックを保持する（要件1）。
        """
        selected_ids = {
            str(getattr(item, "obj_id", "") or "")
            for item in self._scene.selectedItems()
            if hasattr(item, "serialize_edit_object")
        }
        selected_ids.discard("")
        self._is_restoring_history = True
        try:
            # 背景は残す実装の clear_edit_layer で編集オブジェクトだけ消す（要件2）。
            self.clear_edit_layer()
            for obj in snapshot:
                self._add_loaded_object(obj)
            # 書式Undo/Redo後も同じオブジェクトを選択状態へ戻し、ツールバーを
            # 復元後のフォントへ同期する。削除されたIDは自然に無視する。
            with QSignalBlocker(self._scene):
                for item in self.edit_items():
                    item.setSelected(
                        str(getattr(item, "obj_id", "") or "") in selected_ids)
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
        self._on_selection_changed()

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
        if self._edit_mode == "individual":
            self._add_readonly_common_objects()
        objects = self._current_edit_objects()
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
        # 起点しかない状態ではアンドゥ・リドゥとも無効（要件1）。
        self._update_undo_redo_buttons()
        # 読み込んだオブジェクトのバッヂを表示する（要件6）。
        self.refresh_badges()
        # 読み込んだ手書きレイヤーをレイヤーパネルへ反映する（要件3）。
        self._refresh_layer_panel()
        # 伝票No・共通／個別モード切替後も現在の既定フォント表示へ同期する。
        self._sync_favorite_font_controls()

    def _add_readonly_common_objects(self) -> None:
        """個別モード用に共通モデルを淡色・操作不能の参照レイヤーとして表示する。"""
        for obj in self._common_objects:
            before = set(self._scene.items())
            self._add_loaded_object(obj)
            for item in set(self._scene.items()) - before:
                if not hasattr(item, "serialize_edit_object"):
                    continue
                item._COMMON_READONLY = True  # type: ignore[attr-defined]
                item.setSelected(False)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
                item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                item.setOpacity(0.45)
                item.setZValue(-1.0)

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
        font_bold = bool(obj.get("font_bold", obj.get("bold", False)))
        font_italic = bool(obj.get("font_italic", obj.get("italic", False)))
        font_underline = bool(obj.get("font_underline", obj.get("underline", False)))
        font_strikeout = bool(obj.get("font_strikeout", obj.get("strikeout", False)))
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
                                 font_underline=font_underline,
                                 font_strikeout=font_strikeout,
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
                               font_underline=font_underline,
                               font_strikeout=font_strikeout,
                               text_color=text_color,
                               line_width=line_width,
                               stroke_color=stroke_color,
                               fill_color=fill_color,
                               text_align="left",
                               vertical_align="top",
                               auto_fit=bool(obj.get("auto_fit", True)),
                               manual_resized=bool(obj.get("manual_resized", False)),
                               auto_fit_to_box=bool(obj.get("auto_fit_to_box", False)),
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
                  font_underline=font_underline,
                  font_strikeout=font_strikeout,
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
            trace_id = str(uuid.uuid4())
            self._remember_current_voucher()
            before_objects = self._current_edit_objects()
            before_sha = edit_objects_sha256(before_objects)
            for obj in before_objects:
                if obj.get("type") in {"text", "symbol_text", "rectangle", "ellipse"}:
                    _log.info(
                        "event=voucher_edit_save_before trace_id=%s object_id=%s "
                        "text=%r font_family=%r font_size=%s bold=%s italic=%s "
                        "underline=%s strikeout=%s x=%s y=%s edit_scope=%s "
                        "voucher_no=%s edit_objects_sha256=%s",
                        trace_id, obj.get("id"), obj.get("text"),
                        obj.get("font_family"), obj.get("font_size"),
                        bool(obj.get("font_bold", obj.get("bold", False))),
                        bool(obj.get("font_italic", obj.get("italic", False))),
                        bool(obj.get("font_underline", obj.get("underline", False))),
                        bool(obj.get("font_strikeout", obj.get("strikeout", False))),
                        obj.get("x"), obj.get("y"), self._edit_mode,
                        self.current_voucher_no, before_sha,
                    )
            save_voucher_edit_document(
                self.order_no, self._common_objects,
                self._voucher_objects, self.voucher_nos)
            document = load_voucher_edit_document(self.order_no, self.voucher_nos)
            reloaded_common = document["common_edit"]
            reloaded_individual = document["voucher_edits"].get(
                self._current_voucher_key, [])
            objects = (
                reloaded_common if self._edit_mode == "common"
                else reloaded_individual
            )
            metadata = load_edit_document_metadata(self.order_no)
            content_sha = str(metadata["edit_objects_sha256"])
            revision = int(metadata["edit_revision"])
            for obj in objects:
                if obj.get("type") in {"text", "symbol_text", "rectangle", "ellipse"}:
                    _log.info(
                        "event=voucher_edit_saved trace_id=%s object_id=%s "
                        "text=%r font_family=%r font_size=%s bold=%s italic=%s "
                        "underline=%s strikeout=%s x=%s y=%s edit_scope=%s "
                        "voucher_no=%s edit_data_revision=%s "
                        "edit_objects_sha256=%s",
                        trace_id, obj.get("id"), obj.get("text"),
                        obj.get("font_family"), obj.get("font_size"),
                        bool(obj.get("font_bold", obj.get("bold", False))),
                        bool(obj.get("font_italic", obj.get("italic", False))),
                        bool(obj.get("font_underline", obj.get("underline", False))),
                        bool(obj.get("font_strikeout", obj.get("strikeout", False))),
                        obj.get("x"), obj.get("y"), self._edit_mode,
                        self.current_voucher_no, revision, content_sha,
                    )
            self.loaded_object_ids = {o["id"] for o in objects}
            self._last_edit_render_trace_id = trace_id
            self._last_edit_objects_sha256 = content_sha
            self._last_edit_revision = revision
            self.voucherEditSaved.emit(
                str(self.order_no), str(self.current_voucher_no),
                content_sha, trace_id, revision,
            )
            # 保存成功で未保存変更フラグを下ろす（要件3）。
            self.mark_saved()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "保存エラー", f"編集内容の保存に失敗しました:\n{exc}")
            return False

    def preview_snapshot(self) -> dict[str, Any]:
        """押下時点の共通＋全伝票個別モデルをplain dataへdeep copyする。"""
        current = self.serialize_objects()
        voucher_edits = copy.deepcopy(self._voucher_objects)
        if self._edit_mode == "common":
            common = current
        else:
            common = self._common_objects
            voucher_edits[self._current_voucher_key] = current
        payload = {
            "voucher_no": self.current_voucher_no,
            "common_edit": common,
            "voucher_edits": voucher_edits,
        }
        plain = json.loads(json.dumps(payload, ensure_ascii=False))
        encoded = json.dumps(
            plain, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        plain["sha256"] = hashlib.sha256(encoded).hexdigest()
        return plain

    def preview_unsaved_edits(self) -> bool:
        """保存JSONや履歴を触らず、押下時snapshotをworkerでPDFへ合成する。"""
        base_pdf = self._background_pdf_by_voucher.get(self._current_voucher_key, b"")
        if not base_pdf:
            QMessageBox.information(self, "プレビュー", "背景PDFを読み込み中です。")
            return False
        if self._preview_thread is not None and self._preview_thread.isRunning():
            return False
        snapshot = self.preview_snapshot()
        current_individual = (
            snapshot.get("voucher_edits", {}).get(self._current_voucher_key, [])
            if isinstance(snapshot.get("voucher_edits"), dict)
            else []
        )
        objects = list(snapshot["common_edit"]) + list(current_individual)
        voucher_ids: list[str] | None = None
        print_data: dict[str, Any] | None = None
        parent = self.parentWidget()
        request_builder = getattr(parent, "build_editor_preview_request", None)
        if not callable(request_builder):
            QMessageBox.warning(
                self, "プレビューエラー",
                "伝票一覧のプレビュー用データを取得できませんでした。")
            return False
        try:
            voucher_ids, print_data = request_builder(
                self.order_no, self.current_voucher_no, snapshot,
                self._preview_target_voucher,
            )
        except Exception:
            _log.exception("未保存snapshotの共通プレビュー入力作成に失敗")
            QMessageBox.warning(
                self, "プレビューエラー",
                "プレビュー用データを準備できませんでした。")
            return False
        self._preview_generation += 1
        generation = self._preview_generation
        self._preview_action.setEnabled(False)
        self.statusBar().showMessage("プレビュー生成中…")
        thread = QThread()
        worker = _EditPreviewWorker(
            generation, bytes(base_pdf), objects,
            voucher_ids=voucher_ids, print_data=print_data,
            trace_id=str(uuid.uuid4()),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.ready.connect(self._on_edit_preview_ready)
        worker.failed.connect(self._on_edit_preview_failed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_edit_preview_finished)
        thread.finished.connect(lambda t=thread: _BACKGROUND_THREADS.discard(t))
        _BACKGROUND_THREADS.add(thread)
        self._preview_thread = thread
        self._preview_worker = worker
        thread.start()
        return True

    @Slot(int, bytes)
    def _on_edit_preview_ready(self, generation: int, pdf_bytes: bytes) -> None:
        if generation != self._preview_generation or self._closing:
            return
        from app.voucher_preview_controller import open_voucher_preview
        preview = open_voucher_preview(
            self, pdf_bytes, title=f"指図書プレビュー - {self.order_no}")
        preview.destroyed.connect(
            lambda _obj=None, win=preview:
            self._preview_windows.remove(win) if win in self._preview_windows else None)
        self._preview_windows.append(preview)

    @Slot(int, str)
    def _on_edit_preview_failed(self, generation: int, message: str) -> None:
        _log.error("指図書プレビュー生成失敗: %s", message, exc_info=True)
        if generation == self._preview_generation and not self._closing:
            QMessageBox.warning(self, "プレビューエラー", message)

    @Slot()
    def _on_edit_preview_finished(self) -> None:
        self._preview_thread = None
        self._preview_worker = None
        if hasattr(self, "_preview_action"):
            self._preview_action.setEnabled(True)
        if not self._closing:
            self.statusBar().clearMessage()

    def save(self) -> None:
        if not self._persist():
            return
        QMessageBox.information(self, "保存完了", "保存しました")

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
        view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def show(self) -> None:  # noqa: N802
        """指図書編集画面は標準で最大化表示する。"""
        self._default_maximize_applied = True
        logging.getLogger("tks_to_kintone_app").info("voucher_edit_open_maximized")
        self.showMaximized()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        super().showEvent(event)
        _perf_editor("show", self._perf_started)
        apply_windows_title_bar_theme(self, current_title_bar_is_dark())
        if (
            not self._default_maximize_applied
            and not self.isMaximized()
            and not self.isFullScreen()
        ):
            self._default_maximize_applied = True
            logging.getLogger("tks_to_kintone_app").info("voucher_edit_geometry_restored_or_maximized")
            QTimer.singleShot(0, self.showMaximized)
        if self._main_toolbar_container is not None:
            if self._main_toolbar is not None:
                self._main_toolbar.setMinimumWidth(0)
            needed = max(72, self._main_toolbar.sizeHint().height() + 22 if self._main_toolbar is not None else 72)
            if self._main_toolbar_container.minimumHeight() < needed:
                self._main_toolbar_container.setMinimumHeight(needed)
                logging.getLogger("tks_to_kintone_app").info(
                    "voucher_edit_toolbar_wrapped_height_adjusted %s",
                    {"height": needed},
                )
            QTimer.singleShot(0, self._log_toolbar_scroll_metrics)
        QTimer.singleShot(0, self._log_left_pane_scroll_range)
        # 画面表示後にページ全体を編集領域いっぱいへフィット（要件2）。
        self.fit_page_to_view()
        QTimer.singleShot(
            0, lambda: _perf_editor("first_interactive", self._perf_started)
            if not self._closing else None
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        super().resizeEvent(event)
        # ウィンドウ／全画面切替・最大化などのリサイズ時に再フィットする（要件2）。
        self.fit_page_to_view()

    # ── 終了処理 ──────────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        """未保存変更を確認して指図書編集画面を閉じる。"""
        try:
            if self._closing and not self._close_in_progress:
                self._cleanup_selection_handles()
                event.accept()
                return
            from app.gui import update_shutdown_is_committed

            if not update_shutdown_is_committed() and self.is_dirty():
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
            self._closing = True
            self._background_load_generation += 1
            self._preview_generation += 1
            if self._preview_worker is not None:
                cancel = getattr(self._preview_worker, "cancel", None)
                if callable(cancel):
                    cancel()
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
