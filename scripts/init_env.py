"""Create .env from .env.example with fresh random secrets. Refuses to overwrite."""
import pathlib
import re
import secrets
import sys

root = pathlib.Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists() and "--force" not in sys.argv:
    sys.exit(".env already exists (use --force to regenerate; n8n credentials must then be re-provisioned)")

lines = []
for line in (root / ".env.example").read_text().splitlines():
    m = re.match(r"^([A-Z0-9_]+)=change-me", line)  # trailing comment is dropped
    lines.append(f"{m.group(1)}={secrets.token_urlsafe(32)}" if m else line)
target.write_text("\n".join(lines) + "\n")
print(f"wrote {target}")
