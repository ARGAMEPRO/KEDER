"""Weight loading: the operator drops yolov8x-fire.pt in later and it must just work."""
from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path

import pytest

from app import config
from app import services as sv


@pytest.fixture
def weights_env(tmp_path, monkeypatch):
    path = tmp_path / "yolov8x-fire.pt"
    monkeypatch.setenv("YOLO_WEIGHTS", str(path))
    monkeypatch.setenv("WEIGHTS_DIR", str(tmp_path))   # keep auto-detection hermetic
    monkeypatch.setenv("YOLO_WEIGHTS_URL", "")
    monkeypatch.setenv("YOLO_WEIGHTS_SHA256", "")
    monkeypatch.setattr(sv, "_model", None)
    monkeypatch.setattr(sv, "_model_path", None)
    return path


def test_missing_weights_gives_a_clear_error(weights_env):
    assert sv.model_ready() is False
    status = sv.model_status()
    assert status["weights_found"] is False
    with pytest.raises(RuntimeError) as exc:
        sv.get_model()
    assert "Веса модели не найдены" in str(exc.value)
    assert str(weights_env) in str(exc.value)


def test_weights_added_later_are_picked_up_without_restart(weights_env):
    """The whole point: no restart and no cache of the previous failure."""
    assert sv.model_ready() is False
    with pytest.raises(RuntimeError):
        sv.get_model()

    weights_env.write_bytes(b"not-a-real-checkpoint")
    assert sv.model_ready() is True
    assert sv.model_status()["weights_found"] is True

    # It now gets past the file check and stops on the missing vision library.
    with pytest.raises(RuntimeError) as exc:
        sv.get_model()
    message = str(exc.value)
    assert "Веса модели не найдены" not in message
    assert "ultralytics" in message


def test_model_loads_when_ultralytics_is_present(weights_env, monkeypatch):
    """With the vision stack available, a real file must produce a loaded model."""
    weights_env.write_bytes(b"fake-checkpoint")
    loaded = {}

    class FakeYOLO:
        def __init__(self, path):
            loaded["path"] = path

        def predict(self, frame, **kwargs):
            loaded["kwargs"] = kwargs
            return [_FakeResult()]

    class _Box:
        cls = 0
        conf = 0.91
        xyxy = [[1.0, 2.0, 3.0, 4.0]]

    class _FakeResult:
        names = {0: "fire"}
        boxes = [_Box()]

        def plot(self):
            return "annotated"

    fake = types.ModuleType("ultralytics")
    fake.YOLO = FakeYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", fake)
    monkeypatch.setattr(sv, "CV2_AVAILABLE", True)
    monkeypatch.setattr(sv, "cv2", types.SimpleNamespace(
        resize=lambda img, size, interpolation=None: img,
        INTER_AREA=1,
    ))

    import numpy as np

    model = sv.get_model()
    assert loaded["path"] == str(weights_env)

    conf, dets, ann = sv.detect(np.zeros((32, 32, 3), dtype="uint8"))
    assert conf == 0.91
    assert dets[0]["cls"] == "fire"
    assert ann == "annotated"
    assert model is not None
    # half precision must be forced off on CPU
    assert loaded["kwargs"]["half"] is False


def test_checksum_verification(weights_env, monkeypatch):
    payload = b"weights-payload"
    weights_env.write_bytes(payload)
    monkeypatch.setenv("YOLO_WEIGHTS_SHA256", hashlib.sha256(payload).hexdigest())
    fake = types.ModuleType("ultralytics")
    fake.YOLO = lambda path: object()
    monkeypatch.setitem(sys.modules, "ultralytics", fake)
    assert sv.get_model() is not None

    # A wrong checksum must refuse to load, not silently run a tampered model.
    monkeypatch.setattr(sv, "_model", None)
    monkeypatch.setattr(sv, "_model_path", None)
    monkeypatch.setenv("YOLO_WEIGHTS_SHA256", "0" * 64)
    with pytest.raises(RuntimeError) as exc:
        sv.get_model()
    assert "Контрольная сумма" in str(exc.value)


def test_ensure_weights_downloads_and_verifies(tmp_path, monkeypatch):
    from app import main as api

    target = tmp_path / "weights" / "yolov8x-fire.pt"
    payload = b"downloaded-weights"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("YOLO_WEIGHTS", str(target))
    monkeypatch.setenv("WEIGHTS_DIR", str(tmp_path / "weights"))
    monkeypatch.setenv("YOLO_WEIGHTS_URL", "https://example.invalid/w.pt")
    monkeypatch.setenv("YOLO_WEIGHTS_SHA256", digest)

    class FakeStream:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield payload

    monkeypatch.setattr(api.httpx, "stream", lambda *a, **k: FakeStream())
    api.ensure_weights()
    assert target.read_bytes() == payload
    assert not (tmp_path / "weights" / "yolov8x-fire.pt.part").exists()

    # A bad checksum must not install the file.
    target.unlink()
    monkeypatch.setenv("YOLO_WEIGHTS_SHA256", "f" * 64)
    with pytest.raises(RuntimeError):
        api.ensure_weights()
    assert not target.exists()


def test_device_autodetect(monkeypatch):
    monkeypatch.setenv("YOLO_DEVICE", "auto")
    monkeypatch.setitem(sys.modules, "torch", None)  # import torch -> TypeError/ImportError
    assert config.detect_device() == "cpu"

    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert config.detect_device() == "cuda:0"

    monkeypatch.setenv("YOLO_DEVICE", "cpu")
    assert config.detect_device() == "cpu"


def test_default_weights_path_is_cwd_independent(tmp_path, monkeypatch):
    # an empty weights dir, otherwise auto-detection finds the real checkpoint
    monkeypatch.setenv("WEIGHTS_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("YOLO_WEIGHTS", raising=False)
    path = config.weights_path()
    assert os.path.isabs(path)
    assert os.path.basename(path) == "yolov8x-fire.pt"
    assert Path(path).parent == Path(os.environ["WEIGHTS_DIR"])
