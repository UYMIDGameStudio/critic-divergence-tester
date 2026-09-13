"""Deterministic local capacity smoke test; no model quality claims."""
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_review_studio import DocumentReviewProject
from document_review_model import ReviewContext
from project_lifecycle import create_backup, restore_backup


def main():
    results = {"paragraphs": 1000, "timings_seconds": {}}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        def timed(name, call):
            start = time.perf_counter()
            value = call()
            results["timings_seconds"][name] = round(time.perf_counter() - start, 3)
            return value
        source = ("# Capacity fixture\n\n" + "\n\n".join(f"Paragraph {i}: material for a deterministic capacity check." for i in range(1000))).encode()
        project = timed("import", lambda: DocumentReviewProject.create(root / "library", filename="capacity.md", content=source))
        project.confirm_extraction("confirm")
        project.confirm_context(ReviewContext(document_type="document", jurisdiction="unknown", effective_date="unknown", publisher_type="author", audience="editors").to_dict())
        timed("precheck", lambda: project.run_local_prechecks(["expression_ambiguity"]))
        view = timed("view", project.view)
        assert view["extraction"]["total_blocks"] == 1001
        archive = timed("backup", lambda: create_backup(project.root, root / "backup.zip"))
        restored = timed("restore", lambda: restore_backup(archive, root / "restored"))
        assert not DocumentReviewProject(restored).integrity_errors()
        results["bytes"] = len(source)
        results["verified"] = True
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("dist/capacity-report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results))


if __name__ == "__main__":
    main()
