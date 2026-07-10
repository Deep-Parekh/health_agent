"""Deploy HealthVA to the Hugging Face Space via the HTTP upload API.

Uses the locally cached HF token (run `hf auth login` once, yourself — the
token never passes through chat or code output). Uploads the working tree,
excluding secrets and local-only files, as a single commit on the Space.

Usage:
    python scripts/deploy_space.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from huggingface_hub import HfApi

SPACE_ID = "DeepParekh/health_agent"
REPO_ROOT = Path(__file__).resolve().parents[1]

# Never uploaded: secrets, local state, local-only docs, tooling.
IGNORE = [
    ".env",
    ".env.*",
    "!.env.example",
    ".git/*",
    ".venv/*",
    "__pycache__/*",
    "*/__pycache__/*",
    "data/users.db",
    "logs/*",
    "docs/architecture.html",
    "uv.lock",
    ".python-version",
]

if __name__ == "__main__":
    api = HfApi()
    who = api.whoami()
    print(f"Authenticated as: {who['name']}")
    info = api.upload_folder(
        folder_path=str(REPO_ROOT),
        repo_id=SPACE_ID,
        repo_type="space",
        ignore_patterns=IGNORE,
        commit_message="Deploy HealthVA (synced from github.com/Deep-Parekh/health_agent)",
    )
    print(f"Uploaded: {info.commit_url}")
    print(f"Space: https://huggingface.co/spaces/{SPACE_ID}")
    sys.exit(0)
