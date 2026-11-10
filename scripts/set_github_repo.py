"""Point the Unraid template and README at the GitHub repo / GHCR image.

Usage: python scripts/set_github_repo.py owner/repo
"""
import pathlib
import re
import sys

if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", sys.argv[1]):
    sys.exit(__doc__)
owner, repo = sys.argv[1].split("/")
root = pathlib.Path(__file__).resolve().parent.parent
for rel in ("unraid/omniarr.xml", "README.md"):
    f = root / rel
    text = f.read_text()
    # GHCR image names must be lower case; GitHub URLs are case-insensitive
    text = text.replace("ghcr.io/OWNER/REPO", f"ghcr.io/{owner.lower()}/{repo.lower()}")
    text = text.replace("pkgs/container/REPO", f"pkgs/container/{repo.lower()}")
    text = text.replace("OWNER/REPO", f"{owner}/{repo}")
    f.write_text(text)
    print("updated", rel)
