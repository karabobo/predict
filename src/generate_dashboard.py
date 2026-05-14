"""Generate static HTML dashboard for GitHub Pages."""

from pathlib import Path
from dashboard import RULE_CANDIDATE_REPORT_PATH, build_html, render_rule_candidate_markdown

DOCS_DIR = Path(__file__).parent.parent / "docs"


def generate():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    output = DOCS_DIR / "index.html"
    output.write_text(build_html(lite_homepage=True))
    RULE_CANDIDATE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULE_CANDIDATE_REPORT_PATH.write_text(render_rule_candidate_markdown())
    print(f"  Dashboard written to {output}")
    print(f"  Rule candidate report written to {RULE_CANDIDATE_REPORT_PATH}")


if __name__ == "__main__":
    generate()
