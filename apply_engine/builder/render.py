"""
render.py — document rendering pipeline.
HTML -> PDF (headless Edge/Chrome/Chromium) -> PNG (pymupdf) -> quality checks.

Cross-platform: the browser is auto-detected across Windows / macOS / Linux, with a
BROWSER_PATH env override. No Playwright needed for rendering (it shells out to the
installed browser's --print-to-pdf).
"""

import subprocess
import sys
import os
import time
import tempfile
import shutil
from pathlib import Path

# pymupdf
import fitz


def _browser_candidates():
    """Standard install locations per OS, most-preferred first. Override with BROWSER_PATH."""
    env = os.environ.get("BROWSER_PATH")
    if env:
        return [env]
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        cands = [
            rf"{pfx86}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Microsoft\Edge\Application\msedge.exe",
            rf"{pf}\Google\Chrome\Application\chrome.exe",
            rf"{pfx86}\Google\Chrome\Application\chrome.exe",
        ]
        if local:
            cands.append(rf"{local}\Google\Chrome\Application\chrome.exe")
        return cands
    if sys.platform == "darwin":
        return [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    # linux / other: prefer PATH lookups, then common absolute paths
    on_path = [shutil.which(n) for n in
               ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
    return [p for p in on_path if p] + [
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge", "/snap/bin/chromium",
    ]


def _find_browser():
    for path in _browser_candidates():
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "No headless browser found. Install Microsoft Edge, Google Chrome, or Chromium, "
        "or set the BROWSER_PATH environment variable to the browser executable.")


def html_to_pdf(html_path: str, pdf_path: str) -> str:
    """Convert an HTML file to PDF using a headless browser."""
    browser = _find_browser()
    html_path = str(Path(html_path).resolve())
    pdf_path = str(Path(pdf_path).resolve())

    url = f"file:///{html_path.replace(os.sep, '/')}"

    # CRITICAL: a bare `--headless --print-to-pdf` hands off to any already-running browser
    # instance and creates NO file (or leaves a stale one), so edits never re-render. Each
    # attempt gets its OWN throwaway --user-data-dir to force an isolated instance. Some builds
    # also return 0 before flushing the PDF when relaunched quickly (the auto-fit loop does this),
    # so we retry and always render to a temp file moved into place — a failed render must never
    # masquerade as a stale success.
    last_err = ""
    for attempt in range(4):
        profile_dir = tempfile.mkdtemp(prefix="doc_render_")
        tmp_pdf = os.path.join(profile_dir, "out.pdf")
        try:
            result = subprocess.run(
                [browser, "--headless", "--disable-gpu",
                 f"--user-data-dir={profile_dir}",
                 "--no-first-run", "--no-default-browser-check",
                 "--no-pdf-header-footer",
                 f"--print-to-pdf={tmp_pdf}", url],
                capture_output=True, text=True, timeout=60
            )
            for _ in range(20):  # up to ~5s — poll until the PDF actually lands on disk
                if os.path.isfile(tmp_pdf) and os.path.getsize(tmp_pdf) > 0:
                    break
                time.sleep(0.25)
            if os.path.isfile(tmp_pdf) and os.path.getsize(tmp_pdf) > 0:
                shutil.move(tmp_pdf, pdf_path)
                return pdf_path
            last_err = f"returncode={result.returncode} stderr: {result.stderr[:300]}"
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
        time.sleep(0.6)

    raise RuntimeError(
        f"PDF not created after 4 attempts. {last_err or 'browser exited 0 but never flushed a PDF.'}")


def pdf_page_count(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    count = len(doc)
    doc.close()
    return count


def pdf_to_png(pdf_path: str, png_path: str, page: int = 0, dpi: int = 200) -> str:
    doc = fitz.open(pdf_path)
    if page >= len(doc):
        raise ValueError(f"Page {page} out of range (doc has {len(doc)} pages)")
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page].get_pixmap(matrix=mat)
    pix.save(png_path)
    doc.close()
    return png_path


def pdf_extract_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text


_FILE_NAMES = {"resume": "RESUME", "cover_letter": "COVER_LETTER"}


def render_and_check(html_path: str, output_dir: str, name: str = "document",
                     applicant_name: str = "", contact_token: str = "") -> dict:
    """Full pipeline: HTML -> PDF -> PNG + quality checks.

    `applicant_name` / `contact_token` parameterize the has-name / has-contact checks so the
    gate works for ANY applicant (no hardcoded identity). Pass them from the profile; leave empty
    to skip that specific check.

    Returns: pdf_path, png_path, page_count, checks, all_pass.
    """
    output_dir = str(Path(output_dir).resolve())
    os.makedirs(output_dir, exist_ok=True)

    pdf_name = f"{_FILE_NAMES.get(name, name.upper())}.pdf"
    png_name = f"{name}_preview.png"
    pdf_path = os.path.join(output_dir, pdf_name)
    png_path = os.path.join(output_dir, png_name)

    html_to_pdf(html_path, pdf_path)
    page_count = pdf_page_count(pdf_path)
    pdf_to_png(pdf_path, png_path, page=0)
    text = pdf_extract_text(pdf_path)

    checks = {}
    checks["page_count_is_1"] = (page_count == 1)
    checks["text_not_empty"] = (len(text.strip()) > 200)
    if applicant_name:
        checks["has_name"] = (applicant_name.upper() in text.upper())
    if contact_token:
        checks["has_contact"] = (contact_token in text)

    all_pass = all(checks.values())
    return {
        "pdf_path": pdf_path,
        "png_path": png_path,
        "page_count": page_count,
        "text_length": len(text),
        "checks": checks,
        "all_pass": all_pass,
    }


def inject_css_var(html_path: str, var_name: str, value: str, output_path: str = None) -> str:
    """Replace a CSS custom-property value (`--var_name: <old>;`) in the HTML file."""
    output_path = output_path or html_path
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    import re
    pattern = rf"(--{re.escape(var_name)}\s*:\s*)([^;]+)(;)"
    replacement = rf"\g<1>{value}\3"
    content = re.sub(pattern, replacement, content)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    return output_path


def auto_fit_one_page(html_path: str, output_dir: str, name: str = "document",
                      applicant_name: str = "", contact_token: str = "",
                      min_font_pt: float = 8.0, max_attempts: int = 6) -> dict:
    """Iteratively tighten CSS variables until the PDF fits on one page.
    Returns the final render_and_check result, plus `autofit_adjustments` (number of tightening
    passes — >0 on a cover letter means the content ran long and should be re-drafted shorter)."""
    work_html = os.path.join(output_dir, f"{name}_working.html")
    os.makedirs(output_dir, exist_ok=True)
    shutil.copy2(html_path, work_html)

    attempt = 0
    result = render_and_check(work_html, output_dir, name, applicant_name, contact_token)

    while not result["checks"]["page_count_is_1"] and attempt < max_attempts:
        attempt += 1
        print(f"  [auto-fit] attempt {attempt}: {result['page_count']} pages, tightening...")
        if attempt <= 2:
            inject_css_var(work_html, "section-gap", f"{max(4, 8 - attempt * 2)}px")
            inject_css_var(work_html, "role-gap", f"{max(2, 5 - attempt)}px")
            inject_css_var(work_html, "bullet-gap", "1px")
        elif attempt <= 4:
            new_size = max(min_font_pt, 9.0 - (attempt - 2) * 0.5)
            inject_css_var(work_html, "body-font", f"{new_size}pt")
            inject_css_var(work_html, "bullet-font", f"{new_size}pt")
        else:
            inject_css_var(work_html, "page-margin-tb", "0.45in")
            inject_css_var(work_html, "page-margin-lr", "0.65in")
        result = render_and_check(work_html, output_dir, name, applicant_name, contact_token)

    result["autofit_adjustments"] = attempt

    if result["checks"]["page_count_is_1"]:
        final_html = os.path.join(output_dir, f"{name}_final.html")
        shutil.copy2(work_html, final_html)
        result["final_html"] = final_html
        print(f"  [auto-fit] OK - 1 page after {attempt} adjustment(s)")
    else:
        print(f"  [auto-fit] WARN - still {result['page_count']} pages after {max_attempts} attempts")

    if os.path.exists(work_html):
        os.remove(work_html)
    return result
