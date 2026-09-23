"""Local-first research contracts; imports are lazy for diagnostic bootstrapping."""

_EXPORTS = {
    'Workspace': ('governance.paths', 'Workspace'),
    'BoundaryError': ('governance.paths', 'BoundaryError'),
    'Catalog': ('research.catalog', 'Catalog'),
    'Question': ('research.catalog', 'Question'),
    'Source': ('research.catalog', 'Source'),
    'Evidence': ('research.catalog', 'Evidence'),
    'ResearchContracts': ('research.contracts', 'ResearchContracts'),
}

def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    from importlib import import_module
    module, attribute = _EXPORTS[name]
    return getattr(import_module('.' + module, __name__), attribute)

__version__ = "0.1.0a0"
__all__ = ["Workspace", "BoundaryError", "Catalog", "Question", "Source", "Evidence", "ResearchContracts"]
