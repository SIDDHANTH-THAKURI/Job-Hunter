# Job Hunter

An automated job-hunting system that crawls Seek, scores every listing against your profile using AI, and generates a fully tailored resume and cover letter for each role — all from a local dashboard.

---

## What it does

| Step | What happens |
|------|-------------|
| **Crawl** | Fetches live Seek listings for every role in your `target_roles` list (from `me.json`) |
| **Score** | Claude AI reads your profile and gives each job a 1-10 relevance score with a reason and missing skills |
| **Generate** | For any job you click, Claude writes a tailored resume and cover letter, fills a Word template, and exports PDF |
| **Dashboard** | Browse, filter, sort, and manage all jobs in a local web UI at `http://localhost:5000` |

---

## Requirements

- **Python 3.10+**
- **Microsoft Word** (required for PDF export — `docx2pdf` uses Word's COM interface on Windows/macOS)
- **[Apify](https://apify.com) account** — free tier available (used to scrape Seek)
- **[Anthropic](https://console.anthropic.com) account** — pay-as-you-go (Claude Sonnet for generation, Haiku for scoring)

> Estimated cost per 100 jobs scored + 5 resumes generated: ~$0.15-0.30 USD

---

## Getting API keys

### Anthropic (Claude AI)
1. Sign up at [console.anthropic.com](https://console.anthropic.com)
2. Go to **API Keys** and create a new key
3. Copy the key — it starts with `sk-ant-api03-...`

### Apify (Seek scraper)
1. Sign up at [apify.com](https://apify.com)
2. Go to **Settings > Integrations > API tokens**
3. Copy your Personal API token — it starts with `apify_api_...`
4. The scraper used is `websift/seek-job-scraper` — no extra setup needed, it runs automatically

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/SIDDHANTH-THAKURI/Job-Hunter.git
cd Job-Hunter

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Create your .env file
cp .env.example .env
# Then edit .env and fill in your API keys
```

**.env contents:**
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

Key sections to fill in:

| Section | What to put |
|---------|-------------|
| `personal` | Name, email, phone, location, LinkedIn, GitHub, portfolio, visa status |
| `job_preferences` | Salary range, work type, locations (city, state) |
| `target_roles` | List of job titles to search Seek for — the crawler uses this list directly |
| `skills` | Your tech stack, tools, and honest skill assessment |
| `work_experience` | Each role with `title`, `company`, `period`, `framing`, `bullet_count`, `experience_category` (`"relevant"` or `"other"`) |
| `education` | Each qualification with `education_category` (`"main"` or `"additional"`) and `resume_bullets` |
| `projects` | Academic and personal projects with `include_by_default: true` for ones to always show on resume |

> `me.json` is in `.gitignore` — it will never be pushed to GitHub.

### How the crawler uses your profile

The crawler reads directly from `me.json` — no code editing needed:

- **Search terms**: every entry in `target_roles` becomes a Seek search
- **Location**: first entry in `job_preferences.locations` (e.g. `"Sydney, NSW"`)
- **Salary ceiling**: 1.5x your `job_preferences.salary_range.max` — jobs above this are filtered out as too senior

---

## Generate resume templates

Before first use, generate the Word templates:

```bash
python create_templates.py
```

This creates `templates/resume.docx` and `templates/cover.docx`. You can open these in Word to adjust fonts, spacing, or colours — but **do not delete the `{{ }}` or `{%p %}` tags**.

---

## Run

```bash
python run.py
```

This starts the dashboard at [http://localhost:5000](http://localhost:5000) and opens your browser automatically.

---

## Dashboard usage

| Button | What it does |
|--------|-------------|
| **Crawl Seek** | Fetches new job listings (takes ~5-8 mins) |
| **Score All** | Runs Claude Haiku to score every unscored job (fast, cheap) |
| **Score** (per row) | Score a single job |
| **Generate** (per row) | Generate tailored resume and cover letter for that job |
| **Regenerate** | Force re-generate even if cached documents exist |
| **Download** | Download `.docx` or `.pdf` for resume or cover letter |
| **Discard** | Hide a job from the list |
| **Usage** | See your Anthropic and Apify API usage and estimated costs |

Use the **min score slider** and **search box** to filter jobs. Jobs with visa concerns show a red **VISA?** badge.

---

## Project structure

```
job-hunter/
├── app.py                      # Flask API server
├── run.py                      # Entry point (starts server + opens browser)
├── create_templates.py         # Generates Word templates for resume and cover letter
├── utils.py                    # API cost logging helper
├── me.json                     # YOUR profile (gitignored — never pushed)
├── me.json.example             # Template for me.json
├── .env                        # API keys (gitignored — never pushed)
├── crawler/
│   └── seek_crawler.py         # Fetches jobs from Seek via Apify
├── scorer/
│   └── score_jobs.py           # Scores jobs with Claude Haiku (fast, cheap)
├── generator/
│   └── resume_generator.py     # Generates tailored resume and cover letter with Claude Sonnet
├── dashboard/
│   └── index.html              # Single-page local dashboard
├── templates/                  # Word templates (generated by create_templates.py)
├── output/                     # Generated DOCX and PDF files (gitignored)
└── data/
    └── jobs.db                 # SQLite database (gitignored)
```

---

## Notes

- **PDF export requires Microsoft Word.** If Word is not installed, `.docx` files will still be generated and downloadable — only PDF will be skipped (a warning appears in the dashboard).
- **Close Word documents** before regenerating — Word locks files that are open.
- **Scores below 5** are auto-discarded and hidden from the dashboard.
- The resume is ATS-safe: plain text paragraphs, no tables, columns, or text boxes.
- All social links and project URLs in the generated DOCX are proper blue underlined hyperlinks.
- Date formats are consistent throughout (e.g. `Jan 2024 - Mar 2025`).

---

## Privacy

Your personal data (`me.json`) and API keys (`.env`) are in `.gitignore` and will never be committed or pushed. The SQLite database (`data/jobs.db`) and all generated output files are also gitignored.
