"""Minimal GIF89a writer (stdlib only), used for panel GIF uploads.

Two things keep the blobs small enough for the tip, which refuses a GIF begin
over roughly 32 KB:

* the LZW stream is emitted **uncompressed** - a CLEAR code, then literal codes,
  with a CLEAR before the dictionary could ever grow. That is legal LZW, needs no
  dictionary on either side, and sidesteps the code-width-change convention that
  strict decoders disagree about;
* frames after the first carry **only the rectangle that changed** (the frames
  are painted over the previous one, disposal method 1), so an animation that
  moves a few cells costs a few dozen bytes a frame instead of a whole frame.
"""

from typing import Sequence

RGB_TUPLE = tuple[int, int, int]


def _lzw_uncompressed(indices: Sequence[int], min_code_size: int) -> bytes:
    """Literal-only LZW: codes never exceed the initial table."""
    clear = 1 << min_code_size
    end = clear + 1
    code_size = min_code_size + 1
    # A decoder adds one dictionary entry per code it reads, so the width would
    # grow after (1 << code_size) - (clear + 2) codes = clear - 2 of them. Flush
    # with CLEAR before that happens, which keeps every code at code_size bits.
    limit = max(1, clear - 2)

    out = bytearray()
    bit_buffer = 0
    bit_count = 0

    def put(code: int) -> None:
        nonlocal bit_buffer, bit_count
        bit_buffer |= code << bit_count
        bit_count += code_size
        while bit_count >= 8:
            out.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8

    put(clear)
    running = 0
    for index in indices:
        put(index)
        running += 1
        if running >= limit:
            put(clear)
            running = 0
    put(end)
    if bit_count:
        out.append(bit_buffer & 0xFF)
    return bytes(out)


def _lzw_compressed(indices: Sequence[int], min_code_size: int) -> bytes:
    """Real LZW: dictionary codes, so mostly-empty frames cost almost nothing.

    The literal-only writer above is simple but charges for every cell inside
    the changed rectangle, which is what caps an animation at a few dozen
    frames. Compressing means a frame that repaints a band across the whole
    panel costs the cells it changed instead.

    GIF's deferred convention: a table entry is added for the string that was
    just rejected, the decoder learns one code behind, and the code width
    follows the same lag. A CLEAR is emitted when the table is full.
    """
    clear = 1 << min_code_size
    end = clear + 1
    code_size = min_code_size + 1
    last_code = (1 << 12) - 1  # GIF's dictionary ceiling

    table: dict[tuple[int, int], int] = {}
    next_code = clear + 2

    out = bytearray()
    bit_buffer = 0
    bit_count = 0

    def put(code: int) -> None:
        nonlocal bit_buffer, bit_count
        bit_buffer |= code << bit_count
        bit_count += code_size
        while bit_count >= 8:
            out.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8

    def reset() -> None:
        nonlocal table, next_code, code_size
        table = {}
        next_code = clear + 2
        code_size = min_code_size + 1

    if not indices:
        put(clear)
        put(end)
        if bit_count:
            out.append(bit_buffer & 0xFF)
        return bytes(out)

    put(clear)
    previous = int(indices[0])
    for code in indices[1:]:
        code = int(code)
        pair = (previous, code)
        found = table.get(pair)
        if found is not None:
            previous = found
            continue
        put(previous)
        if next_code <= last_code:
            table[pair] = next_code
            next_code += 1
            if next_code > (1 << code_size):
                code_size += 1
        else:
            # Table full: clear and start learning again.
            put(clear)
            reset()
        previous = code
    put(previous)
    put(end)
    if bit_count:
        out.append(bit_buffer & 0xFF)
    return bytes(out)


def _changed_box(previous: Sequence[int], current: Sequence[int], width: int):
    """Bounding box of the pixels that differ, or None when nothing does.

    Painting just that rectangle over the previous frame is what a disposal
    method of 1 means, so the result is identical to resending the whole frame.
    """
    left = top = None
    right = bottom = -1
    for index, (before, after) in enumerate(zip(previous, current)):
        if before == after:
            continue
        x, y = index % width, index // width
        left = x if left is None else min(left, x)
        top = y if top is None else min(top, y)
        right = max(right, x)
        bottom = max(bottom, y)
    if left is None:
        return None
    return left, top, right - left + 1, bottom - top + 1


def _patch(current: Sequence[int], width: int, box) -> list:
    """The rows inside ``box``, row-major, as the frame's own pixel list."""
    left, top, box_w, box_h = box
    cells: list = []
    for y in range(top, top + box_h):
        start = y * width + left
        cells.extend(current[start : start + box_w])
    return cells


def _color_table_bits(palette: Sequence[RGB_TUPLE]) -> int:
    bits = 1
    while (1 << bits) < max(2, len(palette)):
        bits += 1
    return bits


def encode(
    width: int,
    height: int,
    palette: Sequence[RGB_TUPLE],
    frames: Sequence[Sequence[int]],
    *,
    delay_cs: int = 100,
    loop: bool = True,
    difference: bool = True,
    compress: bool = False,
) -> bytes:
    """One GIF from a palette plus per-frame index lists (row-major, w*h long).

    ``difference`` sends each frame after the first as only the rectangle that
    changed against the frame before it. Turn it off to resend every frame in
    full, which is ~2 KB a frame on this panel.

    ``compress`` uses real LZW instead of literal-only codes: the same frames
    cost a fraction of the bytes when the changed rectangle is mostly empty,
    which is the difference between an animation that fits the tip's GIF buffer
    and one that does not.
    """
    emit = _lzw_compressed if compress else _lzw_uncompressed
    bits = _color_table_bits(palette)
    table_entries = 1 << bits
    min_code_size = max(2, bits)

    gif = bytearray(b"GIF89a")
    gif += width.to_bytes(2, "little")
    gif += height.to_bytes(2, "little")
    # Packed field: global color table flag plus the table-size exponent.
    gif.append(0x80 | (bits - 1))
    gif.append(0x00)  # background color index
    gif.append(0x00)  # pixel aspect ratio
    for entry in range(table_entries):
        rgb = palette[entry] if entry < len(palette) else (0, 0, 0)
        gif += bytes([max(0, min(255, int(c))) for c in rgb])

    if loop:
        # Netscape looping extension; a loop count of 0 means "forever".
        gif += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"

    previous: Sequence[int] | None = None
    for indices in frames:
        box = (0, 0, width, height)
        if difference and previous is not None:
            # A frame that changes nothing still has to exist (it carries the
            # delay), so it paints a single pixel of what is already there.
            box = _changed_box(previous, indices, width) or (0, 0, 1, 1)
        left, top, box_w, box_h = box

        # Graphic Control Extension: byte count 0x04 covers flags, the 2-byte
        # delay and the transparent index. Disposal method 1 = leave what this
        # frame painted, which is what makes a partial frame correct.
        gif += b"\x21\xF9\x04\x04"
        gif += int(delay_cs).to_bytes(2, "little")
        gif += b"\x00"  # transparent index
        gif.append(0x00)  # block terminator

        gif += b"\x2C"  # image separator
        gif += int(left).to_bytes(2, "little")
        gif += int(top).to_bytes(2, "little")
        gif += int(box_w).to_bytes(2, "little")
        gif += int(box_h).to_bytes(2, "little")
        gif.append(0x00)  # no local color table, not interlaced
        data = emit(_patch(indices, width, box), min_code_size)
        gif.append(min_code_size)
        for offset in range(0, len(data), 255):
            block = data[offset : offset + 255]
            gif.append(len(block))
            gif += block
        gif.append(0x00)  # end of the image data sub-blocks
        previous = indices

    gif.append(0x3B)  # trailer
    return bytes(gif)
