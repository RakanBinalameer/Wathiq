"""Download a Whisper model (sherpa-onnx, int8) into models/whisper-<size>/.

    python scripts/get_whisper.py            # small (default, most accurate of the three)
    python scripts/get_whisper.py base       # smaller and faster, less accurate
    python scripts/get_whisper.py tiny

Source: github.com/k2-fsa/sherpa-onnx releases (asr-models). Keeps only the int8 files + tokens.
"""
import sys
import tarfile
import urllib.request
from pathlib import Path

URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-{size}.tar.bz2"  # tests may override


def main(size: str) -> None:
    if size not in ("tiny", "base", "small"):
        sys.exit("size must be tiny, base or small")
    dest = Path(__file__).resolve().parent.parent / "models" / f"whisper-{size}"
    dest.mkdir(parents=True, exist_ok=True)
    wanted = (f"{size}-encoder.int8.onnx", f"{size}-decoder.int8.onnx", f"{size}-tokens.txt")
    url = URL.format(size=size)
    print("downloading", url)
    # Streamed straight into tar: no temporary copy of the archive (it counted against small disk quotas).
    with urllib.request.urlopen(url) as resp, tarfile.open(fileobj=resp, mode="r|bz2") as tar:
        for m in tar:
            name = Path(m.name).name
            if m.isfile() and name in wanted:
                src = tar.extractfile(m)
                with open(dest / name, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                print("saved", dest / name)
    missing = [w for w in wanted if not (dest / w).is_file()]
    if missing:
        sys.exit(f"missing after extract: {missing}")
    print("done:", dest)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "small")
