"""伝票一覧と指図書編集で共用するプレビュー画面コントローラー。"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

from app.voucher_edit_objects import voucher_key_for
from app.voucher_templates import VOUCHER_IDS

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget


def resolve_preview_voucher_ids(voucher_checks: object) -> list[str]:
    """一覧のチェック状態から、正式な伝票順で有効な伝票IDだけを返す。"""
    checks = voucher_checks if isinstance(voucher_checks, dict) else {}
    return [voucher_id for voucher_id in VOUCHER_IDS if bool(checks.get(voucher_id))]


def apply_editor_preview_snapshot(
    print_data: dict,
    snapshot: dict[str, object],
) -> dict:
    """未保存の共通編集と伝票No別編集を、対応する各ページへ合成する。

    入力を変更しないdeep-copy境界を設け、共通編集は全ページ、個別編集は
    voucher_noが一致するページだけへ反映する。
    """
    prepared = copy.deepcopy(print_data)
    common = copy.deepcopy(list(snapshot.get("common_edit") or []))
    voucher_edits_raw = snapshot.get("voucher_edits")
    voucher_edits = (
        voucher_edits_raw if isinstance(voucher_edits_raw, dict) else {}
    )

    # 旧snapshot形式との互換。現在伝票の個別編集だけが渡された場合も扱う。
    if not voucher_edits and "voucher_edit" in snapshot:
        voucher_edits = {
            voucher_key_for(snapshot.get("voucher_no")):
            list(snapshot.get("voucher_edit") or [])
        }

    for page in prepared.get("pages") or []:
        if not isinstance(page, dict):
            continue
        key = voucher_key_for(page.get("voucher_no"))
        individual = voucher_edits.get(key)
        if not isinstance(individual, list):
            individual = []
        page["edit_objects"] = copy.deepcopy(common) + copy.deepcopy(individual)
    return prepared


def build_voucher_preview_pdf(
    voucher_ids: list[str],
    print_data: dict,
    *,
    edit_render_trace_id: str = "",
    reload_edit_objects: bool,
) -> bytes:
    """一覧・編集画面で共用する、保存を伴わない正式PDF生成経路。"""
    from app.voucher_service import build_vouchers_pdf_bytes

    return build_vouchers_pdf_bytes(
        list(voucher_ids),
        print_data,
        edit_render_trace_id=edit_render_trace_id,
        reload_edit_objects=reload_edit_objects,
        bypass_preview_cache=True,
    )


def open_voucher_preview(
    parent: "QWidget | None",
    pdf_bytes: bytes,
    *,
    title: str = "",
    edit_render_trace_id: str = "",
    edit_objects_sha256: str = "",
    preview_cache_hit: bool = False,
):
    """既存の印刷プレビューを、両画面で同じ設定により開く。"""
    if not pdf_bytes:
        raise RuntimeError("PDFプレビューの生成結果が空です。")
    from app.voucher_preview_window import VoucherPrintPreviewWindow

    preview = VoucherPrintPreviewWindow(
        bytes(pdf_bytes), parent=parent,
        edit_render_trace_id=edit_render_trace_id,
        edit_objects_sha256=edit_objects_sha256,
        preview_cache_hit=preview_cache_hit,
    )
    if title:
        preview.setWindowTitle(title)
    preview.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    preview.showMaximized()
    return preview
