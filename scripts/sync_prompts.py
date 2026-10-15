"""
Keep the saved copies in prompts/ and the prompts in Langfuse in sync.
(The app itself never reads prompts/ - it always loads prompts from Langfuse.)

Run from the project root:

    python scripts/sync_prompts.py pull    # Langfuse -> prompts/   (after you edited prompts in Langfuse)
    python scripts/sync_prompts.py push    # prompts/ -> Langfuse   (first setup, or after editing a file)

`push` creates a new version in Langfuse only when the text changed, and gives it the
"production" label, so the app starts using it within a minute.
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402  (reads .env)
from app.prompts import PROMPT_NAMES  # noqa: E402

PROMPTS_DIR = ROOT / "prompts"
LABEL = settings.langfuse_prompt_label
HEADER = """# Saved copy of the Langfuse prompt "{name}". The app does NOT read this file:
# it always loads the prompt from Langfuse. Refresh this copy with: python scripts/sync_prompts.py pull
"""


def api(method: str, path: str, body: dict | None = None) -> dict | None:
    token = base64.b64encode(f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()).decode()
    request = urllib.request.Request(
        settings.langfuse_host.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"Langfuse API error {e.code} on {method} {path}: {e.read().decode()[:300]}")


def get_remote(name: str) -> dict | None:
    return api("GET", f"/api/public/v2/prompts/{quote(name)}?label={quote(LABEL)}")


def messages_of(prompt: dict) -> list[dict]:
    body = prompt["prompt"]
    if prompt.get("type") != "chat":
        return [{"role": "user", "content": body}]
    return [{"role": m["role"], "content": m["content"]} for m in body]


def push() -> None:
    files = sorted(PROMPTS_DIR.glob("*.yaml"))
    if not files:
        raise SystemExit(f"No prompt files in {PROMPTS_DIR}")
    for file in files:
        local = yaml.safe_load(file.read_text(encoding="utf-8"))
        name, messages = local["name"], local["messages"]
        remote = get_remote(name)
        if remote and messages_of(remote) == messages:
            print(f"  = {name}: unchanged (v{remote['version']})")
            continue
        created = api("POST", "/api/public/v2/prompts", {
            "name": name,
            "type": "chat",
            "prompt": messages,
            "labels": [LABEL],
            "commitMessage": f"Uploaded from prompts/{file.name}",
        })
        print(f"  + {name}: uploaded as v{created['version']} (label '{LABEL}')")
    missing = set(PROMPT_NAMES) - {yaml.safe_load(f.read_text(encoding='utf-8'))["name"] for f in files}
    if missing:
        print(f"  ! no saved copy for: {', '.join(sorted(missing))}")


class _ReadableDumper(yaml.SafeDumper):
    """Write multi-line text as readable `|` blocks instead of one long quoted line."""


_ReadableDumper.add_representer(
    str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)
)


def pull() -> None:
    PROMPTS_DIR.mkdir(exist_ok=True)
    for name in PROMPT_NAMES:
        remote = get_remote(name)
        if not remote:
            print(f"  ! {name}: not found in Langfuse with label '{LABEL}'")
            continue
        data = {"name": name, "type": "chat", "messages": messages_of(remote)}
        text = HEADER.format(name=name) + f"# Langfuse version: {remote['version']}\n" + yaml.dump(
            data, Dumper=_ReadableDumper, sort_keys=False, allow_unicode=True, width=1000
        )
        (PROMPTS_DIR / f"{name}.yaml").write_text(text, encoding="utf-8")
        print(f"  - {name}: saved v{remote['version']} to prompts/{name}.yaml")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("pull", "push"):
        raise SystemExit(__doc__)
    print(f"{command} prompts ({settings.langfuse_host}, label '{LABEL}'):")
    push() if command == "push" else pull()
