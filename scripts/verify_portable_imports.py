"""Exercise real Office/OCR imports through the actual frozen HTTP application.

No source application modules are imported. Only the child process receives
explicit local component paths; no installation or global environment mutation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--office-fixtures", type=Path, required=True)
    parser.add_argument("--ocr-fixtures", type=Path, required=True)
    parser.add_argument("--ocr-baseline", type=Path, required=True)
    parser.add_argument("--libreoffice", type=Path, required=True)
    parser.add_argument("--tesseract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join((str(args.libreoffice.resolve()), str(args.tesseract.resolve()), environment.get("PATH", "")))
    environment["TESSDATA_PREFIX"] = str((args.tesseract / "tessdata").resolve())
    report = {"executable": str(args.executable.resolve()), "executable_sha256": hashlib.sha256(args.executable.read_bytes()).hexdigest(), "results": []}
    baseline_bytes = args.ocr_baseline.read_bytes()
    report["ocr_baseline_sha256"] = hashlib.sha256(baseline_bytes).hexdigest()
    baseline = {row["id"]: row for row in json.loads(baseline_bytes)["results"]}
    with tempfile.TemporaryDirectory(prefix="frozen-native-import-") as temporary:
        library = Path(temporary) / "library"
        log_path = args.output / "server.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen([str(args.executable.resolve()), "app", "--data-dir", str(library), "--no-browser", "--port", str(port)], cwd=temporary, env=environment, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            token = None

            def request(path, payload=None):
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
                try:
                    headers = {"Content-Type": "application/json"}
                    if token:
                        headers["X-Document-Review-Token"] = token
                    client.request("GET" if payload is None else "POST", path, None if payload is None else json.dumps(payload), headers)
                    response = client.getresponse()
                    data = response.read()
                    if response.status not in {200, 201}:
                        raise RuntimeError(f"Frozen HTTP {path} failed: {response.status}")
                    return data
                finally:
                    client.close()

            try:
                deadline = time.monotonic() + 40
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Frozen server exited before startup; inspect server.log")
                    try:
                        shell = request("/").decode("utf-8")
                        break
                    except (OSError, http.client.HTTPException):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Frozen HTTP startup timed out") from None
                        time.sleep(0.1)
                marker = re.search(r"\bconst\s+TOKEN\s*=\s*(\"[^\"]+\")", shell)
                if not marker:
                    raise RuntimeError("Frozen page has no local session token")
                token = json.loads(marker.group(1))
                state = json.loads(request("/api/state"))
                report["app_version"] = state["app_version"]
                for name in ("pypdf", "pypdfium2", "tesseract", "pdf-ocr", "libreoffice"):
                    row = next(row for row in state["dependencies"] if row["name"].casefold() == name)
                    assert row["available"], row
                samples = [(args.office_fixtures / ("source." + suffix), "chi_sim+chi_tra+eng", "legacy") for suffix in ("doc", "xls", "ppt")]
                manifest = json.loads((args.ocr_fixtures / "manifest.json").read_text(encoding="utf-8"))
                manifest_by_filename = {sample["pdf"]: sample for sample in manifest["fixtures"]}
                samples.extend((args.ocr_fixtures / sample["pdf"], sample["language"], "ocr") for sample in manifest["fixtures"])
                for path, language, kind in samples:
                    raw = path.read_bytes()
                    result = json.loads(request("/api/upload", {"filename": path.name, "content_base64": base64.b64encode(raw).decode("ascii"), "ocr_language": language}))
                    selected = result["selected"]
                    assert selected["extraction"]["available"], selected["state"]
                    assert not selected["state"]["read_only"], selected["state"]
                    assert selected["project"]["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
                    project = library / selected["directory"]
                    model = json.loads((project / "extraction/document.json").read_text(encoding="utf-8"))
                    text = "\n".join(b["text"] for b in model["blocks"])
                    assert text.strip(), path.name
                    metadata = model["metadata"]
                    if kind == "legacy":
                        receipt = metadata["legacy_conversion"]
                        assert receipt["converter"]["version_observed"]
                        for expected in ("Evidence supports the argument.", "证据支持这一论点。", "證據支持這一論點。", "Die Belege stützen diese Schlussfolgerung.", "Les preuves étayent cette conclusion.", "証拠はこの議論を支持する。", "Доказательства подтверждают этот вывод.", "Argumentum testimoniis confirmatur."):
                            assert expected in text, (path.name, expected)
                    else:
                        sample = manifest_by_filename[path.name]
                        assert sample["pdf_sha256"] == hashlib.sha256(raw).hexdigest()
                        expected_ocr = "\n".join(page["actual"] for page in baseline[sample["id"]]["pages"])
                        assert text == expected_ocr, "Frozen recognition differed from verified source-runtime baseline"
                        assert metadata["ocr"]["renderer"] == "pypdfium2"
                        assert metadata["ocr"]["render_dpi"] == 300
                        assert model["quality"]["requires_confirmation"]
                        receipt = metadata["ocr"]
                    report["results"].append({"file": path.name, "kind": kind, "language": language, "source_sha256": model["source"]["sha256"], "blocks": len(model["blocks"]), "text": text, "receipt": receipt})
                    print(f"Frozen {kind}: {path.name} imported", flush=True)
                report["passed"] = True
                (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                if token and process.poll() is None:
                    try:
                        request("/api/shutdown", {})
                    except (OSError, http.client.HTTPException, RuntimeError):
                        pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    print("Actual frozen Office/OCR imports passed")


if __name__ == "__main__":
    main()
