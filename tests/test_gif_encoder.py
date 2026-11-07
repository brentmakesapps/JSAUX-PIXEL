import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "decky-plugin"))

from backend import gif_encoder  # noqa: E402


def parse(gif: bytes):
    """Very small GIF reader: header, global table, and image blocks."""
    assert gif[:6] in (b"GIF87a", b"GIF89a")
    width = int.from_bytes(gif[6:8], "little")
    height = int.from_bytes(gif[8:10], "little")
    bits = (gif[10] & 0x07) + 1
    pos = 13 + 3 * (1 << bits)
    images = []
    while pos < len(gif) and gif[pos] != 0x3B:
        marker = gif[pos]
        if marker == 0x21:
            pos += 2
            while gif[pos]:
                pos += 1 + gif[pos]
            pos += 1
        elif marker == 0x2C:
            left = int.from_bytes(gif[pos + 1 : pos + 3], "little")
            top = int.from_bytes(gif[pos + 3 : pos + 5], "little")
            w = int.from_bytes(gif[pos + 5 : pos + 7], "little")
            h = int.from_bytes(gif[pos + 7 : pos + 9], "little")
            packed = gif[pos + 9]
            min_code_size = gif[pos + 10]
            pos += 11
            stream = bytearray()
            while gif[pos]:
                stream += gif[pos + 1 : pos + 1 + gif[pos]]
                pos += 1 + gif[pos]
            pos += 1
            images.append((left, top, w, h, packed, min_code_size, bytes(stream)))
        else:
            # Unknown extension or padding: step over it.
            pos += 1
    return width, height, bits, images


def decode_literals(stream: bytes, min_code_size: int):
    """Decode the writer's literal-only LZW (CLEAR keeps every code the same width)."""
    clear = 1 << min_code_size
    end = clear + 1
    width = min_code_size + 1
    out = []
    acc = 0
    bits = 0
    for byte in stream:
        acc |= byte << bits
        bits += 8
        while bits >= width:
            code = acc & ((1 << width) - 1)
            acc >>= width
            bits -= width
            if code == clear:
                continue
            if code == end:
                return out
            assert code < clear, f"dictionary code {code} in a literal-only stream"
            out.append(code)
    return out


class GifEncoderTests(unittest.TestCase):
    PALETTE = [(0, 0, 0), (255, 255, 255), (40, 220, 110), (250, 170, 40)]

    def test_round_trip_indices(self) -> None:
        frame = [(x + y) % len(self.PALETTE) for y in range(54) for x in range(64)]
        gif = gif_encoder.encode(64, 54, self.PALETTE, [frame], delay_cs=10)
        width, height, bits, images = parse(gif)
        self.assertEqual((width, height), (64, 54))
        self.assertEqual(bits, 2)
        self.assertEqual(len(images), 1)
        left, top, w, h, packed, min_code_size, stream = images[0]
        self.assertEqual((left, top, w, h, packed), (0, 0, 64, 54, 0))
        self.assertEqual(w * h, 64 * 54)
        self.assertGreaterEqual(min_code_size, 2)
        self.assertEqual(decode_literals(stream, min_code_size), frame)

    def compose(self, width: int, height: int, images) -> list:
        """Composite the way a GIF decoder does: later frames paint over earlier."""
        canvas = [0] * (width * height)
        for left, top, w, h, _packed, min_code_size, stream in images:
            cells = decode_literals(stream, min_code_size)
            self.assertEqual(len(cells), w * h)
            for row in range(h):
                base = (top + row) * width + left
                canvas[base : base + w] = cells[row * w : (row + 1) * w]
        return canvas

    def test_partial_frames_composite_to_the_same_frames(self) -> None:
        width, height = 32, 16
        base = [1] * (width * height)
        moved = list(base)
        for index in (5, 6, 7, 8, 40, 41):
            moved[index] = 2
        moved[60] = 3
        gif = gif_encoder.encode(width, height, self.PALETTE, [base, moved, list(moved)])
        _, _, _, images = parse(gif)
        self.assertEqual(len(images), 3)
        # The first frame is whole, the next one only covers what changed.
        self.assertEqual(images[0][2:4], (width, height))
        self.assertLess(images[1][2] * images[1][3], width * height)
        canvas = self.compose(width, height, images[:2])
        self.assertEqual(canvas, moved)

    def test_differencing_keeps_an_animation_small(self) -> None:
        width, height = 64, 54
        frames = []
        for step in range(16):
            frame = [0] * (width * height)
            for cell in range(4):
                frame[(step + cell) % (width * height)] = 2
            frames.append(frame)
        patched = gif_encoder.encode(width, height, self.PALETTE, frames, delay_cs=8)
        whole = gif_encoder.encode(width, height, self.PALETTE, frames, delay_cs=8,
                                   difference=False)
        self.assertLess(len(patched) * 3, len(whole))

    def test_an_unchanged_frame_still_carries_its_delay(self) -> None:
        frame = [1] * 16
        gif = gif_encoder.encode(4, 4, self.PALETTE, [frame, list(frame)])
        _, _, _, images = parse(gif)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[1][2:4], (1, 1))

    def test_global_color_table_is_a_power_of_two(self) -> None:
        palette = self.PALETTE + [(9, 9, 9), (8, 8, 8)]
        gif = gif_encoder.encode(8, 8, palette, [[0] * 64])
        _, _, bits, _ = parse(gif)
        self.assertEqual(1 << bits, 8)

    def test_palette_entries_are_clamped(self) -> None:
        gif = gif_encoder.encode(2, 1, [(300, -5, 7)], [[0, 1]])
        table = gif[13:16]
        self.assertEqual(table, bytes([255, 0, 7]))

    def test_every_frame_is_framed(self) -> None:
        frames = [[0, 1] * 2, [1, 0] * 2]
        gif = gif_encoder.encode(2, 2, self.PALETTE, frames)
        self.assertEqual(len(parse(gif)[3]), 2)

    def test_trailer_present(self) -> None:
        gif = gif_encoder.encode(2, 2, self.PALETTE, [[0, 1] * 2])
        self.assertEqual(gif[-1], 0x3B)


if __name__ == "__main__":
    unittest.main()
