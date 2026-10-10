"""Start an empty, isolated library for real browser regression tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unified_app import serve_unified_app

fixture = None
project_dir = None
if "--argument-composition" in sys.argv[2:]:
    from test.test_argument_composition import ArgumentCompositionTests
    fixture = ArgumentCompositionTests()
    fixture.setUp()
    request = fixture.request()
    payload = fixture.payload(request)
    payload["argument_assessment"]["dimensions"][3]["assessment"] = '<img src=x onerror="window.__compositionInjected=true"> Straße 日本語 Русский Latīna'
    import copy
    extra = copy.deepcopy(payload["argument_assessment"]["dimensions"][0])
    extra.update(id="problem-framing", title="Problem framing", why_relevant="This synthetic article compares two interpretations.")
    payload["argument_assessment"]["dimensions"].append(extra)
    fixture.collect(request, payload)
    project_dir = fixture.project.root
if "--close-reading" in sys.argv[2:]:
    from test.test_close_reading_quality import CloseReadingQualityTests
    fixture = CloseReadingQualityTests()
    fixture.setUp()
    request = fixture.helper.request()
    finding = fixture.helper.finding()
    detail = fixture.detail()
    detail["strongest_defense"] = '<img src=x onerror="window.__injected=true"> Straße 日本語 Русский Latīna'
    finding["check_data"] = {"close_reading": detail}
    fixture.helper.collect(request, fixture.helper.response(request, finding))
    project_dir = fixture.project.root
server, url = serve_unified_app(data_dir=Path(sys.argv[1]), project_dir=project_dir, open_browser=False)
print(url, flush=True)
try:
    server.serve_forever(poll_interval=0.1)
finally:
    server.server_close()
    if fixture is not None:
        fixture.doCleanups()
