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


def respond(payload: dict):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:

    try:
        import easyocr

        reader = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
        respond({"ready": True})
    except Exception as exc:
        respond({"ready": False, "error": str(exc)})
        return 1

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        prepared_path = None

        try:
            payload = json.loads(raw_line)
            source_path = payload["path"]
            prepared_path = prepare_image(source_path)
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
            respond({"ok": True, "lines": lines})
        except Exception as exc:
            respond({"ok": False, "error": str(exc)})
        finally:
            if prepared_path and os.path.exists(prepared_path):
                try:
                    os.remove(prepared_path)
                except OSError:
                    pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
