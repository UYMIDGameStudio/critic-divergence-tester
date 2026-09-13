"""Isolated real API endpoints for research UI browser regressions."""
import base64
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argument_app import serve_product_app
from argument_ui import serve_workbench
from argument_revision import import_review_report
from test.test_argument_ui import ArgumentUITests
from test.test_research_ui import SAMPLES, language_project

root = Path(sys.argv[1])
servers = []
product, product_url = serve_product_app(data_dir=root / "library", open_browser=False)
servers.append(product)
resume_project, _ = language_project(root / "resume", "繁體中文論證需要證據。", "big5")
import_review_report(resume_project.root, "Review report retained before interrupted prompt preparation.")
resume, resume_url = serve_product_app(data_dir=root / "resume-library",
                                     project_dir=resume_project.root, open_browser=False)
servers.append(resume)
professional = ArgumentUITests().make_project(root / "professional")
server, professional_url = serve_workbench(professional, open_browser=False)
servers.append(server)
samples = []
for index, (text, encoding) in enumerate(SAMPLES):
    project, source = language_project(root / f"sample-{index}", text, encoding)
    server, url = serve_workbench(project, open_browser=False)
    servers.append(server)
    samples.append({"url": url, "text": text, "encoding": encoding,
                    "base64": base64.b64encode(source.read_bytes()).decode("ascii")})
for server in servers:
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
print(json.dumps({"product": product_url, "professional": professional_url,
                  "resume": resume_url, "samples": samples}), flush=True)
threading.Event().wait()
