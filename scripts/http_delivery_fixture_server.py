"""Owned empty/synthetic projects for repeated HTTP shell delivery checks."""
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from argument_app import serve_product_app
from argument_ui import serve_workbench
from test.test_argument_ui import ArgumentUITests
from unified_app import serve_unified_app

root = Path(sys.argv[1])
studio, studio_url = serve_unified_app(data_dir=root / 'studio', open_browser=False)
product, product_url = serve_product_app(data_dir=root / 'product', open_browser=False)
professional_project = ArgumentUITests().make_project(root / 'professional')
professional, professional_url = serve_workbench(professional_project, open_browser=False)
for server in (studio, product, professional):
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
print(json.dumps({'studio': studio_url, 'research': studio_url + 'research/',
                  'product': product_url, 'professional': professional_url}), flush=True)
threading.Event().wait()
