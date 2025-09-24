import importlib

def test_pkg_imports():
    m = importlib.import_module('kmux')
    assert hasattr(m, '__version__')
