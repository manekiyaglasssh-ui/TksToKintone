from __future__ import annotations

import ast
import hashlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pypdf

from app import voucher_service as vs


class _TextCanvas:
    def __init__(self) -> None:
        self.draws: list[tuple[str, float, float, str]] = []
        self.lines: list[tuple[float, float, float, float]] = []

    def setFont(self, *_args): pass
    def setFillColorRGB(self, *_args): pass
    def setStrokeColorRGB(self, *_args): pass
    def setLineWidth(self, *_args): pass
    def drawString(self, x, y, text): self.draws.append(("left", x, y, text))
    def drawRightString(self, x, y, text): self.draws.append(("right", x, y, text))
    def drawCentredString(self, x, y, text): self.draws.append(("center", x, y, text))
    def line(self, *args): self.lines.append(args)


class _TransformCanvas(_TextCanvas):
    def __init__(self) -> None:
        super().__init__()
        self.transforms: list[tuple[float, ...]] = []
        self.translations: list[tuple[float, float]] = []

    def saveState(self): pass
    def restoreState(self): pass
    def translate(self, x, y): self.translations.append((x, y))
    def transform(self, *args): self.transforms.append(args)


def _page_with_styles() -> dict:
    styles = (
        {},
        {"font_bold": True},
        {"font_italic": True},
        {"font_underline": True},
        {"font_strikeout": True},
        {"font_bold": True, "font_italic": True},
        {"font_bold": True, "font_underline": True},
        {
            "font_bold": True,
            "font_italic": True,
            "font_underline": True,
            "font_strikeout": True,
            "rotation": 12.0,
            "opacity": 0.6,
        },
    )
    objects = []
    for index, style in enumerate(styles):
        objects.append({
            "id": f"style-{index}",
            "type": "text",
            "text": f"装飾{index}",
            "x": 30.0,
            "y": 30.0 + index * 18.0,
            "width": 120.0,
            "height": 16.0,
            "font_family": "存在しないフォント名",
            "font_size": 10.0,
            "target_vouchers": [f"{number:02d}" for number in range(1, 9)],
            **style,
        })
    return {
        "order_no": "STYLE-REGRESSION",
        "voucher_no": "V-1",
        "customer_name": "テスト得意先",
        "details": [{"name": "品名", "qty": "1"}],
        "edit_objects": objects,
    }


class TestVoucherPdfTextStyles(unittest.TestCase):
    def setUp(self) -> None:
        vs._EDIT_FONT_CACHE.clear()
        vs._EDIT_FONT_METADATA.clear()
        vs._EDIT_TTC_FACE_CACHE.clear()

    def test_undefined_legacy_bold_offset_name_is_not_referenced(self) -> None:
        source_path = Path(vs.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        legacy_name = "DATA_BOLD_OFFSET_" + "X"
        references = [node.id for node in ast.walk(tree)
                      if isinstance(node, ast.Name) and node.id == legacy_name]
        self.assertEqual(references, [])

    def test_contains_cjk_covers_japanese_fullwidth_and_compatibility(self) -> None:
        for text in ("あ", "ア", "漢", "Ａ", "！", "神", "テキスト"):
            with self.subTest(text=text):
                self.assertTrue(vs.contains_cjk(text))
        for text in ("TEST", "abc123", "!?."):
            with self.subTest(text=text):
                self.assertFalse(vs.contains_cjk(text))

    def test_synthetic_bold_uses_small_point_offset_only_when_requested(self) -> None:
        normal = _TextCanvas()
        vs.draw_text_in_scene_rect(
            normal, "通常", 10, 20, 100, 20, vs._FONT_NAME, 10, bold=False)
        self.assertEqual(len(normal.draws), 1)

        bold = _TextCanvas()
        vs.draw_text_in_scene_rect(
            bold, "太字", 10, 20, 100, 20, vs._FONT_NAME, 10, bold=True)
        self.assertEqual(len(bold.draws), 2)
        self.assertAlmostEqual(
            bold.draws[1][1] - bold.draws[0][1],
            vs.TEXT_SYNTHETIC_BOLD_OFFSET_PT,
        )
        self.assertEqual(vs.TEXT_SYNTHETIC_BOLD_OFFSET_PT, 0.15)

    def test_formal_bold_face_does_not_use_synthetic_bold(self) -> None:
        canvas = _TextCanvas()
        vs.draw_text_in_scene_rect(
            canvas, "正式Bold", 10, 20, 100, 20, "RegisteredBoldFace", 10,
            bold=True)
        self.assertEqual(len(canvas.draws), 1)

    def test_synthetic_italic_uses_shear_and_combines_with_synthetic_bold(self) -> None:
        canvas = _TransformCanvas()
        vs._draw_pdf_text(
            canvas, "drawString", 12, 34, "Yu Gothic UI",
            synthetic_bold=True, synthetic_italic=True)
        self.assertEqual(canvas.translations, [(12, 34)])
        self.assertEqual(
            canvas.transforms,
            [(1, 0, vs.TEXT_SYNTHETIC_ITALIC_SHEAR, 1, 0, 0)],
        )
        self.assertEqual(len(canvas.draws), 2)
        self.assertEqual(vs.TEXT_SYNTHETIC_ITALIC_SHEAR, 0.20)
        self.assertAlmostEqual(
            canvas.draws[1][1] - canvas.draws[0][1],
            vs.TEXT_SYNTHETIC_BOLD_OFFSET_PT,
        )

    def test_yu_gothic_ui_bold_is_kept_and_only_italic_is_synthetic(self) -> None:
        bold_path = Path("/fonts/YuGothB.ttc")

        def find_font(family: str, bold: bool, italic: bool):
            if family == "Yu Gothic UI" and bold and not italic:
                return bold_path
            return None

        with mock.patch.object(vs, "_edit_font_file", side_effect=find_font) as find, \
             mock.patch.object(vs, "_register_edit_font", return_value="YuUiBold"), \
             mock.patch.object(vs, "_ttc_face_index", return_value=2):
            resolved = vs._resolve_edit_pdf_font("Yu Gothic UI", True, True)
        self.assertEqual(resolved, "YuUiBold")
        self.assertEqual(find.call_args_list[:2], [
            mock.call("Yu Gothic UI", True, True),
            mock.call("Yu Gothic UI", True, False),
        ])
        metadata = vs._EDIT_FONT_METADATA[("Yu Gothic UI", True, True)]
        self.assertFalse(metadata["fallback_used"])
        self.assertFalse(metadata["synthetic_bold"])
        self.assertTrue(metadata["synthetic_italic"])
        self.assertFalse(metadata["resolved_is_italic"])
        self.assertEqual(metadata["resolved_font_file"], str(bold_path))
        self.assertEqual(metadata["resolved_pdf_font_name"], "YuUiBold")

    def test_bold_upright_face_keeps_synthetic_italic_independently(self) -> None:
        self.assertEqual(
            vs._synthetic_style_flags(True, True, True, False),
            (False, True),
        )
        self.assertEqual(
            vs._synthetic_style_flags(True, True, True, True),
            (False, False),
        )
        self.assertEqual(
            vs._synthetic_style_flags(True, True, False, False),
            (True, True),
        )

    def test_bold_upright_resolved_face_metadata_uses_synthetic_italic(self) -> None:
        path = Path("/usr/share/fonts/redhat-vf/RedHatText[wght].ttf")
        if not path.is_file():
            self.skipTest("SFNT fixture font is unavailable")

        def find_font(_family: str, bold: bool, italic: bool):
            return path if bold and not italic else None

        face = {
            "family": "Example", "subfamily": "Bold", "is_bold": True,
            "is_italic": False, "italic_angle": 0.0, "fs_selection": 32,
            "fs_selection_italic": False, "post_italic_angle": 0.0,
        }
        with mock.patch.object(vs, "_edit_font_file", side_effect=find_font), \
             mock.patch.object(vs, "_register_edit_font", return_value="ExampleBold"), \
             mock.patch.object(vs, "_ttc_face_index", return_value=0), \
             mock.patch.object(vs, "_inspect_sfnt_face", return_value=face):
            vs._resolve_edit_pdf_font("Example", True, True)
        metadata = vs._EDIT_FONT_METADATA[("Example", True, True)]
        self.assertTrue(metadata["resolved_is_bold"])
        self.assertFalse(metadata["resolved_is_italic"])
        self.assertFalse(metadata["synthetic_bold"])
        self.assertTrue(metadata["synthetic_italic"])

    def test_yu_gothic_ui_regular_combines_synthetic_bold_and_italic(self) -> None:
        regular_path = Path("/fonts/YuGothR.ttc")

        def find_font(family: str, bold: bool, italic: bool):
            if family == "Yu Gothic UI" and not bold and not italic:
                return regular_path
            return None

        with mock.patch.object(vs, "_edit_font_file", side_effect=find_font), \
             mock.patch.object(vs, "_register_edit_font", return_value="YuUiRegular"), \
             mock.patch.object(vs, "_ttc_face_index", return_value=1):
            resolved = vs._resolve_edit_pdf_font("Yu Gothic UI", True, True)
        self.assertEqual(resolved, "YuUiRegular")
        metadata = vs._EDIT_FONT_METADATA[("Yu Gothic UI", True, True)]
        self.assertTrue(metadata["synthetic_bold"])
        self.assertTrue(metadata["synthetic_italic"])

    def test_formal_italic_does_not_use_synthetic_italic(self) -> None:
        with mock.patch.object(vs, "_edit_font_file", return_value=Path("/fonts/italic.ttf")), \
             mock.patch.object(vs, "_register_edit_font", return_value="FormalItalic"):
            resolved = vs._resolve_edit_pdf_font("Example", False, True)
        self.assertEqual(resolved, "FormalItalic")
        self.assertFalse(vs._EDIT_FONT_METADATA[("Example", False, True)]["synthetic_italic"])

    def test_cjk_ignores_native_italic_metadata_and_uses_upright_face(self) -> None:
        native_metadata = {
            "requested_family": "Example CJK",
            "resolved_family": "Example CJK",
            "resolved_subfamily": "Italic",
            "resolved_is_bold": False,
            "resolved_is_italic": True,
        }
        upright_metadata = {
            "requested_family": "Example CJK",
            "resolved_family": "Example CJK",
            "resolved_subfamily": "Regular",
            "resolved_is_bold": False,
            "resolved_is_italic": False,
        }
        with mock.patch.object(vs, "_resolve_edit_pdf_font",
                               return_value="ExampleCJKRegular"), \
             mock.patch.object(vs, "_resolved_edit_font_metadata",
                               return_value=upright_metadata):
            details = vs._text_font_run_details(
                "テキスト", "ExampleCJKItalic", native_metadata, False, True)
        self.assertEqual(len(details), 1)
        run = details[0]
        self.assertEqual(run["font_name"], "ExampleCJKRegular")
        self.assertFalse(run["native_italic_face_used"])
        self.assertTrue(run["synthetic_italic"])
        self.assertEqual(run["italic_strategy"], "synthetic_cjk")

    def test_cjk_bold_face_and_regular_fallback_keep_styles_independent(self) -> None:
        bold_face = {
            "resolved_family": "Example CJK",
            "resolved_subfamily": "Bold",
            "resolved_is_bold": True,
            "resolved_is_italic": False,
        }
        bold_run = vs._text_font_run_details(
            "テキスト", "ExampleCJKBold", bold_face, True, True)[0]
        self.assertFalse(bold_run["synthetic_bold"])
        self.assertTrue(bold_run["synthetic_italic"])

        regular_face = dict(
            bold_face, resolved_subfamily="Regular", resolved_is_bold=False)
        regular_run = vs._text_font_run_details(
            "テキスト", "ExampleCJKRegular", regular_face, True, True)[0]
        self.assertTrue(regular_run["synthetic_bold"])
        self.assertTrue(regular_run["synthetic_italic"])

    def test_mixed_text_splits_cjk_from_native_latin_italic(self) -> None:
        upright = {
            "requested_family": "Example",
            "resolved_family": "Example",
            "resolved_subfamily": "Regular",
            "resolved_is_bold": False,
            "resolved_is_italic": False,
        }
        native = dict(
            upright, resolved_subfamily="Italic", resolved_is_italic=True)

        def metadata(_family, _bold, italic, _font):
            return native if italic else upright

        with mock.patch.object(
                vs, "_resolve_edit_pdf_font",
                side_effect=lambda _family, _bold, italic:
                "ExampleItalic" if italic else "ExampleRegular"), \
             mock.patch.object(vs, "_resolved_edit_font_metadata",
                               side_effect=metadata):
            details = vs._text_font_run_details(
                "ABCテキストDEF", "ExampleRegular", upright, False, True)
        self.assertEqual([run["text"] for run in details],
                         ["ABC", "テキスト", "DEF"])
        self.assertEqual([run["italic_strategy"] for run in details],
                         ["native", "synthetic_cjk", "native"])
        self.assertTrue(details[0]["native_italic_face_used"])
        self.assertFalse(details[1]["native_italic_face_used"])
        self.assertFalse(details[0]["synthetic_italic"])
        self.assertTrue(details[1]["synthetic_italic"])

    def test_ttc_face_index_uses_matching_family_and_style(self) -> None:
        regular = mock.Mock(
            numSubfonts=2, familyName=b"Yu Gothic UI", styleName=b"Regular", flags=0)
        semibold = mock.Mock(
            familyName=b"Yu Gothic UI", styleName=b"Semibold", flags=0)
        with mock.patch.object(vs, "TTFontFace", side_effect=[regular, semibold]):
            index = vs._ttc_face_index(
                Path("/fonts/YuGoth.ttc"), "YU  GOTHIC UI", True, False)
        self.assertEqual(index, 1)

    def test_family_normalization_absorbs_width_case_and_spaces(self) -> None:
        self.assertEqual(
            vs._normalized_font_name("Ｙｕ  Ｇｏｔｈｉｃ　ＵＩ"),
            vs._normalized_font_name("yu gothic ui"),
        )

    def test_family_with_weight_suffix_matches_sfnt_family_and_subfamily(self) -> None:
        self.assertTrue(vs._font_family_matches(
            "Malgun Gothic Semilight", "Malgun Gothic", "Semilight"))
        self.assertTrue(vs._font_family_matches(
            "Yu Gothic UI", "Yu Gothic UI", "Semibold"))
        self.assertFalse(vs._font_family_matches(
            "Yu Gothic UI", "Yu Gothic", "Regular"))

    def test_rotation_and_opacity_apply_to_whole_text_object_state(self) -> None:
        class StateCanvas:
            def __init__(self):
                self.fill_alpha = None
                self.stroke_alpha = None
                self.translations = []
                self.rotations = []
            def setFillAlpha(self, value): self.fill_alpha = value
            def setStrokeAlpha(self, value): self.stroke_alpha = value
            def translate(self, x, y): self.translations.append((x, y))
            def rotate(self, value): self.rotations.append(value)

        canvas = StateCanvas()
        vs._apply_edit_object_pdf_state(canvas, {
            "type": "text", "x": 10, "y": 20, "width": 100, "height": 30,
            "rotation": 15, "opacity": 0.4,
        })
        self.assertEqual(canvas.fill_alpha, 0.4)
        self.assertEqual(canvas.stroke_alpha, 0.4)
        self.assertEqual(canvas.rotations, [-15.0])
        self.assertEqual(len(canvas.translations), 2)

    def test_synthetic_italic_decoration_width_covers_right_overhang(self) -> None:
        canvas = _TextCanvas()
        with mock.patch.object(vs.pdfmetrics, "stringWidth", return_value=50.0):
            vs._draw_pdf_text_decorations(
                canvas, "斜体", 10, 20, "Face", 12,
                underline=True, synthetic_italic=True)
        self.assertGreaterEqual(
            canvas.lines[0][2] - canvas.lines[0][0],
            50.0 + vs.TEXT_SYNTHETIC_ITALIC_SHEAR * 12,
        )
        right = _TextCanvas()
        with mock.patch.object(vs.pdfmetrics, "stringWidth", return_value=50.0):
            vs._draw_pdf_text_decorations(
                right, "斜体", 100, 20, "Face", 12, text_align="right",
                underline=True, synthetic_italic=True)
        self.assertEqual(right.lines[0][2], 100.0)
        self.assertGreaterEqual(
            right.lines[0][2] - right.lines[0][0],
            50.0 + vs.TEXT_SYNTHETIC_ITALIC_SHEAR * 12,
        )

    @staticmethod
    def _styled_pdf(text: str, italic: bool, *, font_name: str = "Helvetica",
                    font_size: float = 72.0) -> bytes:
        from reportlab.pdfgen.canvas import Canvas

        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(400, 200), pageCompression=0)
        vs.draw_styled_pdf_text(
            canvas, text, 80, 60, font_name, font_size,
            synthetic_italic=italic)
        canvas.save()
        return output.getvalue()

    @staticmethod
    def _dark_pixels(pdf: bytes) -> list[tuple[int, int]]:
        import fitz

        with fitz.open(stream=pdf, filetype="pdf") as document:
            pixmap = document[0].get_pixmap(
                matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, alpha=False)
            samples = pixmap.samples
            stride = pixmap.stride
            return [
                (x, y)
                for y in range(pixmap.height)
                for x in range(pixmap.width)
                if samples[y * stride + x] < 180
            ]

    def test_rasterized_latin_has_measurable_top_right_slant(self) -> None:
        normal_pdf = self._styled_pdf("TEST", False)
        italic_pdf = self._styled_pdf("TEST", True)
        normal = self._dark_pixels(normal_pdf)
        italic = self._dark_pixels(italic_pdf)
        self.assertNotEqual(normal, italic)

        def upper_centroid(points: list[tuple[int, int]]) -> float:
            top = min(y for _x, y in points)
            bottom = max(y for _x, y in points)
            cutoff = top + (bottom - top) // 3
            xs = [x for x, y in points if y <= cutoff]
            return sum(xs) / len(xs)

        # 2x rasterで0.20 shearはTEST上部を十分右へ移動する。
        self.assertGreater(upper_centroid(italic) - upper_centroid(normal), 10.0)

    def test_rasterized_japanese_differs_and_pdf_hash_changes(self) -> None:
        vs._ensure_font()
        normal_pdf = self._styled_pdf(
            "テキスト", False, font_name=vs._FONT_NAME, font_size=48)
        italic_pdf = self._styled_pdf(
            "テキスト", True, font_name=vs._FONT_NAME, font_size=48)
        self.assertNotEqual(
            hashlib.sha256(normal_pdf).digest(), hashlib.sha256(italic_pdf).digest())
        self.assertNotEqual(self._dark_pixels(normal_pdf), self._dark_pixels(italic_pdf))

    @staticmethod
    def _four_style_cjk_pdf(bold: bool, italic: bool) -> bytes:
        from reportlab.pdfgen.canvas import Canvas

        vs._ensure_font()
        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(500, 220), pageCompression=0)
        vs.draw_styled_pdf_text(
            canvas, "テキスト", 80, 70, vs._FONT_NAME, 72,
            synthetic_bold=bold, synthetic_italic=italic)
        canvas.save()
        return output.getvalue()

    def test_cjk_four_style_raster_has_measurable_slant_and_weight(self) -> None:
        variants = {
            style: self._dark_pixels(self._four_style_cjk_pdf(*style))
            for style in ((False, False), (False, True),
                          (True, False), (True, True))
        }
        normal = variants[(False, False)]
        italic = variants[(False, True)]
        bold = variants[(True, False)]
        bold_italic = variants[(True, True)]
        self.assertNotEqual(normal, italic)
        self.assertNotEqual(bold, bold_italic)
        italic_shift = self._upper_centroid(italic) - self._upper_centroid(normal)
        combined_shift = (
            self._upper_centroid(bold_italic) - self._upper_centroid(bold))
        self.assertGreater(italic_shift, 7.0)
        self.assertGreater(combined_shift, 7.0)
        self.assertGreater(len(bold_italic), len(italic))

    @staticmethod
    def _four_style_pdf(text: str, bold: bool, italic: bool) -> bytes:
        from reportlab.pdfgen.canvas import Canvas

        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(400, 200), pageCompression=0)
        vs.draw_styled_pdf_text(
            canvas, text, 80, 60, "Helvetica", 72,
            synthetic_bold=bold, synthetic_italic=italic)
        vs._draw_pdf_text_decorations(
            canvas, text, 80, 60, "Helvetica", 72, underline=True,
            synthetic_bold=bold, synthetic_italic=italic)
        canvas.save()
        return output.getvalue()

    @staticmethod
    def _upper_centroid(points: list[tuple[int, int]]) -> float:
        top = min(y for _x, y in points)
        bottom = max(y for _x, y in points)
        cutoff = top + (bottom - top) // 3
        xs = [x for x, y in points if y <= cutoff]
        return sum(xs) / len(xs)

    def test_bold_italic_raster_is_bold_and_slanted_independently(self) -> None:
        variants = {
            (bold, italic): self._four_style_pdf("TEST", bold, italic)
            for bold, italic in ((False, False), (True, False),
                                 (False, True), (True, True))
        }
        pixels = {style: self._dark_pixels(pdf) for style, pdf in variants.items()}
        normal = pixels[(False, False)]
        bold = pixels[(True, False)]
        italic = pixels[(False, True)]
        bold_italic = pixels[(True, True)]
        self.assertNotEqual(bold, bold_italic)
        bold_shift = self._upper_centroid(bold_italic) - self._upper_centroid(bold)
        italic_shift = self._upper_centroid(italic) - self._upper_centroid(normal)
        self.assertGreater(bold_shift, 10.0)
        self.assertGreater(italic_shift, 10.0)
        self.assertAlmostEqual(bold_shift, italic_shift, delta=2.0)
        self.assertGreater(len(bold_italic), len(normal))
        hashes = {hashlib.sha256(pdf).digest() for pdf in variants.values()}
        self.assertEqual(len(hashes), 4)
        # 下線を含む全体幅は斜体張り出し分だけ増えるが、極端には伸びない。
        bold_width = max(x for x, _y in bold) - min(x for x, _y in bold) + 1
        combined_width = (max(x for x, _y in bold_italic)
                          - min(x for x, _y in bold_italic) + 1)
        self.assertGreater(combined_width, bold_width)
        self.assertLess(combined_width - bold_width, 40)

    def test_japanese_glyph_fallback_bold_italic_run_remains_slanted(self) -> None:
        import fitz
        from reportlab.pdfgen.canvas import Canvas

        vs._ensure_font()
        path = Path("/usr/share/fonts/redhat-vf/RedHatText[wght].ttf")
        if not path.is_file():
            self.skipTest("Latin-only cmap fixture is unavailable")
        base_metadata = {
            "resolved_font_file": str(path), "ttc_face_index": 0,
            "resolved_is_bold": False, "resolved_is_italic": False,
            "synthetic_bold": False, "synthetic_italic": False,
        }

        def render(bold: bool, italic: bool) -> tuple[bytes, list[tuple[str, str, bool, bool]]]:
            metadata = vs._font_metadata_for_text(
                base_metadata, "テキスト", requested_bold=bold,
                requested_italic=italic)
            runs = vs._text_font_runs("テキスト", "UnusedLatin", metadata, bold, italic)
            output = io.BytesIO()
            canvas = Canvas(output, pagesize=(400, 200), pageCompression=0)
            vs.draw_styled_pdf_text_runs(
                canvas, runs, 80, 60, 48, text_align="left")
            canvas.save()
            return output.getvalue(), runs

        bold_pdf, bold_runs = render(True, False)
        combined_pdf, combined_runs = render(True, True)
        self.assertEqual(bold_runs, [("テキスト", vs._FONT_NAME, True, False)])
        self.assertEqual(combined_runs, [("テキスト", vs._FONT_NAME, True, True)])
        bold_pixels = self._dark_pixels(bold_pdf)
        combined_pixels = self._dark_pixels(combined_pdf)
        self.assertGreater(
            self._upper_centroid(combined_pixels) - self._upper_centroid(bold_pixels),
            7.0,
        )
        self.assertNotEqual(hashlib.sha256(bold_pdf).digest(),
                            hashlib.sha256(combined_pdf).digest())
        stream = pypdf.PdfReader(io.BytesIO(combined_pdf)).pages[0].get_contents().get_data()
        self.assertIn(b"1 0 .2 1", stream)

    def test_edit_object_e2e_bold_italic_and_italic_both_shear(self) -> None:
        import fitz
        from reportlab.pdfgen.canvas import Canvas

        objects = [
            {"id": "upper", "type": "text", "text": "テキスト",
             "x": 80, "y": 70, "width": 220, "height": 45,
             "font_family": "Yu Gothic UI", "font_size": 18,
             "font_bold": True, "font_italic": True, "font_underline": True,
             "target_vouchers": ["03"]},
            {"id": "lower", "type": "text", "text": "テキスト",
             "x": 80, "y": 145, "width": 220, "height": 45,
             "font_family": "Yu Gothic UI", "font_size": 18,
             "font_bold": False, "font_italic": True, "font_underline": True,
             "target_vouchers": ["03"]},
        ]
        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(vs.PAGE_W, vs.PAGE_H), pageCompression=0)
        with self.assertLogs("tks_to_kintone_app", level="INFO") as captured, \
             mock.patch.object(vs, "_edit_font_file", return_value=None):
            vs._draw_edit_objects(canvas, objects)
        canvas.save()
        pdf = output.getvalue()
        logs = "\n".join(captured.output)
        upper_log = next(line for line in captured.output if "object_id=upper" in line)
        lower_log = next(line for line in captured.output if "object_id=lower" in line)
        self.assertIn("font_bold=True", upper_log)
        self.assertIn("synthetic_bold_used=True", upper_log)
        self.assertIn("synthetic_italic_used=True", upper_log)
        self.assertIn("font_run_count=1", upper_log)
        self.assertIn("font_bold=False", lower_log)
        self.assertIn("synthetic_bold_used=False", lower_log)
        self.assertIn("synthetic_italic_used=True", lower_log)
        self.assertEqual(logs.count("object_id=upper"), 2)
        self.assertEqual(logs.count("object_id=lower"), 2)
        self.assertIn("run_contains_cjk=true", upper_log + logs)
        self.assertIn("italic_strategy=synthetic_cjk", upper_log + logs)
        self.assertIn("native_italic_face_used=false", logs)
        stream = pypdf.PdfReader(io.BytesIO(pdf)).pages[0].get_contents().get_data()
        self.assertGreaterEqual(stream.count(b"1 0 .2 1"), 2)
        with fitz.open(stream=pdf, filetype="pdf") as document:
            upper = document[0].get_pixmap(
                matrix=fitz.Matrix(3, 3), clip=fitz.Rect(70, 65, 320, 125),
                colorspace=fitz.csGRAY, alpha=False).samples
            lower = document[0].get_pixmap(
                matrix=fitz.Matrix(3, 3), clip=fitz.Rect(70, 140, 320, 200),
                colorspace=fitz.csGRAY, alpha=False).samples
        self.assertNotEqual(upper, lower)
        self.assertGreater(sum(value < 180 for value in upper),
                           sum(value < 180 for value in lower))
        self.assertNotEqual(hashlib.sha256(pdf).digest(), b"\0" * 32)

    def test_shear_and_target_text_are_in_same_graphics_state(self) -> None:
        pdf = self._styled_pdf("TEST", True, font_size=30)
        stream = pypdf.PdfReader(io.BytesIO(pdf)).pages[0].get_contents().get_data()
        state_start = stream.index(b"q\n1 0 .2 1 80 60 cm")
        text_draw = stream.index(b"(TEST) Tj", state_start)
        state_end = stream.index(b"\nQ", text_draw)
        self.assertLess(state_start, text_draw)
        self.assertLess(text_draw, state_end)

    def test_end_to_end_voucher_japanese_text_raster_and_hash_change(self) -> None:
        def generate(italic: bool) -> bytes:
            page = {
                "order_no": "ITALIC-E2E", "voucher_no": "1", "details": [],
                "edit_objects": [{
                    "id": "target-text", "type": "text", "text": "テキスト",
                    "x": 50.0, "y": 50.0, "width": 180.0, "height": 45.0,
                    "font_family": "存在しないフォント名", "font_size": 36.0,
                    "font_italic": italic, "target_vouchers": ["03"],
                }],
            }
            return vs.build_vouchers_pdf_bytes(["03"], {"pages": [page]})

        normal_pdf = generate(False)
        italic_pdf = generate(True)
        self.assertNotEqual(
            hashlib.sha256(normal_pdf).digest(), hashlib.sha256(italic_pdf).digest())
        self.assertNotEqual(self._dark_pixels(normal_pdf), self._dark_pixels(italic_pdf))
        stream = pypdf.PdfReader(io.BytesIO(italic_pdf)).pages[0].get_contents().get_data()
        self.assertIn(b"1 0 .2 1 50", stream)

    def test_sfnt_inspection_distinguishes_upright_and_formal_italic(self) -> None:
        upright = Path(
            "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc")
        italic = Path("/usr/share/fonts/google-noto-vf/NotoSans-Italic[wght].ttf")
        if not upright.is_file() or not italic.is_file():
            self.skipTest("SFNT inspection fixture fonts are unavailable")
        regular_metadata = vs._inspect_sfnt_face(upright, 0)
        italic_metadata = vs._inspect_sfnt_face(italic, 0)
        self.assertEqual(regular_metadata["family"], "Noto Sans CJK JP")
        self.assertFalse(regular_metadata["is_italic"])
        self.assertEqual(regular_metadata["italic_angle"], 0.0)
        self.assertFalse(regular_metadata["fs_selection_italic"])
        self.assertTrue(italic_metadata["is_italic"])
        self.assertNotEqual(italic_metadata["post_italic_angle"], 0.0)

    def test_requested_italic_with_upright_actual_face_is_synthetic(self) -> None:
        upright = Path("/usr/share/fonts/redhat-vf/RedHatText[wght].ttf")
        if not upright.is_file():
            self.skipTest("upright SFNT fixture font is unavailable")
        with mock.patch.object(vs, "_edit_font_file", return_value=upright):
            vs._resolve_edit_pdf_font("Red Hat Text", False, True)
        metadata = vs._EDIT_FONT_METADATA[("Red Hat Text", False, True)]
        self.assertFalse(metadata["resolved_is_italic"])
        self.assertEqual(metadata["resolved_italic_angle"], 0.0)
        self.assertTrue(metadata["synthetic_italic"])
        self.assertEqual(metadata["ttc_face_index"], 0)

    def test_actual_formal_italic_face_disables_synthetic_italic(self) -> None:
        italic = Path("/usr/share/fonts/google-noto-vf/NotoSans-Italic[wght].ttf")
        if not italic.is_file():
            self.skipTest("italic SFNT fixture font is unavailable")
        with mock.patch.object(vs, "_edit_font_file", return_value=italic):
            vs._resolve_edit_pdf_font("Noto Sans", False, True)
        metadata = vs._EDIT_FONT_METADATA[("Noto Sans", False, True)]
        self.assertTrue(metadata["resolved_is_italic"])
        self.assertFalse(metadata["synthetic_italic"])
        self.assertNotEqual(metadata["resolved_italic_angle"], 0.0)

    def test_two_font_families_use_distinct_cache_keys_and_registered_names(self) -> None:
        paths = {
            "Red Hat Text": Path("/usr/share/fonts/redhat-vf/RedHatText[wght].ttf"),
            "Red Hat Mono": Path("/usr/share/fonts/redhat-vf/RedHatMono[wght].ttf"),
        }
        if not all(path.is_file() for path in paths.values()):
            self.skipTest("two-font fixtures are unavailable")
        with mock.patch.object(
            vs, "_edit_font_file", side_effect=lambda family, _bold, _italic: paths[family]
        ):
            names = {
                family: vs._resolve_edit_pdf_font(family, False, False)
                for family in paths
            }
        self.assertNotEqual(names["Red Hat Text"], names["Red Hat Mono"])
        metadata_a = vs._EDIT_FONT_METADATA[("Red Hat Text", False, False)]
        metadata_b = vs._EDIT_FONT_METADATA[("Red Hat Mono", False, False)]
        self.assertNotEqual(metadata_a["cache_key"], metadata_b["cache_key"])
        self.assertNotEqual(metadata_a["resolved_font_file"], metadata_b["resolved_font_file"])
        self.assertIn("reportlab_ttfont_subfont_v3", metadata_a["cache_key"])
        self.assertEqual(metadata_a["renderer_revision"], 5)

    def test_same_ttc_path_different_face_index_gets_distinct_registration_name(self) -> None:
        path = Path("/fonts/Collection.ttc")
        registered: list[str] = []
        with mock.patch.object(vs, "_ttc_face_index", side_effect=[0, 1]), \
             mock.patch.object(vs.pdfmetrics, "getRegisteredFontNames", return_value=[]), \
             mock.patch.object(vs.pdfmetrics, "registerFont",
                               side_effect=lambda font: registered.append(font.fontName)), \
             mock.patch.object(vs, "TTFont",
                               side_effect=lambda name, *_a, **_k: mock.Mock(fontName=name)):
            name0 = vs._register_edit_font(path, False, False, family="Family A")
            name1 = vs._register_edit_font(path, False, False, family="Family B")
        self.assertNotEqual(name0, name1)
        self.assertIn("face0", name0)
        self.assertIn("face1", name1)
        self.assertEqual(registered, [name0, name1])

    def test_cmap_reports_japanese_missing_but_latin_supported(self) -> None:
        path = Path("/usr/share/fonts/redhat-vf/RedHatText[wght].ttf")
        if not path.is_file():
            self.skipTest("Latin-only cmap fixture is unavailable")
        metadata = {"resolved_font_file": str(path), "ttc_face_index": 0}
        self.assertEqual(vs._font_missing_characters(metadata, "TEST"), [])
        self.assertEqual(vs._font_missing_characters(metadata, "テキスト"),
                         list("テキスト"))
        text_metadata = vs._font_metadata_for_text(metadata, "ABCテキスト")
        self.assertTrue(text_metadata["glyph_fallback_used"])
        self.assertEqual(text_metadata["fallback_reason"], "missing_glyphs")
        runs = vs._text_font_runs(
            "ABCテキスト", "SelectedLatin", text_metadata, False, True)
        self.assertEqual([run[0] for run in runs], ["ABC", "テキスト"])
        self.assertEqual(runs[0][1], "SelectedLatin")
        self.assertEqual(runs[1][1], vs._FONT_NAME)

    def test_two_fonts_in_one_pdf_have_distinct_resources_and_raster_shapes(self) -> None:
        import fitz
        from reportlab.pdfgen.canvas import Canvas

        paths = {
            "Red Hat Text": Path("/usr/share/fonts/redhat-vf/RedHatText[wght].ttf"),
            "Red Hat Mono": Path("/usr/share/fonts/redhat-vf/RedHatMono[wght].ttf"),
        }
        if not all(path.is_file() for path in paths.values()):
            self.skipTest("two-font fixtures are unavailable")
        with mock.patch.object(
            vs, "_edit_font_file", side_effect=lambda family, _bold, _italic: paths[family]
        ):
            font_a = vs._resolve_edit_pdf_font("Red Hat Text", False, False)
            font_b = vs._resolve_edit_pdf_font("Red Hat Mono", False, False)
        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(500, 200), pageCompression=0)
        vs.draw_styled_pdf_text(canvas, "TEST", 40, 70, font_a, 54)
        vs.draw_styled_pdf_text(canvas, "TEST", 280, 70, font_b, 54)
        canvas.save()
        pdf = output.getvalue()
        page = pypdf.PdfReader(io.BytesIO(pdf)).pages[0]
        font_resources = page["/Resources"]["/Font"]
        custom_resources = [
            str(reference.get_object().get("/BaseFont", ""))
            for reference in font_resources.values()
            if str(reference.get_object().get("/Subtype", "")) == "/TrueType"
        ]
        self.assertEqual(len(custom_resources), 2)
        self.assertEqual(len(set(custom_resources)), 2)
        stream = page.get_contents().get_data()
        used_fonts = set(re.findall(rb"/(F\d+\+?\d*)\s+54\s+Tf", stream))
        self.assertEqual(len(used_fonts), 2)
        with fitz.open(stream=pdf, filetype="pdf") as document:
            left = document[0].get_pixmap(
                matrix=fitz.Matrix(2, 2), clip=fitz.Rect(30, 60, 230, 145),
                colorspace=fitz.csGRAY, alpha=False).samples
            right = document[0].get_pixmap(
                matrix=fitz.Matrix(2, 2), clip=fitz.Rect(270, 60, 470, 145),
                colorspace=fitz.csGRAY, alpha=False).samples
        self.assertNotEqual(left, right)

    def test_pdf_generation_resolves_each_objects_saved_family_independently(self) -> None:
        objects = [
            {"id": "A", "type": "text", "text": "TEST", "x": 30, "y": 30,
             "width": 150, "height": 30, "font_family": "Family A",
             "font_size": 12, "target_vouchers": ["03"]},
            {"id": "B", "type": "text", "text": "TEST", "x": 30, "y": 70,
             "width": 150, "height": 30, "font_family": "Family B",
             "font_size": 12, "target_vouchers": ["03"]},
        ]
        with self.assertLogs("tks_to_kintone_app", level="INFO") as captured, \
             mock.patch.object(vs, "_resolve_edit_pdf_font",
                               side_effect=[vs._FONT_NAME, vs._FONT_NAME]) as resolve:
            vs.build_vouchers_pdf_bytes(["03"], {"pages": [{
                "order_no": "MULTI", "voucher_no": "V1", "details": [],
                "edit_objects": objects,
            }]})
        self.assertEqual(resolve.call_args_list, [
            mock.call("Family A", False, False),
            mock.call("Family B", False, False),
        ])
        logs = "\n".join(captured.output)
        self.assertIn("object_id=A", logs)
        self.assertIn("font_family='Family A'", logs)
        self.assertIn("object_id=B", logs)
        self.assertIn("font_family='Family B'", logs)
        self.assertIn("voucher_no=V1", logs)

    def test_selected_bold_italic_face_precedes_safe_font_fallback(self) -> None:
        selected_path = Path("/fonts/selected-bold-italic.ttf")
        with mock.patch.object(vs, "_edit_font_file",
                               return_value=selected_path) as find_font, \
             mock.patch.object(vs, "_register_edit_font",
                               return_value="SelectedBoldItalic"):
            resolved = vs._resolve_edit_pdf_font("Meiryo", True, True)
        self.assertEqual(resolved, "SelectedBoldItalic")
        find_font.assert_called_once_with("Meiryo", True, True)
        self.assertFalse(vs._EDIT_FONT_METADATA[("Meiryo", True, True)]["fallback_used"])
        self.assertFalse(vs._EDIT_FONT_METADATA[("Meiryo", True, True)]["synthetic_bold"])

    def test_safe_bold_face_precedes_cid_synthetic_bold(self) -> None:
        safe_path = Path("/fonts/safe-bold.ttf")

        def find_font(family: str, bold: bool, italic: bool):
            if family == vs._SAFE_JAPANESE_FONT_FAMILIES[0] and bold and not italic:
                return safe_path
            return None

        with mock.patch.object(vs, "_edit_font_file", side_effect=find_font), \
             mock.patch.object(vs, "_register_edit_font", return_value="SafeBold"):
            resolved = vs._resolve_edit_pdf_font("Missing", True, False)
        self.assertEqual(resolved, "SafeBold")
        metadata = vs._EDIT_FONT_METADATA[("Missing", True, False)]
        self.assertTrue(metadata["fallback_used"])
        self.assertFalse(metadata["synthetic_bold"])

    def test_bold_italic_underline_strikeout_cid_fallback_draws(self) -> None:
        canvas = _TextCanvas()
        with mock.patch.object(vs, "_edit_font_file", return_value=None):
            font_name = vs._resolve_edit_pdf_font("Missing", True, True)
        self.assertEqual(font_name, vs._FONT_NAME)
        vs.draw_text_in_scene_rect(
            canvas, "全装飾", 10, 20, 100, 20, font_name, 12,
            bold=True, underline=True, strikeout=True)
        self.assertEqual(len(canvas.draws), 2)
        self.assertEqual(len(canvas.lines), 2)

    def test_all_01_to_08_generate_with_all_style_combinations_on_cid_fallback(self) -> None:
        data = {"pages": [_page_with_styles()]}
        with mock.patch.object(vs, "_edit_font_file", return_value=None):
            pdf = vs.build_vouchers_pdf_bytes(
                [f"{number:02d}" for number in range(1, 9)], data)
        self.assertTrue(pdf.startswith(b"%PDF"))
        reader = pypdf.PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 8)
        content = b"\n".join(page.get_contents().get_data() for page in reader.pages)
        self.assertIn(b"1 0 .2 1", content)

    def test_file_creation_uses_same_style_generation_path(self) -> None:
        data = {"pages": [_page_with_styles()]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(vs, "_edit_font_file", return_value=None):
            path = vs.create_vouchers_pdf(
                ["03", "04"], data, output_dir=Path(tmp))
            self.assertTrue(path.is_file())
            self.assertEqual(len(pypdf.PdfReader(str(path)).pages), 2)

    def test_internal_name_error_is_logged_but_not_shown_in_user_message(self) -> None:
        with mock.patch.object(vs, "_assemble_pdf_bytes",
                               side_effect=NameError("internal_variable")), \
             mock.patch.object(vs._log, "exception") as logged:
            with self.assertRaisesRegex(RuntimeError, "ログを確認") as caught:
                vs.build_vouchers_pdf_bytes(["03"], {"pages": [_page_with_styles()]})
        self.assertNotIn("internal_variable", str(caught.exception))
        logged.assert_called_once()
        log_args = logged.call_args.args
        self.assertIn("order_no", log_args[0])
        self.assertIn("STYLE-REGRESSION", repr(log_args))


if __name__ == "__main__":
    unittest.main()
