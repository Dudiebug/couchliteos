import testenv  # noqa: F401  (first: scratch run and state directories)
import os
import pathlib
import shutil
import tempfile
import unittest

import couchliteos_artwork as artwork
import couchliteos_tile as tile

try:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):
    GdkPixbuf = None

SIZE = 48  # small: compose() is plain Python


def picture(width, height, paint):
    """RGBA bytes; paint(x, y) gives (r, g, b, a)."""
    return b"".join(bytes(paint(x, y)) for y in range(height) for x in range(width))


def pixel(rgba, size, x, y):
    at = (y * size + x) * 4
    return tuple(rgba[at:at + 4])


def disc(colour):
    def paint(x, y):
        inside = (x - 9.5) ** 2 + (y - 9.5) ** 2 <= 7 ** 2
        return (*colour, 255) if inside else (0, 0, 0, 0)
    return paint


def rounded_square(colour, radius=5):
    def paint(x, y):
        dx, dy = max(radius - x, x - (19 - radius), 0), max(radius - y, y - (19 - radius), 0)
        return (*colour, 255) if dx * dx + dy * dy <= radius * radius else (0, 0, 0, 0)
    return paint


class PlanTest(unittest.TestCase):
    def test_a_logo_on_transparency_sits_on_a_light_tile(self):
        how = tile.plan(picture(20, 20, disc((200, 20, 30))), 20, 20)
        self.assertFalse(how.full_bleed)
        self.assertEqual((how.top, how.bottom), tile.LIGHT_TILE)

    def test_a_light_logo_sits_on_a_dark_tile(self):
        how = tile.plan(picture(20, 20, disc((250, 250, 250))), 20, 20)
        self.assertEqual((how.top, how.bottom), tile.DARK_TILE)

    def test_a_square_logo_is_the_whole_tile_in_its_own_colour(self):
        how = tile.plan(picture(20, 20, lambda x, y: (20, 40, 200, 255)), 20, 20)
        self.assertTrue(how.full_bleed)
        self.assertEqual(how.square, (0.0, 0.0, 1.0, 1.0))
        self.assertAlmostEqual(how.top[2], 200 / 255)

    def test_a_rounded_square_logo_counts_as_a_square(self):
        how = tile.plan(picture(20, 20, rounded_square((10, 10, 10))), 20, 20)
        self.assertTrue(how.full_bleed)

    def test_a_rounded_square_with_a_margin_is_fitted_by_its_own_edges(self):
        def paint(x, y):
            return (10, 10, 10, 255) if 2 <= x < 18 and 2 <= y < 18 else (0, 0, 0, 0)
        how = tile.plan(picture(20, 20, paint), 20, 20)
        self.assertTrue(how.full_bleed)
        self.assertEqual(how.square, (0.1, 0.1, 0.9, 0.9))
        x, y, width, height = tile.logo_box(how, 20, 20, SIZE)
        left, top, side = tile.tile_rect(SIZE)
        self.assertAlmostEqual(x + width * 0.1, left, delta=1)
        self.assertAlmostEqual(x + width * 0.9, left + side, delta=1)

    def test_an_empty_picture_is_not_an_error(self):
        how = tile.plan(bytes(20 * 20 * 4), 20, 20)
        self.assertEqual((how.full_bleed, how.top), (False, tile.LIGHT_TILE[0]))

    def test_rgba_that_does_not_match_its_size_is_refused(self):
        with self.assertRaises(ValueError):
            tile.plan(b"\0" * 10, 20, 20)


class BoxTest(unittest.TestCase):
    def test_a_logo_fits_in_the_middle(self):
        how = tile.Plan(False, *tile.LIGHT_TILE)
        x, y, width, height = tile.logo_box(how, 100, 50, 256)
        _left, _top, side = tile.tile_rect(256)
        self.assertEqual(width, round(side * tile.LOGO))
        self.assertEqual(height, round(side * tile.LOGO / 2))
        self.assertAlmostEqual(x + width / 2, 128, delta=1)

    def test_a_tiny_icon_is_not_blown_up_past_max_upscale(self):
        how = tile.Plan(False, *tile.LIGHT_TILE)
        self.assertEqual(tile.logo_box(how, 16, 16, 256)[2:], (48, 48))
        bleed = tile.Plan(True, (0, 0, 0), (0, 0, 0))
        self.assertEqual(tile.logo_box(bleed, 16, 16, 256)[2:], (48, 48))


class ComposeTest(unittest.TestCase):
    def compose(self, paint, size=20):
        logo = picture(size, size, paint)
        how = tile.plan(logo, size, size)
        box = tile.logo_box(how, size, size, SIZE)
        # Scaled as GdkPixbuf would: nearest neighbour is enough here.
        scaled = picture(box[2], box[3], lambda x, y: tuple(logo[((y * size // box[3]) * size + x * size // box[2]) * 4:][:4]))
        return tile.compose(scaled, box, how, SIZE)

    def test_size_corners_shadow_and_gloss(self):
        out = self.compose(disc((200, 20, 30)))
        self.assertEqual(len(out), SIZE * SIZE * 4)
        self.assertEqual(pixel(out, SIZE, 0, 0)[3], 0)  # outside the rounded corner, above the shadow
        left, top, side = tile.tile_rect(SIZE)
        bottom = round(top + side)
        self.assertGreater(pixel(out, SIZE, SIZE // 2, bottom)[3], 0)  # the shadow under the tile
        self.assertLess(pixel(out, SIZE, SIZE // 2, bottom)[3], 255)
        middle = pixel(out, SIZE, SIZE // 2, SIZE // 2)
        self.assertEqual(middle[3], 255)
        self.assertGreater(middle[0], 150)  # the logo shows through
        upper = pixel(out, SIZE, round(left + 3), round(top + 4))
        lower = pixel(out, SIZE, round(left + 3), round(top + side - 4))
        self.assertGreater(sum(upper[:3]), sum(lower[:3]))  # gloss and light from above

    def test_a_square_logo_covers_the_tile(self):
        out = self.compose(lambda x, y: (20, 40, 200, 255))
        left, top, side = tile.tile_rect(SIZE)
        near_bottom = pixel(out, SIZE, SIZE // 2, round(top + side - 3))
        self.assertGreater(near_bottom[2], near_bottom[0] + 60)  # blue to the edge, no light tile


@unittest.skipIf(GdkPixbuf is None, "needs GdkPixbuf (python3-gi)")
class MakeTileTest(unittest.TestCase):
    def png(self, width=64, height=64, alpha=True):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, alpha, 8, width, height)
        pixbuf.fill(0x2040C0FF)
        return bytes(pixbuf.save_to_bufferv("png", [], [])[1])

    def test_a_png_becomes_a_tile_png(self):
        data = tile.make_tile(self.png(), size=64)
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(data)
        loader.close()
        made = loader.get_pixbuf()
        self.assertEqual((made.get_width(), made.get_height(), made.get_has_alpha()), (64, 64, True))

    def test_an_svg_file_and_a_jpeg_without_alpha(self):
        with tempfile.TemporaryDirectory() as folder:
            svg = pathlib.Path(folder) / "app.svg"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                           '<circle cx="12" cy="12" r="9" fill="#e33"/></svg>')
            self.assertTrue(tile.make_tile(path=svg, size=64).startswith(b"\x89PNG"))
        self.assertTrue(tile.make_tile(self.png(alpha=False), size=64).startswith(b"\x89PNG"))

    def test_garbage_and_huge_input_are_refused(self):
        with self.assertRaises(artwork.ArtworkError):
            tile.make_tile(b"\x89PNG\r\n\x1a\nnot really", size=64)
        with self.assertRaises(artwork.ArtworkError):
            tile.make_tile(b"\x89PNG\r\n\x1a\n" + b"x" * tile.MAX_SOURCE, size=64)


class TilesTest(unittest.TestCase):
    def setUp(self):
        self.folder = pathlib.Path(tempfile.mkdtemp())
        self.icon = self.folder / "icon.svg"
        self.icon.write_text("<svg/>")
        self.made = []

    def tearDown(self):
        shutil.rmtree(self.folder)

    def tiles(self, fail=False, limit=10):
        def make(path):
            self.made.append(path)
            if fail:
                raise artwork.ArtworkError("broken")
            return b"\x89PNG tile"
        return tile.Tiles(self.folder / "tiles", make=make, limit=limit)

    def test_made_once_then_drawn_from_the_cache(self):
        tiles = self.tiles()
        self.assertTrue(tiles.run_job(self.icon))
        self.assertTrue(tiles.take_changed())
        self.assertFalse(tiles.take_changed())
        found = tiles.tile(self.icon)
        self.assertEqual(found.read_bytes(), b"\x89PNG tile")
        self.assertEqual(self.made, [self.icon])

    def test_a_changed_icon_gets_a_new_tile(self):
        tiles = self.tiles()
        tiles.run_job(self.icon)
        before = tiles.name(self.icon)
        os.utime(self.icon, ns=(1, 1))
        self.assertNotEqual(tiles.name(self.icon), before)
        tiles._started = True  # no worker thread: the job stays queued
        self.assertIsNone(tiles.tile(self.icon))  # queued, not made yet

    def test_a_broken_icon_is_not_tried_again(self):
        tiles = self.tiles(fail=True)
        self.assertFalse(tiles.run_job(self.icon))
        self.assertIsNone(tiles.tile(self.icon))
        self.assertEqual(len(self.made), 1)
        self.assertIsNone(tiles.tile(self.folder / "missing.svg"))

    def test_the_oldest_tiles_go_past_the_limit(self):
        tiles = self.tiles(limit=2)
        for index in range(4):
            icon = self.folder / f"{index}.svg"
            icon.write_text("<svg/>")
            os.utime(icon, ns=(index, index))
            tiles.run_job(icon)
        self.assertEqual(len(list((self.folder / "tiles").glob("*.png"))), 2)


if __name__ == "__main__":
    unittest.main()
