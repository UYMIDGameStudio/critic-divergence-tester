"""Generate real, owned report artifacts for the CommonMark browser regression."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test.test_report_literal_text import export_fixture

if __name__ == '__main__':
    print(json.dumps(export_fixture(sys.argv[1]), ensure_ascii=False))
