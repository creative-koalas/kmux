import importlib

kmux_app = importlib.import_module('kmux.app')

def test_app_exports():
    assert hasattr(kmux_app, 'mcp')
    assert hasattr(kmux_app, 'set_root_password')
