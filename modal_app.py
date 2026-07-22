"""Modal deployment for the HealthVA API.

Serves api.py's FastAPI app as a Modal ASGI endpoint. Everything host-specific
lives here; api.py stays a plain FastAPI app that also runs under local uvicorn.

Deploy:   modal deploy modal_app.py
Dev:      modal serve modal_app.py        (temporary live URL)

Prereqs (owner, one-time):
  - `modal token new` on this machine (browser auth)
  - a Modal secret named "healthva-secrets" holding:
      OPENAI_API_KEY, DATABASE_URL, AGENT_API_SECRET
    (create it in the Modal dashboard so values never transit the CLI)
"""

from __future__ import annotations

from pathlib import Path

import modal

REPO = Path(__file__).resolve().parent

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements(REPO / "requirements.txt")
    # Ship the code + the read-only data DBs into the image.
    .add_local_dir(REPO / "healthva", remote_path="/root/healthva")
    .add_local_file(REPO / "api.py", remote_path="/root/api.py")
    .add_local_dir(REPO / "data", remote_path="/root/data")
)

app = modal.App("healthva")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("healthva-secrets")],
    min_containers=0,          # scale to zero — no cost when idle
    scaledown_window=300,      # keep warm 5 min after last request
    timeout=120,               # a multi-tool turn fits comfortably
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def fastapi_app():
    from api import app as fastapi

    return fastapi
