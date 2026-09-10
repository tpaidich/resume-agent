import os
import shutil
import subprocess
import tempfile


def _candidate_name() -> str:
    """Read the candidate's name from the resume rather than hardcoding it,
    so this file carries no personal data."""
    import yaml

    try:
        with open("resume/master.yaml") as f:
            return (yaml.safe_load(f).get("meta", {}).get("name") or "Resume").strip()
    except Exception:
        return "Resume"


def compile_pdf(placeholders: dict, job: dict) -> str:
    """Substitute %%KEY%% tokens into template.tex and compile to PDF."""
    os.makedirs("applications", exist_ok=True)

    from datetime import date

    def _safe(s):
        return "".join(c if c.isalnum() or c in " -" else "" for c in str(s)).strip()

    company = _safe(job.get("company", "Company"))
    title = _safe(job.get("title", "Role"))
    today = date.today().strftime("%m-%d-%Y")
    out_name = f"{_candidate_name()} Resume {company} {today} {title}"

    with open("resume/template.tex") as f:
        source = f.read()

    for key, value in placeholders.items():
        source = source.replace(f"%%{key}%%", value)

    # Warn about any unfilled placeholders
    import re
    remaining = re.findall(r"%%[A-Z_]+%%", source)
    if remaining:
        print(f"[compiler] Warning: unfilled placeholders: {remaining}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tex_path = os.path.join(tmpdir, "resume.tex")
        with open(tex_path, "w") as f:
            f.write(source)

        cls_src = "resume/resume.cls"
        if os.path.exists(cls_src):
            shutil.copy(cls_src, os.path.join(tmpdir, "resume.cls"))

        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "resume.tex"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
        )

        pdf_src = os.path.join(tmpdir, "resume.pdf")
        pdf_dst = os.path.join("applications", f"{out_name}.pdf")

        if os.path.exists(pdf_src):
            shutil.copy(pdf_src, pdf_dst)
            return pdf_dst

        log = (result.stdout or result.stderr)[-2000:]
        raise RuntimeError(f"pdflatex failed:\n{log}")
