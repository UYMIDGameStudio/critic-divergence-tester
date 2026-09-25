"""Browser assets loaded identically from source, wheels and portable bundles."""

from importlib.resources import files
import json

SCRIPTS = ("i18n.js", "state.js", "api.js", "drafts.js", "imports.js", "adversarial.js", "views.js", "events.js", "editing.js", "navigation.js", "app.js")


def shell_template() -> str:
    resources = files(__package__)
    template = resources.joinpath("shell.html").read_text(encoding="utf-8")
    styles = resources.joinpath("styles.css").read_text(encoding="utf-8")
    scripts = "\n\n".join(resources.joinpath(name).read_text(encoding="utf-8") for name in SCRIPTS)
    messages = {}
    for name in ("locales-part-a.json", "locales-part-b.json", "locales-system.json", "locales-adversarial.json"):
        resource = resources.joinpath(name)
        messages.update(json.loads(resource.read_text(encoding="utf-8")))
    scripts = scripts.replace("__UI_MESSAGES__", json.dumps(messages, ensure_ascii=False).replace("<", "\\u003c"))
    return template.replace("__STYLES__", styles).replace("__SCRIPTS__", scripts)
