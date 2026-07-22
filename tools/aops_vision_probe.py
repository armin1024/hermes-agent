"""Non-destructive OpenAI-compatible image-input probe for AOPS installs."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
from typing import Any


def _model_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        ).strip()
    return str(content or "").strip()


def _red_png_data_url() -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), (255, 0, 0)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def probe_vision(config: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
    model = str(model_cfg.get("model") or model_cfg.get("default") or "").strip()
    base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    supports_vision = model_cfg.get("supports_vision") is True
    result: dict[str, Any] = {
        "ok": False,
        "model": model,
        "baseUrl": base_url,
        "supportsVisionConfigured": supports_vision,
        "code": None,
        "error": None,
    }
    if not supports_vision:
        result.update(code="capability_disabled", error="model.supports_vision is not true")
        return result
    if not model or not base_url:
        result.update(code="missing_model_config", error="model.model/default and model.base_url are required")
        return result

    api_key = str(model_cfg.get("api_key") or "").strip()
    api_key_env = str(model_cfg.get("api_key_env") or "").strip()
    if not api_key and api_key_env:
        api_key = os.environ.get(api_key_env, "").strip()
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What color is this square? Reply with only the color."},
                {"type": "image_url", "image_url": {"url": _red_png_data_url()}},
            ],
        }],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        import httpx

        response = httpx.post(endpoint, headers=headers, json=request, timeout=timeout)
    except Exception as exc:
        result.update(code="request_failed", error=str(exc))
        return result
    result["httpStatus"] = response.status_code
    if response.status_code >= 400:
        result.update(code="gateway_rejected_image", error=response.text[:500])
        return result
    try:
        payload = response.json()
    except ValueError:
        result.update(code="invalid_gateway_response", error="response was not JSON")
        return result
    answer = _model_content(payload)
    result["answer"] = answer[:200]
    normalized = answer.casefold()
    if "red" not in normalized and "红" not in answer:
        result.update(
            code="vision_result_unverified",
            error="gateway accepted the image request but the model did not identify the red square",
        )
        return result
    result.update(ok=True, code="ok")
    return result


def main() -> int:
    try:
        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv()
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        config = load_config()
        result = probe_vision(config)
    except Exception as exc:
        result = {"ok": False, "code": "probe_failed", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
