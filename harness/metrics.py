"""Inclusive phase timings; nested phases are intentionally not additive."""
from functools import wraps
import time


def timed_phase(name):
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            started = time.monotonic()
            try:
                return method(self, *args, **kwargs)
            finally:
                graph = getattr(self, "graph", None)
                if graph is None:
                    graph = self.state.graph
                graph.timings[name] = graph.timings.get(name, 0.0) + time.monotonic() - started
        return wrapped
    return decorate
