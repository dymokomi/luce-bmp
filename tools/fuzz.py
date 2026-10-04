#!/usr/bin/env python3
"""Mutate bitmaps and decode them: the decoder must never trap, hang or grow without
bound, whatever the bytes.

Each case is one of the corpus files (see conformance.py) changed by a few random
mutations: bytes flipped, inserted, deleted or repeated, the file cut short, chunks
spliced in from another file, and BMP's own values (header sizes, bit depths,
compressions, masks, RLE escapes, 32-bit fields set to 0 or 0xFFFFFFFF) dropped in at
random places. A fifth of the cases lose their file header and are read as bare DIBs,
half of those as icon DIBs with an AND mask. Cases run in batches through build/bmpcheck
(tools/bmpcheck.lucb, with `qs`: decoded, nothing written, a 4-megapixel limit), each
batch under a time limit; a batch that dies or times out is narrowed to the case
responsible, which is saved under build/fuzz-failures. The peak memory of the decoding
processes is reported.

First, a stress phase decodes generated bitmaps that are large or adversarial: 2048 x
2048 pixels of 32-bit bitfields, RLE8 data of single-pixel runs and of deltas, a stream
of row ends, a header claiming 65535 x 65535 pixels, and an icon DIB with its mask. Each
must finish in under two seconds.

  python3 tools/fuzz.py [--cases 20000] [--seed 1] [--guard-malloc]
"""
import argparse, os, random, resource, shutil, struct, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from conformance import CORPORA  # noqa: E402

TOOL = ROOT / "build/bmpcheck"
FAILURES = ROOT / "build/fuzz-failures"
# With --guard-malloc, the tool runs under macOS's Guard Malloc (every allocation on its
# own page, freed pages unmapped), which turns an overrun or a use after free into a crash.
ENVIRONMENT = dict(os.environ)
TOKENS = [b"BM", b"\x0c\x00\x00\x00", b"\x28\x00\x00\x00", b"\x40\x00\x00\x00", b"\x6c\x00\x00\x00",
          b"\x7c\x00\x00\x00", b"\x01\x00", b"\x02\x00", b"\x04\x00", b"\x08\x00", b"\x10\x00", b"\x18\x00",
          b"\x20\x00", b"\x01\x00\x00\x00", b"\x02\x00\x00\x00", b"\x03\x00\x00\x00", b"\x04\x00\x00\x00",
          b"\x06\x00\x00\x00", b"\xff\xff\xff\xff", b"\x00\x00\x00\x00", b"\x00\x00\x00\xff", b"\xff\xff\x00\x00",
          b"\x00\x00", b"\x00\x01", b"\x00\x02\xff\xff", b"\x00\xff", b"\xff\x00", b"\x00" * 64, b"\xff" * 64]


def bmp(width, height, bits, pixels, compression=0, palette=b"", masks=b"", header=40):
    """A BMP file with a BITMAPINFOHEADER (or V5) of the given fields."""
    info = struct.pack("<IiiHHIIiiII", header, width, height, 1, bits, compression, 0, 2835, 2835, 0, 0)
    info += masks + b"\x00" * (header - len(info) - len(masks))
    offset = 14 + len(info) + len(palette)
    return b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset) + info + palette + pixels


def stress_files():
    """Large and adversarial bitmaps: (name, bytes)."""
    masks = struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    yield "bitfields 2048x2048", bmp(2048, 2048, 32, os.urandom(2048 * 2048 * 4), 3, masks=masks, header=124)
    palette = bytes(range(256)) * 4
    runs = b"".join(b"\x01" + bytes([x % 256]) for x in range(2048)) + b"\x00\x00"
    yield "RLE8 single runs", bmp(2048, 2048, 8, runs * 2048 + b"\x00\x01", 1, palette=palette)
    yield "RLE8 deltas", bmp(2048, 2048, 8, b"\x00\x02\xff\x00" * 400000 + b"\x00\x02\x00\x01" * 400000, 1, palette=palette)
    yield "row ends", bmp(16, 2048, 8, b"\x00\x00" * 1000000, 1, palette=palette)
    yield "65535 x 65535", bmp(65535, 65535, 24, b"\x00" * 1000)
    yield "icon", bmp(2048, 4096, 32, b"\x00" * (2048 * 2048 * 4) + b"\x55" * (2048 * 2048 // 8))[14:], "qsi"


def stress():
    """Decode the stress files; answer the failures."""
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        for name, data, *flags in stress_files():
            path = Path(directory) / "stress.bmp"
            path.write_bytes(data)
            started = time.monotonic()
            done, crashed, trailer = run([(flags[0] if flags else "qs", str(path))], timeout=30)
            spent = time.monotonic() - started
            verdict = "trap" if crashed else ("slow" if spent > 2 else "ok")
            print(f"stress {name:<20} {len(data) / 1e6:6.2f} MB {spent:6.2f} s  {verdict}", flush=True)
            if verdict != "ok":
                failures.append(f"stress {name}: {verdict} {trailer.strip()[-200:]}")
    return failures


def mutate(data, rng, donors):
    data = bytearray(data)
    for _ in range(rng.randint(1, 6)):
        choice = rng.random()
        at = rng.randint(0, len(data))
        if choice < 0.2 and data:
            data[min(at, len(data) - 1)] ^= 1 << rng.randint(0, 7)
        elif choice < 0.35:
            data[at:at] = rng.choice(TOKENS)
        elif choice < 0.45 and len(data) > 4:
            token = rng.choice(TOKENS)
            spot = rng.randint(0, min(len(data), 140) - 1)
            data[spot:spot + len(token)] = token
        elif choice < 0.55 and data:
            del data[at:at + rng.randint(1, 16)]
        elif choice < 0.62 and data:
            del data[at:]
        elif choice < 0.72 and data:
            end = min(len(data), at + rng.randint(1, 64))
            data[at:at] = data[at:end] * rng.randint(1, 8)
        elif choice < 0.85:
            donor = rng.choice(donors)
            start = rng.randint(0, max(0, len(donor) - 1))
            data[at:at] = donor[start:start + rng.randint(1, 200)]
        elif data:
            data[min(at, len(data) - 1)] = rng.choice([0, 1, 2, 3, 4, 5, 6, 8, 12, 16, 24, 32, 40, 0x80, 0xff])
    return bytes(data)


def run(jobs, timeout):
    """Run `jobs` [(flags, path)] in one tool process: (finished count, crashed?, stderr)."""
    with tempfile.TemporaryDirectory() as directory:
        listing = Path(directory) / "list.txt"
        listing.write_text("".join(f"{flags} {path}\n" for flags, path in jobs))
        try:
            result = subprocess.run([str(TOOL), str(listing), directory], capture_output=True, timeout=timeout, env=ENVIRONMENT)
        except subprocess.TimeoutExpired as expired:
            return len((expired.stdout or b"").splitlines()), True, "timeout"
        return len(result.stdout.splitlines()), result.returncode != 0, result.stderr.decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--no-stress", action="store_true")
    parser.add_argument("--guard-malloc", action="store_true")
    options = parser.parse_args()
    if options.guard_malloc:
        ENVIRONMENT["DYLD_INSERT_LIBRARIES"] = "/usr/lib/libgmalloc.dylib"
    stressed = [] if options.no_stress else stress()
    rng = random.Random(options.seed)
    seeds = sorted(p for corpus in CORPORA.values() for p in corpus.rglob("*.bmp") if p.stat().st_size < 400000)
    donors = [p.read_bytes() for p in seeds]
    failures = []
    done, crashed, trailer = run([("qs", str(seed)) for seed in seeds], timeout=600)
    print(f"corpus: {len(seeds)} files, {'crashed: ' + trailer.strip()[-300:] if crashed else 'no traps'}", flush=True)
    if crashed:
        failures.append(f"corpus file {seeds[done]}: {trailer.strip()[-300:]}")
    with tempfile.TemporaryDirectory() as work:
        made = 0
        while made < options.cases:
            count = min(options.batch, options.cases - made)
            jobs = []
            for index in range(count):
                path = Path(work) / f"{made + index}.bmp"
                data, flags = mutate(rng.choice(donors), rng, donors), "qs"
                if rng.random() < 0.2:
                    data, flags = data[14:], rng.choice(["qsd", "qsi"])
                path.write_bytes(data)
                jobs.append((flags, str(path)))
            start = 0
            while start < len(jobs):
                done, crashed, trailer = run(jobs[start:], timeout=120)
                if not crashed:
                    break
                bad = start + done
                FAILURES.mkdir(parents=True, exist_ok=True)
                saved = FAILURES / f"case-{made + bad}-{jobs[bad][0]}.bmp"
                shutil.copy(jobs[bad][1], saved)
                failures.append(f"{saved}: {trailer.strip()[-300:]}")
                print(f"FAIL {failures[-1]}", flush=True)
                start = bad + 1
            made += count
            for path in Path(work).iterdir():
                path.unlink()
            print(f"{made} cases, {len(failures)} failing", flush=True)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    print(f"{options.cases} cases: {len(failures)} traps or hangs; peak memory of a decoding process {peak_mb:.1f} MB")
    for line in stressed:
        print(f"FAIL {line}")
    return 1 if failures or stressed else 0


if __name__ == "__main__":
    sys.exit(main())
