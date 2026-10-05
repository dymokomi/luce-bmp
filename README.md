# luce-bmp

A BMP decoder for Luce/Base: Windows and OS/2 headers, 1- to 32-bit pixels, RLE,
bitfields and alpha, and the bare DIBs inside icons. It depends on no other package: only
the compiler's built-in modules. The browser engine, luce-image and luce-ico decode
bitmaps through it.

```luce
from luce_bmp import bmp

let found = try bmp.info(data)                    # size, depth, compression, header
let pixels = try new u8[found.width * found.height * 4] ---
try bmp.decode_rgba8(data, pixels)                # straight RGBA, top row first

if let payload = bmp.embedded(data):              # a BI_PNG or BI_JPEG bitmap
    decode_with_png_or_jpeg(payload)

let entry = try bmp.info_dib(dib, bmp.Options(icon = true))     # an ICO or CUR entry
try bmp.decode_dib_rgba8(dib, pixels, bmp.Options(icon = true))
```

## API

- `info(data) -> Info!` for a BMP file: `width`, `height`, `bits` (1, 2, 4, 8, 16, 24 or
  32; 0 for an embedded JPEG or PNG), `compression` (`rgb`, `rle8`, `rle4`, `bitfields`,
  `alpha_bitfields`, `jpeg`, `png`, `rle24`, `huffman`), `header_size` (12 OS/2 1.x, 40,
  52, 56, 108 V4, 124 V5, 16 to 64 OS/2 2.x), `top_down`, `colors` (palette entries) and
  `alpha` (whether decoded pixels can be transparent).
- `decode_rgba8(data, out, options)`: the pixels into `out`, at least
  `width * height * 4` bytes.
- `info_dib(data, options)` and `decode_dib_rgba8(data, out, options)`: the same for a
  bare DIB, an info header first. `Options.icon` reads an ICO or CUR entry: the header's
  height counts the colour rows and the AND mask rows after them, a 32-bit image's fourth
  byte is alpha, and where the image has no alpha of its own the mask's set bits make
  their pixels transparent.
- `embedded(data) -> const u8[]?`: the JPEG or PNG a BI_JPEG or BI_PNG bitmap holds, a
  view of `data`; decoding such a bitmap fails with `unsupported`.
- `Options.max_pixels` (default 2^28) bounds the picture.
- Errors: `corrupt` (not a BMP, or headers that are damaged or contradict one another),
  `unsupported` (OS/2 Huffman 1D, 64-bit pixels, OS/2 bitmap arrays, an embedded JPEG or
  PNG, an unknown compression), `limit`, `invalid` (an output buffer too small).

## Browser behaviour

Where the format is loose, this decoder does what Chrome does:

- Each compression must suit its depth (bitfields 16 or 32 bits, RLE8 up to 8, RLE4 up
  to 4, RLE24 and Huffman only in OS/2 2.x headers), and compressed bitmaps cannot be
  top-down. Bitfield masks that overlap or have gaps are refused.
- A 32-bit BI_RGB bitmap's fourth byte is not alpha (most writers leave garbage there);
  alpha is read from a 56-byte or larger header's alpha mask, from BI_ALPHABITFIELDS, and
  in icons. An image whose alpha is zero throughout is opaque.
- Channels under 8 bits scale to the full range, rounding (5-bit 31 is 255, 1 is 8);
  wider ones keep their top 8 bits.
- Pixels an RLE stream skips (deltas, early row and bitmap ends) are transparent; runs
  past the end of a row are cut off there. Indices past the palette are opaque black.
- Palettes are as long as `biClrUsed` says (at most 2^bits), cut where the pixels begin.
  It is more lenient where files are damaged: a file cut short decodes the rows it holds
  (the rest stay transparent); a pixel offset of 0, inside the headers, or leaving a
  paletted bitmap no palette is read as "right after the palette"; a gap of exactly three
  bytes a colour holds 3-byte entries (some OS/2 2.x writers).

## Tests

```
./test.sh                                   # unit tests, native and C, every compiler; -W, fmt
LUCE_BASE_EXTRA=~/.local/bin/luce-base ./test.sh     # and a second toolchain
python3 tools/conformance.py --python VENV/bin/python --list-failing
python3 tools/fuzz.py --cases 20000 [--guard-malloc]
```

The unit tests (`tests/bmp/`) write their bitmaps with a small builder. `tools/bmpcheck`
decodes a list of files; `tools/conformance.py` compares the pixels with two oracles on
corpora kept outside the repository in `../.donors/`: BMP Suite 2.8's reference PNGs (any
of the alternatives it allows), and Python Pillow on every BMP of BMP Suite, Pillow's test
images and Ladybird's LibGfx test inputs:

| Oracle | Files | Match | Differ on purpose | Unexplained |
| --- | ---: | ---: | ---: | ---: |
| BMP Suite references (g/, q/, x/) | 72 | 64 | 8 | 0 |
| Pillow 12.3 | 108 | 67 | 41 | 0 |

BMP Suite's 20 bad files (b/) decode (15) or fail cleanly (5). The deliberate
differences, each listed with its reason by `--list-failing`:

- From BMP Suite's references: OS/2 Huffman 1D, 64-bit pixels and bitmap arrays are not
  decoded, as Chrome does not decode them (3); channels wider than 8 bits keep their top
  8 bits as Chrome and Firefox do where the references round (4); colour profiles are not
  applied (1).
- From Pillow: Pillow rejects 18 files whose layout it does not read (2-bit pixels, OS/2
  2.x headers, RLE24, uncommon bitfields, BI_ALPHABITFIELDS) and 2 cut short; it paints
  RLE-skipped pixels with palette colour 0 where browsers leave them transparent (10);
  it replicates bits where Chrome and BMP Suite's references round (6); it misreads one
  RLE4 file the suite's reference agrees with us on; it reads a 3-byte OS/2 palette as
  4-byte (Ladybird expects what we decode); it opens an ICO named .bmp; it accepts a
  top-down RLE bitmap Chrome refuses; it keeps an alpha channel zero throughout.

`tools/fuzz.py` mutates the corpora (20,000 cases: bit flips, insertions, truncations,
spliced chunks, BMP's header and RLE values; a fifth read as bare and icon DIBs) and
decodes each under a time limit, after a stress phase (2048 x 2048 bitfields, RLE8 of
single-pixel runs and of deltas, a million row ends, a 65535 x 65535 header, a 2048 x
2048 icon with its mask, each well under two seconds): no traps, no hangs, the peak
memory of a decoding process 66 MB. Under Guard Malloc (`--guard-malloc`, and the unit
tests run with `DYLD_INSERT_LIBRARIES=/usr/lib/libgmalloc.dylib`) nothing is reported.
