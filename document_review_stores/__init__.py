"""Composable stores backing DocumentReviewProject."""

from . import base as _base
from . import ingestion as _ingestion
from . import audits as _audits
from . import revision as _revision
from . import exports as _exports
from .ingestion import IngestionState
from .audits import AuditRunStore
from .revision import RevisionPlanBuilder
from .exports import ExportCenter
from types import CodeType

COMPONENT_TYPES = (IngestionState, AuditRunStore, RevisionPlanBuilder, ExportCenter)
COMPONENT_BY_METHOD = {
    name: component_type
    for component_type in COMPONENT_TYPES
    for name, value in component_type.__dict__.items()
    if callable(value) and not name.startswith("__")
}
_MODULES = (_base, _ingestion, _audits, _revision, _exports)

def _referenced_names(code):
    names = set(code.co_names)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names.update(_referenced_names(constant))
    return names


def _module_dependencies(module):
    names = set()
    for value in vars(module).values():
        functions = vars(value).values() if isinstance(value, type) else (value,)
        for function in functions:
            if isinstance(function, property):
                function = function.fget
            while getattr(function, "__code__", None) is not None:
                names.update(_referenced_names(function.__code__))
                function = getattr(function, "__wrapped__", None)
    return frozenset(name for name in names if not name.startswith("__"))


_DEPENDENCIES = {module: _module_dependencies(module) for module in _MODULES}
_COMPONENT_MODULES = {
    component: next(module for module in _MODULES if module.__name__ == component.__module__)
    for component in COMPONENT_TYPES
}
_BOUND_NAMES = {}


def bind_studio_globals(namespace, component_type=None):
    """Refresh only used dependencies, retaining legacy facade patch hooks.

    Never copy module metadata such as __name__, __spec__, or __file__. Only
    changed bindings are written, avoiding five full namespace copies for each
    delegated call in a nested view/export operation.
    """
    modules = _MODULES if component_type is None else (_base, _COMPONENT_MODULES[component_type])
    for module in modules:
        if module not in _BOUND_NAMES:
            _BOUND_NAMES[module] = _DEPENDENCIES[module].intersection(namespace)
        target = vars(module)
        for name in _BOUND_NAMES[module]:
            if target.get(name) is not namespace[name]:
                target[name] = namespace[name]

__all__ = [
    "IngestionState",
    "AuditRunStore",
    "RevisionPlanBuilder",
    "ExportCenter",
    "COMPONENT_TYPES",
    "COMPONENT_BY_METHOD",
    "bind_studio_globals",
]
