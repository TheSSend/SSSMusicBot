import json
import os
import sys
import tempfile

from PIL import Image, ImageFilter, ImageOps


OCR_MAX_SIDE = 1600


def prepare_image(source_path: str) -> str:

    with Image.open(source_path) as image:
        prepared = image.convert("RGB")
        prepared.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE), Image.Resampling.LANCZOS)
        prepared = ImageOps.autocontrast(prepared)
        prepared = prepared.filter(ImageFilter.SHARPEN)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            prepared.save(tmp.name, format="PNG", optimize=True)
            return tmp.name


def main() -> int:

    if len(sys.argv) != 2:
        sys.stderr.write("usage: ocr_worker.py <image-path>\n")
        return 2

    source_path = sys.argv[1]
    prepared_path = None

    try:
        import easyocr

        prepared_path = prepare_image(source_path)
        reader = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
        lines = reader.readtext(
            prepared_path,
            detail=0,
            paragraph=False,
            decoder="greedy",
            beamWidth=1,
            batch_size=1,
            workers=0,
            canvas_size=OCR_MAX_SIDE,
            mag_ratio=1.0,
            text_threshold=0.7,
            low_text=0.4,
            link_threshold=0.4,
        )
        sys.stdout.write(json.dumps({"lines": lines}, ensure_ascii=False))
        return 0
    except Exception as exc:
        sys.stderr.write(str(exc))
        return 1
    finally:
        if prepared_path and os.path.exists(prepared_path):
            try:
                os.remove(prepared_path)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
