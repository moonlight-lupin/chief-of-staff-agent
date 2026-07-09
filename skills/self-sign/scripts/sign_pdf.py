#!/usr/bin/env python3
"""Apply confirmed self-signature placements to a PDF with hash-verified inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fitz  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"PyMuPDF is required for sign_pdf.py: {exc}", file=sys.stderr)
    raise SystemExit(2)

try:
    import yaml  # type: ignore
except Exception as exc:  # pragma: no cover
    print(f"PyYAML is required for sign_pdf.py: {exc}", file=sys.stderr)
    raise SystemExit(2)

TOOL_VERSION = "0.1.2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        return {}
    loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def config_auto_date(config: dict[str, Any]) -> bool:
    self_sign = config.get("self_sign", {}) if isinstance(config, dict) else {}
    return bool(isinstance(self_sign, dict) and self_sign.get("auto_date", False))


def parse_locations(value: str) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--locations must be a JSON array: {exc}") from exc
    if not isinstance(loaded, list):
        raise ValueError("--locations must be a JSON array")
    result: list[dict[str, Any]] = []
    for idx, loc in enumerate(loaded, start=1):
        if not isinstance(loc, dict):
            raise ValueError(f"location #{idx} must be an object")
        if "coordinates" in loc and not all(k in loc for k in ("x", "y", "w", "h")):
            coords = loc.get("coordinates")
            if isinstance(coords, list) and len(coords) == 4:
                x0, y0, x1, y1 = [float(v) for v in coords]
                loc = dict(loc)
                loc.update({"x": x0, "y": y0, "w": max(1.0, x1 - x0), "h": max(1.0, y1 - y0)})
        missing = [key for key in ("page", "x", "y", "w", "h") if key not in loc]
        if missing:
            raise ValueError(f"location #{idx} missing required key(s): {', '.join(missing)}")
        result.append(dict(loc))
    return result


def run_detector(input_pdf: Path, config_path: str | None) -> list[dict[str, Any]]:
    detector = Path(__file__).with_name("sign_detector.py")
    cmd = [sys.executable, str(detector), str(input_pdf), "--doc-type", "pdf", "--format", "json"]
    if config_path:
        cmd.extend(["--config", config_path])
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"sign_detector.py failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return parse_locations(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"sign_detector.py returned invalid locations: {exc}") from exc


def confirm_locations(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not locations:
        print("No signature/date locations detected.", file=sys.stderr)
        return []
    print("Detected locations:", file=sys.stderr)
    for idx, loc in enumerate(locations, start=1):
        loc_id = loc.get("id") or f"loc-{idx}"
        kind = loc.get("location_type", "signature")
        party = loc.get("matched_party", "unknown")
        conf = loc.get("confidence", "?")
        print(
            f"  {idx}. {loc_id}: page {loc.get('page')} x={loc.get('x')} y={loc.get('y')} "
            f"w={loc.get('w')} h={loc.get('h')} type={kind} party={party} confidence={conf}",
            file=sys.stderr,
        )
    answer = input("Sign/fill all listed locations? Type YES to proceed: ").strip()
    if answer != "YES":
        return []
    return locations


def signature_rect(loc: dict[str, Any], sig_width: float, sig_height: float) -> fitz.Rect:
    x = float(loc["x"])
    y = float(loc["y"])
    max_w = float(loc["w"])
    max_h = float(loc["h"])
    if sig_width <= 0 or sig_height <= 0:
        return fitz.Rect(x, y, x + max_w, y + max_h)
    scale = max_w / sig_width
    height = sig_height * scale
    width = max_w
    if height > max_h:
        scale = max_h / sig_height
        height = max_h
        width = sig_width * scale
    y_offset = max(0.0, (max_h - height) / 2.0)
    return fitz.Rect(x, y + y_offset, x + width, y + y_offset + height)


def apply_locations(input_pdf: Path, output_pdf: Path, signature: Path, locations: list[dict[str, Any]], auto_date: bool) -> None:
    if not locations:
        raise RuntimeError("No confirmed locations to sign")
    sig_pix = fitz.Pixmap(str(signature))
    sig_width = float(sig_pix.width)
    sig_height = float(sig_pix.height)
    sig_pix = None  # release native resources
    doc = fitz.open(str(input_pdf))
    try:
        for loc in locations:
            page_num = int(loc["page"])
            if page_num < 1 or page_num > len(doc):
                raise RuntimeError(f"Location {loc.get('id', '')} references missing page {page_num}")
            page = doc[page_num - 1]
            rect = fitz.Rect(float(loc["x"]), float(loc["y"]), float(loc["x"]) + float(loc["w"]), float(loc["y"]) + float(loc["h"]))
            loc_type = str(loc.get("location_type") or loc.get("type") or "signature").lower()
            if loc_type == "date" and auto_date:
                page.insert_textbox(rect, date.today().isoformat(), fontsize=10, align=0)
            else:
                page.insert_image(signature_rect(loc, sig_width, sig_height), filename=str(signature), keep_proportion=True, overlay=True)
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_pdf), garbage=4, deflate=True)
    finally:
        doc.close()


def write_manifest(output_pdf: Path, source_file: Path, source_hash: str, signature: Path, locations: list[dict[str, Any]]) -> Path:
    output_hash = sha256_file(output_pdf)
    signature_hash = sha256_file(signature)
    manifest = {
        "source_file": str(source_file),
        "source_hash": source_hash,
        "output_file": str(output_pdf),
        "output_hash": output_hash,
        "signature_image": str(signature),
        "signature_hash": signature_hash,
        "locations_signed": locations,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_version": TOOL_VERSION,
    }
    manifest_path = Path(str(output_pdf) + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sign a PDF at confirmed locations with source hash verification")
    parser.add_argument("--input", required=True, help="Input PDF path")
    parser.add_argument("--output", required=True, help="Output signed PDF path")
    parser.add_argument("--signature", required=True, help="Signature PNG path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--locations", help="JSON array of confirmed locations")
    group.add_argument("--interactive", action="store_true", help="Run sign_detector.py and ask for confirmation")
    parser.add_argument("--source-hash", help="Expected SHA256 of input PDF from detection time")
    parser.add_argument("--config", help="company.yaml for self_sign.auto_date and detector aliases")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        input_pdf = Path(args.input).expanduser().resolve()
        output_pdf = Path(args.output).expanduser().resolve()
        signature = Path(args.signature).expanduser().resolve()
        for path, label in [(input_pdf, "input PDF"), (signature, "signature image")]:
            if not path.exists():
                raise FileNotFoundError(f"{label} not found: {path}")
        source_hash = sha256_file(input_pdf)
        if args.source_hash and source_hash.lower() != args.source_hash.lower():
            print(
                f"Refusing to sign: source hash mismatch (expected {args.source_hash}, got {source_hash}). "
                "The file changed after detection.",
                file=sys.stderr,
            )
            return 1
        config = load_config(args.config)
        if args.locations:
            locations = parse_locations(args.locations)
        else:
            detection_hash = source_hash
            detected = run_detector(input_pdf, args.config)
            locations = confirm_locations(detected)
            if not locations:
                print("Refusing to sign: no locations confirmed.", file=sys.stderr)
                return 1
            # Hard rule: refuse if the file changed between detection and signing.
            source_hash_after_detection = sha256_file(input_pdf)
            if source_hash_after_detection != detection_hash:
                print("Refusing to sign: source PDF changed after detection.", file=sys.stderr)
                return 1
            source_hash = source_hash_after_detection
        # Final verification immediately before mutation.
        if sha256_file(input_pdf) != source_hash:
            print("Refusing to sign: source PDF changed before signing.", file=sys.stderr)
            return 1
        apply_locations(input_pdf, output_pdf, signature, locations, auto_date=config_auto_date(config))
        manifest_path = write_manifest(output_pdf, input_pdf, source_hash, signature, locations)
        print(json.dumps({"output": str(output_pdf), "manifest": str(manifest_path), "source_hash": source_hash}, indent=2))
        return 0
    except Exception as exc:
        print(f"sign_pdf.py error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
