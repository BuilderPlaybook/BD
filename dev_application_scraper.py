"""
Development Application Contact Scraper — enriches ArcGIS data with
applicant contact info from Tempest detail pages via Playwright.

Step 1: Pull active applications from ArcGIS API (fast, free)
Step 2: Scrape each detail page for the Application Contact (Playwright)
Step 3: Merge and save the enriched dataset

URL pattern: tender.victoria.ca/webapps/ourcity/Prospero/Details.aspx?folderNumber={FOLDER_NUMBER}

Standalone version — no database, no external services. Outputs a JSON
file to ./output/dev_application_leads_enriched.json.

Requires Playwright + a Chromium build:
    pip install playwright
    playwright install chromium

Usage:
    python dev_application_scraper.py
    python dev_application_scraper.py --limit 20
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

# All output is written next to this script, in ./output/
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


# ═══════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════

VICTORIA_CONFIG = {
    "arcgis_url": "https://maps.victoria.ca/server/rest/services/OpenData/OpenData_PlanningAndDevelopment/MapServer",
    "active_layer": 3,
    "detail_url_template": "https://tender.victoria.ca/webapps/ourcity/Prospero/Details.aspx?folderNumber={folder}",
    "delay_seconds": 2.5,  # Rate limit: 2.5s between page loads — be polite to the city's server
}


# ═══════════════════════════════════════════════════════
# Step 1: Pull from ArcGIS API
# ═══════════════════════════════════════════════════════

def fetch_active_applications() -> list[dict]:
    """Pull all active dev applications from ArcGIS REST API."""
    url = f"{VICTORIA_CONFIG['arcgis_url']}/{VICTORIA_CONFIG['active_layer']}/query"
    all_records = []
    offset = 0

    while True:
        try:
            r = httpx.get(url, params={
                "where": "1=1", "outFields": "*", "returnGeometry": "false",
                "f": "json", "resultRecordCount": 200, "resultOffset": offset,
            }, timeout=30)
            data = r.json()
            if "error" in data:
                print(f"    API error: {data['error']}", flush=True)
                break
            features = data.get("features", [])
            print(f"    Batch at offset {offset}: {len(features)} features", flush=True)
            if not features:
                break
            for f in features:
                all_records.append(f["attributes"])
            if len(features) < 200:
                break
            offset += 200
        except Exception as e:
            print(f"    Fetch error: {e}", flush=True)
            break

    return all_records


# ═══════════════════════════════════════════════════════
# Step 2: Scrape detail pages with Playwright
# ═══════════════════════════════════════════════════════

async def scrape_detail_pages(folder_numbers: list[str], limit: int = 0) -> dict[str, dict]:
    """Scrape Tempest detail pages for applicant contact info."""
    from playwright.async_api import async_playwright

    if limit > 0:
        folder_numbers = folder_numbers[:limit]

    results = {}
    total = len(folder_numbers)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for idx, fn in enumerate(folder_numbers):
            url = VICTORIA_CONFIG["detail_url_template"].format(folder=fn)
            try:
                await page.goto(url, wait_until="networkidle", timeout=15000)
                await page.wait_for_timeout(500)

                text = await page.inner_text("body")
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                # Parse fields from the detail page
                parsed = parse_detail_page(lines)
                results[fn] = parsed

                if (idx + 1) % 10 == 0 or idx == 0:
                    print(f"    [{idx+1}/{total}] {fn}: {parsed.get('applicant_name', '?')[:30]}", flush=True)

            except Exception as e:
                results[fn] = {"error": str(e)}
                if (idx + 1) % 10 == 0:
                    print(f"    [{idx+1}/{total}] {fn}: ERROR", flush=True)

            # Rate limiting
            await asyncio.sleep(VICTORIA_CONFIG["delay_seconds"])

        await browser.close()

    return results


def parse_detail_page(lines: list[str]) -> dict:
    """Parse structured data from Tempest detail page text."""
    data = {
        "applicant_name": "",
        "applicant_phone": "",
        "applicant_email": "",
        "project_type": "",
        "status": "",
        "purpose_full": "",
        "related_applications": [],
    }

    i = 0
    while i < len(lines):
        line = lines[i]

        if line == "Application Contact:" and i + 1 < len(lines):
            data["applicant_name"] = lines[i + 1].strip()
            # Check next lines for phone/email (format: "Telephone: xxx", "Email: xxx")
            for j in range(i + 2, min(i + 6, len(lines))):
                lj = lines[j].strip()
                if lj.startswith("Telephone:"):
                    data["applicant_phone"] = lj.replace("Telephone:", "").strip()
                elif lj.startswith("Email:") and "victoria.ca" not in lj.lower():
                    data["applicant_email"] = lj.replace("Email:", "").strip()
                elif lj in ("Project Type:", "City Contact:"):
                    break

        elif line == "Project Type:" and i + 1 < len(lines):
            data["project_type"] = lines[i + 1].strip()

        elif line == "Status:" and i + 1 < len(lines):
            data["status"] = lines[i + 1].strip()

        elif line == "Purpose:" and i + 1 < len(lines):
            purpose_lines = []
            for j in range(i + 1, min(i + 10, len(lines))):
                if lines[j] in ("Links:", "Related Permits", "Related Permits and Applications"):
                    break
                purpose_lines.append(lines[j])
            data["purpose_full"] = " ".join(purpose_lines)

        i += 1

    return data


# ═══════════════════════════════════════════════════════
# Step 3: Merge and classify
# ═══════════════════════════════════════════════════════

def classify_project(app: dict) -> str:
    """Classify application into project type."""
    purpose = (app.get("purpose", "") + " " + app.get("purpose_full", "")).lower()
    app_type = app.get("app_type", "").lower()

    if any(kw in purpose for kw in ["construct a new", "new building", "new single", "new duplex", "new dwelling", "new townhouse"]):
        return "new_construction"
    if any(kw in purpose for kw in ["townhouse", "multi-unit", "apartment", "condo", "multiple dwelling", "rental"]):
        return "multi_family"
    if any(kw in purpose for kw in ["secondary suite", "garden suite", "laneway", "carriage", "adu"]):
        return "adu"
    if "heritage" in app_type:
        return "heritage_renovation"
    if any(kw in purpose for kw in ["addition", "extend", "expansion"]):
        return "addition"
    if any(kw in purpose for kw in ["renovation", "alteration", "alter", "convert", "restore"]):
        return "renovation"
    if "rezoning" in app_type:
        return "rezoning"
    return "other"


def score_lead(app: dict) -> tuple[str, str]:
    """Score a lead: (score, reason)."""
    project = app.get("project_type", "other")
    days_old = app.get("days_since_filed", 999)
    is_residential = project in ("new_construction", "renovation", "addition", "adu", "heritage_renovation", "multi_family")

    if not is_residential:
        return "low", f"Non-residential: {project}"
    if project in ("new_construction",) and days_old <= 90:
        return "high", f"New construction, {days_old}d old"
    if project in ("new_construction", "multi_family"):
        return "high", "New construction / multi-family"
    if project in ("renovation", "addition", "heritage_renovation") and days_old <= 90:
        return "high", f"Major work, {days_old}d old"
    if project in ("renovation", "addition", "heritage_renovation"):
        return "medium", "Renovation/addition"
    if project == "adu":
        return "medium", "ADU/suite"
    return "low", project


# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Dev Application Contact Scraper")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of detail pages to scrape (0=all)")
    args = parser.parse_args()

    start = time.time()
    print("Development Application Scraper", flush=True)

    # Step 1: API pull
    print("\n  Step 1: Pulling from ArcGIS API...", flush=True)
    raw_apps = fetch_active_applications()
    print(f"    {len(raw_apps)} active applications", flush=True)

    # Normalize
    apps = []
    for r in raw_apps:
        created = r.get("CREATED_DATE")
        date_filed = None
        days_old = 999
        if created and isinstance(created, (int, float)):
            dt = datetime.fromtimestamp(created / 1000)
            date_filed = dt.strftime("%Y-%m-%d")
            days_old = (datetime.now() - dt).days

        house = str(r.get("HOUSE", "") or "").strip()
        street = str(r.get("STREET", "") or "").strip()

        apps.append({
            "folder_number": str(r.get("FOLDER_NUMBER", "") or ""),
            "app_type": str(r.get("AppType", "") or ""),
            "address": f"{house} {street}".strip(),
            "neighbourhood": str(r.get("Neighbourhood", "") or ""),
            "status": str(r.get("STATUS", "") or ""),
            "purpose": str(r.get("PURPOSE", "") or ""),
            "date_filed": date_filed,
            "days_since_filed": days_old,
            "pid": str(r.get("PID", "") or ""),
            "folio": str(r.get("FOLIO", "") or ""),
            "city_contact": str(r.get("CityContact", "") or ""),
        })

    # Step 2: Scrape detail pages
    folder_numbers = [a["folder_number"] for a in apps if a["folder_number"]]
    print(f"\n  Step 2: Scraping {len(folder_numbers)} detail pages (est. {len(folder_numbers) * 3}s)...", flush=True)
    scraped = await scrape_detail_pages(folder_numbers, limit=args.limit)
    print(f"    Scraped {len(scraped)} pages", flush=True)

    # Step 3: Merge
    print("\n  Step 3: Merging and classifying...", flush=True)
    enriched = []
    for app in apps:
        fn = app["folder_number"]
        detail = scraped.get(fn, {})

        merged = {
            **app,
            "applicant_name": detail.get("applicant_name", ""),
            "applicant_phone": detail.get("applicant_phone", ""),
            "applicant_email": detail.get("applicant_email", ""),
            "purpose_full": detail.get("purpose_full", app["purpose"]),
        }

        merged["project_type"] = classify_project(merged)
        score, reason = score_lead(merged)
        merged["lead_score"] = score
        merged["lead_reason"] = reason
        merged["has_applicant"] = merged["applicant_name"] not in ("", "N/A", "NOT FOUND")

        enriched.append(merged)

    # Sort: high first, then by date
    score_order = {"high": 0, "medium": 1, "low": 2}
    enriched.sort(key=lambda x: (score_order.get(x["lead_score"], 3), -x.get("days_since_filed", 0)))

    # Stats
    high = sum(1 for l in enriched if l["lead_score"] == "high")
    med = sum(1 for l in enriched if l["lead_score"] == "medium")
    low = sum(1 for l in enriched if l["lead_score"] == "low")
    with_contact = sum(1 for l in enriched if l["has_applicant"])
    types = Counter(l["project_type"] for l in enriched)

    print(f"\n  Results: {len(enriched)} applications", flush=True)
    print(f"    High: {high} | Medium: {med} | Low: {low}", flush=True)
    print(f"    With applicant contact: {with_contact} ({with_contact*100//max(len(enriched),1)}%)", flush=True)
    print(f"    Types: {dict(types)}", flush=True)

    print(f"\n  Top 10 leads:", flush=True)
    for l in enriched[:10]:
        contact = l["applicant_name"] if l["has_applicant"] else "(no contact)"
        print(f"    [{l['lead_score']}] {l['address']} ({l['neighbourhood']})", flush=True)
        print(f"         {l['project_type']} | {l['date_filed']} | Contact: {contact}", flush=True)

    # Save
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "dev_application_leads_enriched.json"
    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(enriched),
        "high": high,
        "medium": med,
        "low": low,
        "with_applicant_contact": with_contact,
        "project_types": dict(types),
        "leads": enriched,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved: {out_path}", flush=True)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
