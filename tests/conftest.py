import pytest


@pytest.fixture(autouse=True)
def _private_data_dir(tmp_path, monkeypatch):
    """Keep progress files from tests out of the real app-data folder."""
    monkeypatch.setenv("TEAL_RPA_DATA_DIR", str(tmp_path / "app-data"))
