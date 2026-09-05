"""Composable responsibility stores for :class:`DocumentReviewProject`."""

from __future__ import annotations

from functools import wraps


def _serialized_mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _project_mutation_lock(self.root):
            return method(self, *args, **kwargs)

    return wrapped


class _ProjectComponent:
    def __init__(self, project):
        self._project = project

    @property
    def root(self):
        return self._project.root

    def __getattr__(self, name):
        return getattr(self._project, name)



__all__ = [name for name in globals() if not name.startswith("__")]
