"""Stub the HA integration dependencies that aren't installed in the venv.

``homeassistant.components.bluetooth`` imports the ``usb`` integration, which
needs ``aiousbwatcher`` and ``serialx`` — neither is a dependency of this
integration. Only the bluetooth module's namespace is needed here (the tests
monkeypatch ``async_discovered_service_info``), so dummy packages are enough.

Import this module before anything that reaches ``homeassistant.components``.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types

STUB_ROOTS = ("aiousbwatcher", "serialx", "usb")


class _Anything:
    """Accepts anything and returns itself — stands in for any stubbed symbol."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, item):
        return _Anything()

    def __call__(self, *args, **kwargs):
        return _Anything()


class _StubModule(types.ModuleType):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return _Anything


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Fabricate any module under STUB_ROOTS, including submodules."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in STUB_ROOTS:
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        module = _StubModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        return None


if not any(isinstance(finder, _StubFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _StubFinder())
