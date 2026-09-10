import re
import json
import anthropic

client = anthropic.Anthropic()

# Max bullets to include per role (keeps resume on one page)
_BULLET_LIMITS = {
    "factory_intelligence": 3,
    "tcs":                  3,
    "merck":                3,
    "nasa":                 2,
}


def tailor_resume(job: dict, master: dict, gaps: list[str] | None = None) -> dict:
    """Return a %%PLACEHOLDER%% substitution dict for template.tex."""
    sections = _extract_sections(master)
    skills = master.get("skills", {})
    rules = master.get("agent_rules", [])
    coursework = master.get("coursework", [])

    # Determine which courses (if any) are eligible — must address a scorer gap
    eligible_courses = _match_courses_to_gaps(coursework, gaps or [])

    all_tools = (
        skills.get("databases", [])
        + skills.get("infra_and_tools", [])
        + skills.get("bi_and_reporting", [])
        + skills.get("pm_tools", [])
    )
    seen = set()
    all_tools_deduped = [x for x in all_tools if not (x in seen or seen.add(x))]

    # Build bullet instructions with explicit per-role caps
    bullet_instructions = "\n".join(
        f"- {role}: pick the {_BULLET_LIMITS[role]} most relevant bullets from the pool below (no more)"
        for role in ["factory_intelligence", "tcs", "merck", "nasa"]
    )

    coursework_instruction = (
        f"Available courses that address a scorer-identified gap: {json.dumps(eligible_courses)}\n"
        "Include up to 6 of these as 'relevant_courses'. Reorder so strongest match leads."
        if eligible_courses else
        "No scorer gaps match any coursework — return relevant_courses as an empty list []."
    )

    prompt = f"""You are a resume editor. Tailor this resume for the job below.

RULES — violating any of these is a critical failure:
{chr(10).join(f'- {r}' for r in rules)}
- NEVER use em dashes (—). Use semicolons or commas instead. This is absolute.
- Do NOT rewrite bullets. Reorder them and make only minimal wording tweaks to shift emphasis. Preserve every technical term, metric, tool name, and level of detail exactly as written.
- The resume MUST fit on one page. Respect per-role bullet limits strictly.

JOB: {job['title']} at {job['company']}
DESCRIPTION:
{job['description'][:3000]}

--- SKILLS ---
Select only the items most relevant to this job, plus a few others that strengthen the application.
Aim for 4-7 items per row. Drop anything clearly irrelevant. Reorder so strongest matches lead.
All available items (you may only pick from these):
languages pool: {skills.get('languages', [])}
ml_and_data pool: {skills.get('ml_and_data', [])}
tools pool: {all_tools_deduped}

--- EXPERIENCE BULLETS ---
{bullet_instructions}
Reorder so the most job-relevant bullet leads.

factory_intelligence pool: {json.dumps(sections['factory_intelligence'])}
tcs pool: {json.dumps(sections['tcs'])}
merck pool: {json.dumps(sections['merck'])}
nasa pool: {json.dumps(sections['nasa'])}

--- DATA MINE ---
Output exactly 3 bullets, one per sub-project, each prefixed with the company in bold LaTeX.
Format each exactly like: \\textbf{{Company}} (dates): <bullet text>
Source:
  Johnson & Johnson (Jan-May 2026): {json.dumps(sections['data_mine_jj'])}
  Elanco (Aug-Dec 2025): {json.dumps(sections['data_mine_elanco'])}
  NIIMBL (Jan-May 2025): {json.dumps(sections['data_mine_niimbl'])}

--- COURSEWORK ---
{coursework_instruction}

Return JSON only — no prose, no markdown fences:
{{
  "languages": "Python, SQL, R, ...",
  "ml_data": "PyTorch, ...",
  "tools": "Docker, Tableau, ...",
  "factory_intelligence_bullets": ["bullet text", ...],
  "tcs_bullets": ["bullet text", ...],
  "data_mine_bullets": ["\\\\textbf{{Johnson \\\\& Johnson}} (Jan-May 2026): ...", "\\\\textbf{{Elanco}} (Aug-Dec 2025): ...", "\\\\textbf{{NIIMBL}} (Jan-May 2025): ..."],
  "merck_bullets": ["bullet text", ...],
  "nasa_bullets": ["bullet text", ...],
  "relevant_courses": ["Course Name", ...]
}}"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text
    start, end = text.find("{"), text.rfind("}") + 1
    data = json.loads(text[start:end])

    def to_latex(bullets):
        return "\n    ".join(f"\\item {b}" for b in bullets)

    def coursework_section(courses):
        if not courses:
            return ""
        rows = []
        for i in range(0, len(courses), 3):
            chunk = courses[i:i+3]
            rows.append(" & ".join(chunk) + (" & " * (3 - len(chunk))) + r"\\")
        table_body = "\n".join(rows)
        return (
            "\\vspace{-8pt}\n"
            "\\begin{rSection}{Relevant Coursework}\n"
            "\\vspace{-4pt}\n"
            "\\begin{tabular}{ @{} l l l @{} }\n"
            f"{table_body}\n"
            "\\end{tabular}\n"
            "\\end{rSection}\n"
        )

    return {
        "LANGUAGES":                    data["languages"],
        "ML_DATA":                      data["ml_data"],
        "TOOLS":                        data["tools"],
        "FACTORY_INTELLIGENCE_BULLETS": to_latex(data["factory_intelligence_bullets"]),
        "TCS_BULLETS":                  to_latex(data["tcs_bullets"]),
        "DATA_MINE_BULLETS":            to_latex(data["data_mine_bullets"]),
        "MERCK_BULLETS":                to_latex(data["merck_bullets"]),
        "NASA_BULLETS":                 to_latex(data["nasa_bullets"]),
        "COURSEWORK_SECTION":           coursework_section(data.get("relevant_courses", [])),
    }


def _match_courses_to_gaps(coursework: list[str], gaps: list[str]) -> list[str]:
    """Return courses that share meaningful keywords with at least one scorer gap."""
    if not gaps:
        return []
    stop = {"a", "an", "the", "of", "in", "for", "and", "or", "with", "to", "is", "are",
            "experience", "knowledge", "understanding", "ability", "skills", "using"}
    matched = []
    for course in coursework:
        c_words = set(re.sub(r"[^\w\s]", "", course.lower()).split()) - stop
        for gap in gaps:
            g_words = set(re.sub(r"[^\w\s]", "", gap.lower()).split()) - stop
            if c_words & g_words:
                matched.append(course)
                break
    return matched


def _extract_sections(master: dict) -> dict:
    sections = {
        "factory_intelligence": [],
        "tcs": [],
        "data_mine_jj": [],
        "data_mine_elanco": [],
        "data_mine_niimbl": [],
        "merck": [],
        "nasa": [],
    }
    for exp in master.get("experience", []):
        company = exp.get("company", "").lower()
        bullets = exp.get("bullets", [])
        if "factory" in company:
            sections["factory_intelligence"] = bullets
        elif "tata" in company or "tcs" in company:
            sections["tcs"] = bullets
        elif "johnson" in company:
            sections["data_mine_jj"] = bullets
        elif "elanco" in company:
            sections["data_mine_elanco"] = bullets
        elif "niimbl" in company:
            sections["data_mine_niimbl"] = bullets
        elif "merck" in company:
            sections["merck"] = bullets
        elif "nasa" in company:
            sections["nasa"] = bullets
    return sections
