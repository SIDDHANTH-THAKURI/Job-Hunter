"""
Generates a general-purpose resume (not tailored to any specific job).
Run: python make_my_resume.py
Output: my-resume/Siddhanth_Resume.docx + Siddhanth_Resume.pdf
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv()

from generator.resume_generator import _profile, _call_claude, _fill_resume, _to_pdf

OUT = BASE_DIR / "my-resume"
OUT.mkdir(exist_ok=True)

GENERIC_JOB = {
    "title":    "Software Developer / Analyst / Junior PM",
    "company":  "",
    "location": "Sydney, NSW",
    "salary":   "",
    "description": (
        "General-purpose resume — not tied to any single job. "
        "Showcase the full range: enterprise software development at Accenture (C#, ASP.NET Core, "
        "React.js, SQL Server, Azure, Agile), machine learning and data science (MCS with Distinction "
        "from UOW, DrugNexusAI capstone), and AI-assisted product development (ShiftMate, HireReady, "
        "Job Hunter). "
        "Suitable for Software Developer, Business Analyst, Data Analyst, Junior PM, or Solutions Consultant. "
        "career_objective: 80-100 words, broad and compelling — lead with enterprise engineering background, "
        "ML graduate credentials, and a track record of shipping real products from real problems. "
        "Do NOT mention any specific company name in the career objective. "
        "Make skills section comprehensive — include all meaningful categories from the profile."
    ),
}


if __name__ == "__main__":
    print("\n  Generating general-purpose resume with Claude Sonnet...")
    profile = _profile()
    result  = _call_claude(profile, GENERIC_JOB)

    resume_data = result["resume"]
    resume_data["title_subtitle"] = "SOFTWARE DEVELOPER  |  ML GRADUATE  |  PRODUCT BUILDER"

    docx = OUT / "Siddhanth_Resume.docx"
    pdf  = OUT / "Siddhanth_Resume.pdf"

    print("  Filling Word template...")
    _fill_resume(resume_data, docx)
    print(f"  Saved: {docx}")

    print("  Converting to PDF...")
    try:
        _to_pdf(docx, pdf)
        print(f"  Saved: {pdf}")
    except Exception as e:
        print(f"  PDF skipped (Word not available): {e}")

    print("\n  Done. Check my-resume/ folder.\n")
