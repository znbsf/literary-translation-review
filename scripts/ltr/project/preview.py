"""Static private preview: no server, network, localStorage, or model calls."""
import json
import secrets
from pathlib import Path
from .storage import atomic_write


def render(root: Path, project: dict) -> None:
    template = Path(__file__).with_name("preview.html").read_text(encoding="utf-8")
    payload = json.dumps(project, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = template.replace("__NONCE__", secrets.token_urlsafe(24)).replace("__PROJECT__", payload)
    atomic_write(root / "review.html", html)
