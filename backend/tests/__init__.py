"""pytest 测试包。

放一个 `__init__.py` 是为了让 `from tests.conftest import ...` 这种写法
在 `cd backend && pytest` 下稳定可解析（避免不同 pytest 版本对 rootdir/importmode
的处理差异导致 `ModuleNotFoundError: tests`）。
"""
