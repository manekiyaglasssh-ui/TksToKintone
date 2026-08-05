import unittest

from app.text_style_resolver import (
    TextStyle,
    decoration_geometry,
    line_height_pt,
)


class TextStyleResolverTests(unittest.TestCase):
    def test_old_and_new_style_keys_are_compatible(self):
        style = TextStyle.from_object({
            "font_family": "HGP創英角ポップ体", "font_size": 36,
            "bold": True, "font_italic": True,
            "font_underline": True, "strikeout": True,
        })
        self.assertEqual(style.family, "HGP創英角ポップ体")
        self.assertEqual(style.size_pt, 36.0)
        self.assertTrue(all((style.bold, style.italic, style.underline, style.strikeout)))

    def test_logical_metrics_are_shared_and_dpi_independent(self):
        self.assertAlmostEqual(line_height_pt(36), 43.2)
        width, underline, strikeout = decoration_geometry(36)
        self.assertAlmostEqual(width, 1.62)
        self.assertAlmostEqual(underline, -4.32)
        self.assertAlmostEqual(strikeout, 10.8)


if __name__ == "__main__":
    unittest.main()
