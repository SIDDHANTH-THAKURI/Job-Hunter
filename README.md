# Job Hunter

A local job-hunting system that crawls Seek, LinkedIn, and Indeed, scores every listing with Claude AI, and generates a tailored resume and cover letter for each role — all from a browser-based dashboard running on your machine.

---

## What it does

| Step | What happens |
|------|-------------|
| **Crawl** | Fetches live listings from Seek, LinkedIn, and Indeed for every role in your `target_roles` list |
| **Score** | Claude AI reads your profile and gives each job a 1–10 relevance score with a reason and list of missing skills |
| **Generate** | For any job you click, Claude writes a tailored resume and cover letter, fills a Word template, and exports to PDF |
| **Dashboard** | Browse, filter, sort, and manage all jobs in a local web UI at `http://localhost:5000` |
| **Manual entry** | Paste any job description directly into the dashboard — it scores and generates like any crawled job |
| **General resume** | Run `python make_my_resume.py` to generate a generic (non-tailored) resume from your profile |

---

## Requirements

- **Python 3.10+**
- **Microsoft Word** (required for PDF export — `docx2pdf` uses Word's COM interface on Windows/macOS)
- **[Apify](https://apify.com) account** — free tier works (used to scrape Seek, LinkedIn, and Indeed)
- **[Anthropic](https://console.anthropic.com) account** — pay-as-you-go (Claude Sonnet for generation, Haiku for scoring)

> Estimated cost per 100 jobs scored + 5 resumes generated: ~$0.15–0.30 USD

---

## Getting API keys

### Anthropic (Claude AI)
1. Sign up at [console.anthropic.com](https://console.anthropic.com)
2. Go to **API Keys** → create a new key
3. Copy it — it starts with `sk-ant-api03-...`

### Apify (job scrapers)
1. Sign up at [apify.com](https://apify.com)
2. Go to **Settings → Integrations → API tokens**
3. Copy your Personal API token — it starts with `apify_api_...`

The scrapers used are:
- `websift/seek-job-scraper` — Seek listings
- `bebity/linkedin-jobs-scraper` — LinkedIn listings
- `misceres/indeed-scraper` — Indeed listings (Australian results)

No extra Apify setup is needed — the actors run automatically when you crawl.

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/SIDDHANTH-THAKURI/Job-Hunter.git
cd Job-Hunter

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install Playwright browser (used by the Indeed bot — safe to run even if you won't use it)
python -m playwright install chromium

# 4. Create your .env file
cp .env.example .env
# Edit .env and fill in your API keys
```

**.env file:**
```
APIFY_API_KEY=apify_api_your_key_here
ANTHROPIC_API_KEY=sk-ant-api03-your_key_here
```

---

## Configure your profile

Copy `me.json.example` to `me.json` and fill in your details:

```bash
cp me.json.example me.json
```

Open `me.json` in any text editor. The key sections are:

| Section | What to fill in |
|---------|----------------|
| `personal` | Name, email, phone, location, LinkedIn, GitHub, portfolio, visa status |
| `job_preferences` | Salary range, work type (full-time/part-time/contract), locations (city, state) |
| `target_roles` | Job titles to search — the crawler uses this list directly as search terms |
| `skills` | Your honest skill breakdown — used by Claude to write accurate resumes without overclaiming |
| `work_experience` | Each role with `title`, `company`, `period`, `framing`, `key_points`, and `experience_category` |
| `education` | Degrees and certifications with `resume_bullets` |
| `projects` | Projects with `highlights`, `stack`, and `include_by_default` |
| `gaps_to_be_aware_of` | Skills gaps or experience gaps — Claude uses this to avoid overclaiming |

> `me.json` is in `.gitignore` and will never be committed or pushed to GitHub.

### The `skills.honest_assessment` field

This is a 2–3 sentence plain-English description of your actual skill level. Claude uses it as a guide when writing your resume — be truthful. If you inflate this, your resume will overclaim and you will struggle in interviews.

### How the crawler uses your profile

- **Search terms**: every entry in `target_roles` is used as a Seek/LinkedIn/Indeed search query
- **Locations**: the `job_preferences.locations` list is searched across all sources
- **Salary filter**: jobs priced above 1.5× your `salary_range.max` are filtered out as too senior

---

## Generate Word templates

Before first use, generate the resume and cover letter Word templates:

```bash
python create_templates.py
```

This creates `templates/resume.docx` and `templates/cover.docx`. You can open these in Word to adjust fonts, spacing, or colours — but **do not delete or modify the `{{ }}` or `{%p %}` tags**, as these are filled by the generator.

---

## Run

```bash
python run.py
```

This starts the dashboard at [http://localhost:5000](http://localhost:5000) and opens it in your browser automatically.

---

## Dashboard usage

### Seek / LinkedIn / Indeed tabs

All three job sources share the same interface:

| Button | What it does |
|--------|-------------|
| **Crawl Seek / Crawl LinkedIn / Crawl Indeed** | Fetches new listings (~5–10 min per source) |
| **Score All** | Runs Claude Haiku to score every unscored job (fast, cheap) |
| **Score** (per row) | Score a single job |
| **Generate** (per row) | Generate a tailored resume and cover letter for that job |
| **Regenerate** | Force re-generate even if documents already exist |
| **Download** | Download `.docx` or `.pdf` for resume or cover letter |
| **Discard** | Hide a job from the list |
| **Add Job Manually** | Paste any job description to add it without crawling |
| **Usage** | View Anthropic and Apify API usage and estimated costs |

Use the **min score slider** and **search box** to filter the list. Jobs with potential visa issues show a red **VISA?** badge.

### Status tracking

Each job has a status you can update: `Not Applied → Applied → Interview → Offer → Rejected`. This is tracked in the local database and visible in the table.

### Generate a general resume

To generate a non-tailored resume (useful for sending speculatively):

```bash
python make_my_resume.py
```

Output goes to `my-resume/` (gitignored).

---

## Project structure

```
job-hunter/
├── app.py                      # Flask API server
├── run.py                      # Entry point — starts server and opens browser
├── create_templates.py         # Generates Word templates for resume and cover letter
├── make_my_resume.py           # Generates a general (non-tailored) resume
├── utils.py                    # API cost logging helper
├── me.json                     # YOUR profile (gitignored — never pushed)
├── me.json.example             # Template — copy this to me.json and fill it in
├── .env                        # API keys (gitignored — never pushed)
├── .env.example                # Template for .env
├── crawler/
│   └── seek_crawler.py         # Crawls Seek, LinkedIn, and Indeed via Apify
├── scorer/
│   └── score_jobs.py           # Scores jobs with Claude Haiku
├── generator/
│   └── resume_generator.py     # Generates tailored resumes and cover letters with Claude Sonnet
├── bot/
│   ├── indeed_apply.py         # Indeed auto-apply bot (coming soon — currently disabled)
│   └── indeed_pipeline.py      # Pipeline: crawl → score → generate → apply
├── dashboard/
│   └── index.html              # Single-page local dashboard
├── templates/                  # Word templates (generated by create_templates.py, gitignored)
├── output/                     # Generated DOCX and PDF files (gitignored)
└── data/
    └── jobs.db                 # SQLite database (gitignored)
```

---

## Notes

- **PDF export requires Microsoft Word.** `.docx` files are always generated — PDF is skipped with a warning if Word is not installed.
- **Close Word documents before regenerating** — Word locks open files and the generator will fail.
- **Jobs scored below 5** are auto-discarded and hidden from the dashboard.
- The generated resume is ATS-safe: plain text paragraphs with no tables, columns, or text boxes.
- All social links and project URLs in the generated DOCX are proper blue underlined hyperlinks.
- Cross-source deduplication is applied — if the same job appears on both Seek and LinkedIn, it is only kept once.

---

## Coming soon

- **Indeed auto-apply bot** — The `bot/` directory contains a Playwright-based bot that can automatically fill and submit Easy Apply forms on Indeed. It is currently disabled in the dashboard due to Cloudflare bot detection challenges. Contributions welcome.

---

## Privacy

Your personal data (`me.json`), API keys (`.env`), generated output (`output/`), and the job database (`data/jobs.db`) are all in `.gitignore` and will never be committed or pushed.
