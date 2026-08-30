"""Cross-edge bearing result aggregation service.

Public package exports are resolved lazily so importing one compatibility
submodule does not initialize the repository and storage adapters.
"""

from importlib import import_module

__all__ = [
    "SummaryRepository",
    "SummaryService",
    "build_arbitration_request",
    "build_window_result",
]


def __getattr__(name: str) -> object:
    modules = {
        "SummaryRepository": ".repository",
        "SummaryService": ".service",
        "build_arbitration_request": ".aggregation",
        "build_window_result": ".aggregation",
    }
    module_name = modules.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
