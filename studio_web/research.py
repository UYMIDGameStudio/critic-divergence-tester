"""Research and compatibility shells assembled from packaged static assets."""
from importlib.resources import files
import json


def research_shell(kind: str) -> str:
    if kind not in {"product", "professional"}:
        raise ValueError("unknown research shell")
    resources = files("studio_web")
    prefix = "research_" + kind
    template = resources.joinpath(prefix + ".html").read_text(encoding="utf-8")
    styles = resources.joinpath(prefix + ".css").read_text(encoding="utf-8")
    messages = resources.joinpath("locales-research.json").read_text(encoding="utf-8")
    messages = json.dumps(json.loads(messages), ensure_ascii=False).replace("<", "\\u003c")
    scripts = resources.joinpath("research_i18n.js").read_text(encoding="utf-8")
    scripts = scripts.replace("__RESEARCH_MESSAGES__", messages)
    scripts += "\n" + resources.joinpath(prefix + ".js").read_text(encoding="utf-8")
    return template.replace("__STYLES__", styles).replace("__SCRIPTS__", scripts)
