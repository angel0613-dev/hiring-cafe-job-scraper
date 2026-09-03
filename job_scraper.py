import csv
import json
import re
import sys
import time
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# hiring.cafe is a Next.js app. Its /api/search-jobs endpoint used to accept an
# unauthenticated POST; it is now GET-only and returns 401 without credentials.
# The server-rendered page still embeds the same job records in __NEXT_DATA__ at
# props.pageProps.ssrHits, and it honours the searchState passed in the query
# string, so we read the results from there instead.

BASE_URL = "https://hiring.cafe"
TIMEOUT = (10, 60)  # (connect, read) - the SSR page is large and slow to build

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

UNITED_STATES = {
    "formatted_address": "United States",
    "types": ["country"],
    "geometry": {"location": {"lat": "39.8283", "lon": "-98.5795"}},
    "id": "user_country",
    "address_components": [
        {"long_name": "United States", "short_name": "US", "types": ["country"]}
    ],
    "options": {"flexible_regions": ["anywhere_in_continent", "anywhere_in_world"]},
}

PAY_PERIODS = ["yearly", "monthly", "weekly", "bi-weekly", "daily", "hourly"]


def make_session():
    """A session that retries on the timeouts and 5xx errors this host throws."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        backoff_factor=2,  # waits 0s, 2s, 4s, 8s between attempts
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
    return session


def build_search_state(search_query, **overrides):
    """
    Build the searchState object the site expects.

    Any keyword argument overrides a default, e.g.
        build_search_state("Nurse", workplaceTypes=["Remote"])
    """
    state = {
        "locations": [UNITED_STATES],
        "workplaceTypes": ["Remote", "Hybrid", "Onsite"],
        "defaultToUserLocation": False,
        "userLocation": None,
        "commitmentTypes": [
            "Full Time",
            "Part Time",
            "Contract",
            "Internship",
            "Temporary",
            "Seasonal",
            "Volunteer",
        ],
        "seniorityLevel": ["No Prior Experience Required", "Entry Level", "Mid Level"],
        "roleTypes": ["Individual Contributor", "People Manager"],
        "searchQuery": search_query,
        "dateFetchedPastNDays": 61,
        "sortBy": "default",
        "companyPublicOrPrivate": "all",
        "isNonProfit": "all",
    }
    state.update(overrides)
    return state


def fetch_hits(search_state, session=None):
    """Fetch one page of raw job records for a given searchState."""
    session = session or make_session()
    url = f"{BASE_URL}/?searchState={quote(json.dumps(search_state))}"
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()

    match = NEXT_DATA_RE.search(response.text)
    if not match:
        raise ValueError("__NEXT_DATA__ not found - the page layout may have changed")

    return json.loads(match.group(1))["props"]["pageProps"].get("ssrHits", [])


def _compensation(v5):
    """Render whichever pay period the listing actually populated."""
    currency = v5.get("listed_compensation_currency") or "USD"
    for period in PAY_PERIODS:
        low = v5.get(f"{period}_min_compensation")
        high = v5.get(f"{period}_max_compensation")
        if low or high:
            if low and high and low != high:
                return f"{currency} {low:,.0f} - {high:,.0f} / {period}"
            return f"{currency} {(low or high):,.0f} / {period}"
    return ""


def flatten_job(job):
    """
    Turn a 560-line raw record into a flat row with the useful fields up front.

    The raw payload is kept under "raw" so nothing is lost.
    """
    v5 = job.get("v5_processed_job_data") or {}
    info = job.get("job_information") or {}
    company = job.get("enriched_company_data") or {}

    cities = v5.get("workplace_cities") or []
    location = cities[0] if cities else ", ".join(v5.get("workplace_countries") or [])

    return {
        "title": info.get("title") or v5.get("core_job_title") or "",
        "company": v5.get("company_name") or company.get("name") or job.get("source", ""),
        "location": location,
        "workplace_type": v5.get("workplace_type") or "",
        "commitment": ", ".join(v5.get("commitment") or []),
        "seniority": v5.get("seniority_level") or "",
        "compensation": _compensation(v5),
        "apply_url": job.get("apply_url") or "",
        "posted_date": (v5.get("estimated_publish_date") or "")[:10],
        "requirements_summary": v5.get("requirements_summary") or "",
        "role_activities": ", ".join(v5.get("role_activities") or []),
        "technical_tools": ", ".join(v5.get("technical_tools") or []),
        "min_years_experience": v5.get("min_industry_and_role_yoe"),
        "company_website": company.get("homepage_uri") or v5.get("company_website") or "",
        "company_size": company.get("nb_employees"),
        "company_industry": ", ".join(company.get("industries") or []),
        "source": job.get("source") or "",
        "board_token": job.get("board_token") or "",
        "id": job.get("id") or "",
        "raw": job,
    }


def scrape_hiring_cafe_jobs(search_query, deep=False, flat=True, **filters):
    """
    Scrape job listings from hiring.cafe.

    Args:
        search_query (str): The search term for job listings.
        deep (bool): Also query narrower filter slices and merge the results.
                     The site only server-renders one page per query, so this
                     is how to get past that cap.
        flat (bool): Return flattened rows instead of the raw nested records.
        **filters: Any searchState override, e.g. workplaceTypes=["Remote"].

    Returns:
        list: Job dicts, de-duplicated by id.
    """
    slices = [{}]
    if deep:
        slices += (
            [{"workplaceTypes": [t]} for t in ("Remote", "Hybrid", "Onsite")]
            + [
                {"seniorityLevel": [s]}
                for s in ("No Prior Experience Required", "Entry Level", "Mid Level")
            ]
            + [{"commitmentTypes": [c]} for c in ("Full Time", "Part Time", "Contract")]
        )

    session = make_session()
    jobs_by_id = {}
    failed = []

    for index, extra in enumerate(slices):
        state = build_search_state(search_query, **{**filters, **extra})
        label = ", ".join(f"{k}={v}" for k, v in extra.items()) or "all filters"

        try:
            hits = fetch_hits(state, session=session)
        except (requests.exceptions.RequestException, ValueError) as error:
            print(f"  [{label}] failed after retries: {type(error).__name__}")
            failed.append(label)
            continue

        new = sum(1 for hit in hits if hit.get("id") not in jobs_by_id)
        for hit in hits:
            if hit.get("id"):
                jobs_by_id[hit["id"]] = hit
        print(f"  [{label}] {len(hits)} hits, {new} new (total {len(jobs_by_id)})")

        if index < len(slices) - 1:
            time.sleep(1.5)

    if failed:
        print(f"\n  {len(failed)} slice(s) failed: {'; '.join(failed)}")

    jobs = list(jobs_by_id.values())
    return [flatten_job(job) for job in jobs] if flat else jobs


def save_jobs_to_json(jobs, filename, include_raw=False):
    """Save job data to a JSON file, dropping the bulky raw payload by default."""
    if not jobs:
        print("No jobs to save")
        return
    if not include_raw:
        jobs = [{k: v for k, v in job.items() if k != "raw"} for job in jobs]
    with open(filename, "w", encoding="utf-8") as handle:
        json.dump(jobs, handle, indent=2, ensure_ascii=False)
    print(f"Saved {len(jobs)} jobs to {filename}")


def save_jobs_to_csv(jobs, filename):
    """Save the flat fields to CSV - the fastest way to eyeball the results."""
    if not jobs:
        return
    columns = [key for key in jobs[0] if key != "raw"]
    with open(filename, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(jobs)
    print(f"Saved {len(jobs)} jobs to {filename}")


if __name__ == "__main__":
    # Job titles and companies are frequently non-ASCII; the default Windows
    # console codepage cannot encode them.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    search_term = input("Enter search term (e.g., 'Data Scientist', 'Software Engineer'): ").strip()
    deep = input("Deep search across filter slices? (y/N): ").strip().lower().startswith("y")

    print(f"\nSearching hiring.cafe for '{search_term}'...")
    jobs = scrape_hiring_cafe_jobs(search_term, deep=deep)

    if not jobs:
        print("\nNo jobs found.")
        raise SystemExit(1)

    stem = search_term.lower().replace(" ", "_")
    print()
    save_jobs_to_json(jobs, f"{stem}_jobs.json")
    save_jobs_to_csv(jobs, f"{stem}_jobs.csv")

    print(f"\nFound {len(jobs)} jobs for '{search_term}':\n")
    for job in jobs[:10]:
        print(f"  {job['title']} | {job['company']} | {job['location'] or 'n/a'}")
        print(f"      {job['apply_url']}")
