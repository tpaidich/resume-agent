"""Repo-root-relative paths.

The existing tools (latex_compiler, tracker) use paths relative to the process
CWD, which assumes the repo root. Sourcing modules get imported from the
scheduler and from Streamlit, so they resolve paths from __file__ instead and
expose ROOT for callers that need to chdir before calling those older tools.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COMPANIES_FILE = os.path.join(ROOT, "config", "companies.yaml")
APPLICATIONS_DIR = os.path.join(ROOT, "applications")
DB_FILE = os.path.join(APPLICATIONS_DIR, "sourced_jobs.db")
MASTER_RESUME = os.path.join(ROOT, "resume", "master.yaml")
