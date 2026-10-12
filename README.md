# LeadScraper Pro

A Streamlit app that finds business websites for a niche and location, crawls them, and extracts public contact details: emails, phone numbers and social profiles.

## Features

- Web search via DuckDuckGo, with Google as a best-effort fallback
- Same-site crawler with configurable depth and page limit (contact/about pages first)
- Extracts emails, phones, social links and business name/description
- Filters: require email, require phone, skip directories and social sites
- Optional robots.txt compliance and politeness delay
- Dashboard, searchable leads table, activity log
- CSV and JSON export

## Installation

Requires Python 3.9+.

```bash
pip install streamlit requests beautifulsoup4 pandas
pip install fake-useragent   # optional
```

## Run

```bash
streamlit run app2.py
```

The app opens at http://localhost:8501.

## Usage

1. Adjust limits in the sidebar (max leads, crawl depth, pages per site, search engine).
2. Open **New Search**, enter a niche (e.g. `Dentists`) and an optional location, then click **Start search**.
3. Review results in **Dashboard** and **Leads Database**.
4. Download CSV or JSON from the sidebar.

## Troubleshooting

| Problem | Fix |
|---|---|
| `streamlit` not recognized | Run `python -m streamlit run app2.py` |
| "No results" or search blocked | Select **DuckDuckGo** as the engine; Google often blocks scripted requests |
| Error about `width` / `use_container_width` | `pip install --upgrade streamlit` |
| Search stops returning results | Search engines change their markup; update the parsers in the search section of `app2.py` |

## Notes

- Extracted contacts are scraped, not verified.
- Respect each site's terms of service and robots.txt, and follow the privacy and anti-spam laws that apply to you when contacting leads.
- For high-volume use, an official search API (Google Programmable Search, SerpAPI) is more reliable than scraping result pages.

## Donate

If this tool saves you time, you can support its development with Bitcoin (BTC):

```
bc1qcvgn85qtg40uf7t88x32d2cfjwjrqhkaqgxkny
```

Please double-check the address before sending. Bitcoin transactions can't be reversed.
