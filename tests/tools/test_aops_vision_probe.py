from types import SimpleNamespace

from tools.aops_vision_probe import probe_vision


def _config(**model_overrides):
    model = {
        "model": "qwen35-122b",
        "base_url": "http://model.internal/v1",
        "supports_vision": True,
    }
    model.update(model_overrides)
    return {"model": model}


def test_probe_vision_sends_openai_image_content(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"choices": [{"message": {"content": "RED"}}]},
        )

    monkeypatch.setattr("httpx.post", fake_post)
    result = probe_vision(_config())

    assert result["ok"] is True
    assert captured["url"] == "http://model.internal/v1/chat/completions"
    image_part = captured["json"]["messages"][0]["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_probe_vision_reports_gateway_rejection(monkeypatch):
    monkeypatch.setattr(
        "httpx.post",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=400,
            text="image content is unsupported",
        ),
    )
    result = probe_vision(_config())
    assert result["ok"] is False
    assert result["code"] == "gateway_rejected_image"


def test_probe_vision_requires_capability_flag():
    result = probe_vision(_config(supports_vision=False))
    assert result["code"] == "capability_disabled"
