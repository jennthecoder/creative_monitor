"""Render the web app to static HTML for GitHub Pages.

    python -m scripts.build_site [--out site] [--base /creative_monitor]

Crawls from the digest page and follows every internal link, so whatever the
templates link to gets published and nothing else. `--base` is the path the site
is served under (a project site lives at https://<user>.github.io/<repo>/).
Fails if any linked page doesn't render, so a broken link never ships.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

os.environ["STATIC_SITE"] = "1"  # before the app is imported: rubric page is read-only

from web.app import app  # noqa: E402

LINK_RE = re.compile(r'(?:href|src)="([^"#?]+)"')


def build(out: Path, base: str) -> int:
    base = "/" + base.strip("/") if base.strip("/") else ""
    client = app.test_client()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    seen, queue, failures = set(), ["/"], []
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        r = client.get(path, base_url=f"http://localhost{base}/")
        if r.status_code != 200:
            failures.append(f"{r.status_code} {path}")
            continue
        target = out / path.lstrip("/")
        if path.endswith("/"):
            target = target / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(r.data)
        if r.mimetype == "text/html":
            for href in LINK_RE.findall(r.get_data(as_text=True)):
                if href.startswith(base + "/"):
                    queue.append(href[len(base):])

    (out / ".nojekyll").touch()  # serve files as-is (no Jekyll processing)
    print(f"Built {len(seen) - len(failures)} files into {out}/ (base '{base or '/'}')")
    for f in failures:
        print(f"  failed: {f}", file=sys.stderr)
    return 1 if failures else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="site", type=Path)
    p.add_argument("--base", default="", help="URL path the site is served under, e.g. /creative_monitor")
    a = p.parse_args()
    sys.exit(build(a.out, a.base))


if __name__ == "__main__":
    main()
