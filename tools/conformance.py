#!/usr/bin/env python3
"""Decode reference BMP corpora with luce-bmp and compare the pixels.

Two oracles:

- BMP Suite 2.8 (https://entropymine.com/jason/bmpsuite/): its html/bmpsuite.html pairs
  each good (g/) and questionable (q/) image with the reference PNG(s) of how it should
  look; any one of the alternatives it offers is a match. Bad (b/) images have no
  reference: they must decode or fail cleanly.
- Python Pillow, run here, on every BMP: BMP Suite, Pillow's Tests/images and Ladybird's
  LibGfx test inputs.

Pixels compare exactly, except that two fully transparent pixels are equal whatever
their colour. A bitmap holding a JPEG or PNG is checked by decoding the payload luce-bmp
hands back with Pillow. The corpora live in ../.donors (never in this repository):

  ../.donors/bmpsuite/bmpsuite-2.8
  ../.donors/pillow-tests/Tests/images
  ../.donors/ladybird-pin/Tests/LibGfx/test-inputs/bmp

  python3 tools/conformance.py [--list-failing] [--python PYTHON-WITH-PILLOW]

LUCE_BASE names the compiler (default: luce-base on PATH).
"""
import argparse, json, os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DONORS = ROOT.parent / ".donors"
SUITE = DONORS / "bmpsuite/bmpsuite-2.8"
CORPORA = {
    "bmpsuite": SUITE,
    "pillow": DONORS / "pillow-tests/Tests/images",
    "ladybird": DONORS / "ladybird-pin/Tests/LibGfx/test-inputs/bmp",
}
TOOL = ROOT / "build/bmpcheck"

# Where we differ from an oracle on purpose, following Chrome: file name -> why.
WIDE = "channels wider than 8 bits keep their top 8 bits, as Chrome and Firefox do; the reference rounds"
NARROW = "channels under 8 bits scale with rounding, as Chrome does and the suite's references show; Pillow replicates bits"
UNSUPPORTED = "not decoded, as in Chrome: {}"
REJECTED = "Pillow rejects a layout it does not read (2-bit, OS/2 2.x, RLE24, odd bitfields) or a damaged header; we decode it"
SKIPPED = "pixels an RLE stream skips are transparent, as in Chrome and Firefox; Pillow paints palette colour 0"
DELIBERATE = {
    "suite": {
        "q/pal1huffmsb.bmp": UNSUPPORTED.format("OS/2 Huffman 1D"), "q/rgba64.bmp": UNSUPPORTED.format("64-bit pixels"),
        "x/ba-bm.bmp": UNSUPPORTED.format("an OS/2 bitmap array"),
        "q/rgb24prof2.bmp": "colour profiles are not applied (this file swaps red and green through one)",
        "q/rgb16-3103.bmp": WIDE, "q/rgb32-111110.bmp": WIDE, "q/rgba32-1010102.bmp": WIDE, "q/rgba32-61754.bmp": WIDE,
    },
    "pillow": {
        "bmpsuite/rletopdown.bmp": "a top-down RLE bitmap is rejected, as Chrome rejects it",
        "bmpsuite/pal4rle.bmp": "Pillow 12 misreads RLE4 runs here; the suite's reference agrees with us",
        "bmpsuite/rgb16-565.bmp": NARROW, "bmpsuite/rgb16-565pal.bmp": NARROW, "bmpsuite/rgb16.bmp": NARROW,
        "bmpsuite/rgb16bfdef.bmp": NARROW, "bmpsuite/rgb16faketrns.bmp": NARROW,
        "bmpsuite/rgba16-5551.bmp": NARROW + "; it also drops the 1-bit alpha",
        "ladybird/os2_3bpc.bmp": "3-byte palette entries an OS/2 2.x writer left; Pillow reads 4 (Ladybird expects black and white)",
        "ladybird/oss-fuzz-testcase-62541.bmp": "an ICO named .bmp is not a BMP; Pillow opens files by their content",
        "pillow/rgb32bf-abgr.bmp": "an alpha channel zero throughout is opaque, as Chrome shows it",
        "pillow/l2rgb_read.bmp": "a file cut short decodes the rows it holds; Pillow rejects it",
        "bmpsuite/shortfile.bmp": "a file cut short decodes the rows it holds; Pillow rejects it",
        **{f"bmpsuite/{name}.bmp": SKIPPED for name in [
            "badrle", "badrle4", "badrle4bis", "badrle4ter", "badrlebis", "badrleter", "pal4rlecut", "pal4rletrns",
            "pal8rlecut", "pal8rletrns"]},
        **{f"bmpsuite/{name}.bmp": REJECTED for name in [
            "badpalettesize", "rgb16-880", "rgb32bf", "pal2", "pal2color", "pal8os2v2-16", "pal8oversizepal", "rgb16-231",
            "rgb16-3103", "rgb24rle24", "rgb32-111110", "rgb32-7187", "rgba16-1924", "rgba16-4444", "rgba32-1010102",
            "rgba32-61754", "rgba32-81284", "rgba32abf"]},
    },
}


def build():
    base = os.environ.get("LUCE_BASE", "luce-base")
    TOOL.parent.mkdir(exist_ok=True)
    subprocess.run([base, "build", str(ROOT / "tools/bmpcheck.lucb"), "-o", str(TOOL), "--release"], cwd=ROOT, check=True)


# Pillow's pixels: per path, an error, or the size and the RGBA written to a file. Paths
# after "--payload" are a bitmap's embedded JPEG or PNG, decoded the same way.
PILLOW = r"""
import json, sys
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
out = {}
for index, path in enumerate(sys.argv[2:]):
    try:
        with Image.open(path) as image:
            target = f"{sys.argv[1]}/{index}.pillow"
            open(target, "wb").write(image.convert("RGBA").tobytes())
            out[path] = {"size": list(image.size), "rgba": target}
    except Exception as failure:
        out[path] = {"error": f"{type(failure).__name__}: {failure}"}
print(json.dumps(out))
"""


def decode(paths, directory):
    """Run bmpcheck over `paths`: per path, what it reported, pixels included."""
    listing = Path(directory) / "list.txt"
    listing.write_text("".join(f"- {p}\n" for p in paths))
    result = subprocess.run([str(TOOL), str(listing), directory], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        sys.exit(f"bmpcheck failed:\n{result.stderr}")
    found = {}
    for line in result.stdout.splitlines():
        number, status, *rest = line.split(" ", 2)
        path = paths[int(number)]
        if status == "ok":
            width, height = rest[0].split(" ")[:2]
            found[path] = {"size": [int(width), int(height)], "rgba": (Path(directory) / f"{number}.rgba").read_bytes()}
        elif status == "embedded":
            found[path] = {"embedded": str(Path(directory) / f"{number}.payload")}
        else:
            found[path] = {"error": " ".join(rest)}
    return found


def pillow(paths, directory, python):
    result = subprocess.run([python, "-c", PILLOW, directory, *paths], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        sys.exit(f"Pillow failed:\n{result.stderr}")
    return json.loads(result.stdout)


def differing(a, b):
    count = 0
    for at in range(0, min(len(a), len(b)), 4):
        if a[at + 3] == 0 and b[at + 3] == 0:
            continue
        if a[at:at + 4] != b[at:at + 4]:
            count += 1
    return count + abs(len(a) - len(b)) // 4


def suite_references():
    """BMP Suite's references: suite-relative BMP path -> [reference PNG paths]."""
    text = (SUITE / "html/bmpsuite.html").read_text(encoding="latin-1")
    references = {}
    for row in re.findall(r"<tr>(.*?)</tr>", text, flags=re.S):
        images = re.findall(r'<img src="([^"]+)"', row)
        bitmaps = [i for i in images if i.endswith(".bmp")]
        pictures = [str(SUITE / "html" / i) for i in images if not i.endswith(".bmp")]
        for bitmap in bitmaps:
            if pictures:
                references[bitmap.replace("../", "")] = pictures
    return references


def compare(got, theirs):
    """'' when `got` matches Pillow's `theirs`, else what differs."""
    if "error" in got:
        return f"we fail ({got['error'][:60]})"
    if got["size"] != theirs["size"]:
        return f"size {got['size']} vs {theirs['size']}"
    count = differing(got["rgba"], Path(theirs["rgba"]).read_bytes())
    return f"{count} px differ" if count else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-failing", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--no-build", action="store_true")
    options = parser.parse_args()
    if not options.no_build:
        build()
    paths = sorted(str(p) for key, corpus in CORPORA.items() for p in (corpus.glob("*/*.bmp") if key == "bmpsuite" else corpus.glob("*.bmp")))
    references = suite_references()
    with tempfile.TemporaryDirectory() as directory:
        ours = decode(paths, directory)
        pictures = sorted({p for group in references.values() for p in group})
        payloads = [v["embedded"] for v in ours.values() if "embedded" in v]
        theirs = pillow(paths + pictures + payloads, directory, options.python)
        suite_rows, pillow_rows = [], []
        for bitmap, group in sorted(references.items()):
            got = ours[str(SUITE / bitmap)]
            if "embedded" in got:
                got = {"size": theirs[got["embedded"]]["size"], "rgba": Path(theirs[got["embedded"]]["rgba"]).read_bytes()} \
                    if "error" not in theirs[got["embedded"]] else {"error": "payload Pillow cannot read"}
            verdicts = [compare(got, theirs[p]) for p in group]
            suite_rows.append((bitmap, "" if "" in verdicts else verdicts[0]))
        bad = [p for p in paths if "/b/" in p]
        failing_cleanly = sum(1 for p in bad if "error" in ours[p])
        for path in paths:
            got = ours[path]
            corpus = next(k for k, v in CORPORA.items() if path.startswith(str(v)))
            name = f"{corpus}/{Path(path).name}"
            other = theirs[path]
            if "embedded" in got:
                payload = theirs[got["embedded"]]
                pillow_rows.append((name, "" if "error" not in payload else "payload unreadable"))
            elif "error" in other and "error" in got:
                pillow_rows.append((name, ""))
            elif "error" in other:
                pillow_rows.append((name, f"Pillow rejects ({other['error'][:50]}), we decode"))
            else:
                pillow_rows.append((name, compare(got, other)))
    print(f"\nBMP Suite bad files (b/): {len(bad)}, none trap: {len(bad) - failing_cleanly} decode, {failing_cleanly} fail cleanly")
    unexplained = 0
    for key, title, rows in (("suite", "BMP Suite references (g/, q/, x/)", suite_rows),
                             ("pillow", "Pillow 12, every corpus", pillow_rows)):
        known = DELIBERATE.get(key, {})
        matched = [r for r in rows if not r[1]]
        deliberate = [r for r in rows if r[1] and r[0] in known]
        failing = [r for r in rows if r[1] and r[0] not in known]
        unexplained += len(failing)
        print(f"\n{title}: {len(rows)} files, {len(matched)} match, {len(deliberate)} differ on purpose, {len(failing)} unexplained")
        for name, problem in failing:
            print(f"  DIFF {name}: {problem}")
        if options.list_failing:
            for name, problem in deliberate:
                print(f"  on purpose {name}: {known[name]} [{problem}]")
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
