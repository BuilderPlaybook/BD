"""
Development Application Monitor — early-stage lead generation.

Pulls development permit applications from the City of Victoria's ArcGIS
REST API. These happen BEFORE building permits — the earliest public signal
that someone is planning a construction project. At this stage, the homeowner
has a design but may not have committed to a builder.

Standalone version — no database, no external services. Outputs a JSON
file to ./output/dev_application_leads.json.

Usage:
    python dev_application_monitor.py
"""

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
# Data source configs — extensible per municipality
# ═══════════════════════════════════════════════════════

DEV_APP_SOURCES = {
    "victoria": {
        "name": "City of Victoria",
        "type": "arcgis_rest",
        "base_url": "https://maps.victoria.ca/server/rest/services/OpenData/OpenData_PlanningAndDevelopment/MapServer",
        "active_layer": 3,
        "history_table": 18,
        "relevant_app_types": [
            "Development Permit",
            "Dev Permit with Variance",
            "Development Variance Permit",
            "Delegated Development Permit",
            "Delegated Development Variance Permit",
            "Heritage Alteration Permit",
            "Delegated Heritage Permit",
            "Rezoning",
        ],
        "exclude_app_types": [
            "Tax Incentive Permit",
            "Heritage Designation",
            "Temporary Use Permit",
        ],
        "high_value_neighbourhoods": [
            "FAIRFIELD", "ROCKLAND", "GONZALES", "JAMES BAY",
            "OAKLANDS", "FERNWOOD", "VICTORIA WEST",
        ],
    },
}


# ═══════════════════════════════════════════════════════
# ArcGIS fetcher
# ═══════════════════════════════════════════════════════

async def fetch_dev_applications(source: dict) -> list[dict]:
    """Fetch all active development applications from ArcGIS REST API."""
    url = f"{source['base_url']}/{source['active_layer']}/query"
    all_records = []
    offset = 0
    batch = 200

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "where": "1=1",
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": batch,
                "resultOffset": offset,
            }
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"    Error at offset {offset}: {e}")
                break

            features = data.get("features", [])
            if not features:
                break
            for f in features:
                all_records.append(f.get("attributes", {}))
            if len(features) < batch:
                break
            offset += batch

    return all_records


# ═══════════════════════════════════════════════════════
# Normalize and classify
# ═══════════════════════════════════════════════════════

def normalize_victoria_dev_app(raw: dict) -> dict:
    """Convert Victoria ArcGIS dev application to standard format."""
    # Parse date (epoch ms)
    created_raw = raw.get("CREATED_DATE")
    created = None
    if created_raw:
        if isinstance(created_raw, (int, float)):
            created = datetime.fromtimestamp(created_raw / 1000).strftime("%Y-%m-%d")
        elif isinstance(created_raw, str) and created_raw.strip():
            created = created_raw[:10]

    # Build full purpose from Purpose1-Purpose10
    purpose_parts = []
    for i in range(1, 11):
        p = raw.get(f"Purpose{i}", "")
        if p:
            purpose_parts.append(str(p))
    full_purpose = "".join(purpose_parts).replace("<br>", " ").replace("<br/>", " ").strip()
    if not full_purpose:
        full_purpose = str(raw.get("PURPOSE", "") or "")

    # Address
    house = str(raw.get("HOUSE", "") or "").strip()
    street = str(raw.get("STREET", "") or "").strip()
    unit = str(raw.get("UNIT", "") or "").strip()
    address = f"{house} {street}".strip()
    if unit:
        address = f"{unit}-{address}"

    return {
        "source": "victoria",
        "folder_number": str(raw.get("FOLDER_NUMBER", "") or ""),
        "app_type": str(raw.get("AppType", "") or ""),
        "address": address,
        "neighbourhood": str(raw.get("Neighbourhood", "") or ""),
        "status": str(raw.get("STATUS", "") or ""),
        "purpose": full_purpose,
        "subject": str(raw.get("SUBJECT", "") or ""),
        "date_filed": created,
        "pid": str(raw.get("PID", "") or ""),
        "folio": str(raw.get("FOLIO", "") or ""),
        "city_contact": str(raw.get("CityContact", "") or ""),
        "city_email": str(raw.get("email", "") or ""),
        "city_phone": str(raw.get("phone", "") or ""),
        "tracker_id": str(raw.get("DevAppTracker", "") or ""),
    }


def classify_project_type(app: dict) -> str:
    """Classify development application into project type."""
    purpose = app.get("purpose", "").lower()
    app_type = app.get("app_type", "").lower()

    # New construction signals
    if any(kw in purpose for kw in ["construct a new", "new building", "new single", "new duplex",
                                      "new dwelling", "new townhouse", "new residential"]):
        return "new_construction"

    # Multi-family / townhouse
    if any(kw in purpose for kw in ["townhouse", "multi-unit", "multi-family", "apartment",
                                      "condo", "multiple dwelling", "rental"]):
        return "multi_family"

    # ADU / secondary suite
    if any(kw in purpose for kw in ["secondary suite", "garden suite", "laneway", "carriage",
                                      "accessory dwelling", "adu", "backyard home"]):
        return "adu"

    # Heritage renovation
    if "heritage" in app_type.lower():
        return "heritage_renovation"

    # Addition
    if any(kw in purpose for kw in ["addition", "extend", "expansion", "bump-out", "second storey"]):
        return "addition"

    # Renovation
    if any(kw in purpose for kw in ["renovation", "renovate", "alteration", "alter", "remodel",
                                      "restore", "restoration", "convert"]):
        return "renovation"

    # Subdivision / rezoning for residential
    if "rezoning" in app_type.lower() or "subdivision" in purpose:
        if any(kw in purpose for kw in ["residential", "dwelling", "house", "home", "townhouse"]):
            return "rezoning_residential"
        return "rezoning_other"

    return "other"


def is_residential(app: dict) -> bool:
    """Check if application is residential (not purely commercial/institutional)."""
    purpose = app.get("purpose", "").lower()

    commercial_only = ["commercial", "office", "retail", "industrial", "institutional",
                       "church", "school", "hospital", "government", "signage", "sign",
                       "barbed wire", "fence only", "parking lot"]

    # If purpose is purely commercial, exclude
    if any(kw in purpose for kw in commercial_only) and not any(
        kw in purpose for kw in ["residential", "dwelling", "house", "home", "suite", "townhouse"]):
        return False

    return True


def mentions_builder(app: dict) -> bool:
    """Check if the application mentions a builder/contractor."""
    purpose = app.get("purpose", "").lower()
    builder_signals = ["contractor", "builder", "construction company", "general contractor",
                       "building contractor", "homes ltd", "construction ltd"]
    return any(s in purpose for s in builder_signals)


# ═══════════════════════════════════════════════════════
# Scoring
# ═══════════════════════════════════════════════════════

def score_dev_lead(app: dict, source_cfg: dict) -> dict:
    """Score a development application as a potential lead."""
    project_type = classify_project_type(app)
    is_res = is_residential(app)
    has_builder = mentions_builder(app)
    neighbourhood = app.get("neighbourhood", "").upper()
    high_value_hood = neighbourhood in source_cfg.get("high_value_neighbourhoods", [])

    # Days since filing
    days_old = 999
    if app.get("date_filed"):
        try:
            filed = datetime.strptime(app["date_filed"], "%Y-%m-%d")
            days_old = (datetime.now() - filed).days
        except ValueError:
            pass

    # Scoring
    if not is_res:
        score = "exclude"
        reason = "Non-residential application"
    elif has_builder:
        score = "low"
        reason = "Builder/contractor already mentioned in application"
    elif project_type in ("new_construction", "multi_family") and days_old <= 60:
        score = "high"
        reason = f"New construction, filed {days_old}d ago, no builder mentioned"
    elif project_type in ("new_construction", "multi_family"):
        score = "high"
        reason = f"New construction, no builder mentioned"
    elif project_type in ("renovation", "addition", "heritage_renovation") and days_old <= 60:
        score = "high"
        reason = f"Major renovation/addition, filed {days_old}d ago, no builder"
    elif project_type in ("renovation", "addition", "heritage_renovation"):
        score = "medium"
        reason = f"Renovation/addition, no builder mentioned"
    elif project_type == "adu" and days_old <= 90:
        score = "medium"
        reason = f"ADU/suite, filed {days_old}d ago"
    elif project_type == "rezoning_residential":
        score = "medium"
        reason = "Residential rezoning — project in early planning"
    elif project_type == "adu":
        score = "low"
        reason = "ADU/suite, older application"
    else:
        score = "low"
        reason = f"Other: {project_type}"

    # Boost for high-value neighbourhoods
    if score == "medium" and high_value_hood:
        score = "high"
        reason += f" + premium neighbourhood ({neighbourhood.title()})"

    return {
        **app,
        "project_type": project_type,
        "is_residential": is_res,
        "has_builder_mentioned": has_builder,
        "days_since_filed": days_old,
        "lead_score": score,
        "lead_reason": reason,
    }


# ═══════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════

async def run_dev_monitor():
    start = time.time()
    print("Development Application Monitor")

    all_leads = []

    for source_key, source_cfg in DEV_APP_SOURCES.items():
        print(f"\n  [{source_cfg['name']}] Fetching active development applications...")
        raw = await fetch_dev_applications(source_cfg)
        print(f"    Raw applications: {len(raw)}")

        # Normalize
        normalized = [normalize_victoria_dev_app(r) for r in raw]

        # Filter to relevant app types
        relevant_types = set(source_cfg.get("relevant_app_types", []))
        if relevant_types:
            normalized = [a for a in normalized if a["app_type"] in relevant_types]
            print(f"    After type filter: {len(normalized)}")

        # Score
        scored = [score_dev_lead(a, source_cfg) for a in normalized]

        # Exclude non-residential
        scored = [s for s in scored if s["lead_score"] != "exclude"]
        print(f"    After residential filter: {len(scored)}")

        all_leads.extend(scored)

    # Sort by score then recency
    score_order = {"high": 0, "medium": 1, "low": 2}
    all_leads.sort(key=lambda x: (score_order.get(x["lead_score"], 3), x.get("days_since_filed", 999)))

    # Stats
    high = sum(1 for l in all_leads if l["lead_score"] == "high")
    med = sum(1 for l in all_leads if l["lead_score"] == "medium")
    low = sum(1 for l in all_leads if l["lead_score"] == "low")

    types = Counter(l["project_type"] for l in all_leads)
    hoods = Counter(l["neighbourhood"] for l in all_leads)

    recent_90 = sum(1 for l in all_leads if l.get("days_since_filed", 999) <= 90)

    print(f"\n  Results: {len(all_leads)} residential leads")
    print(f"    High: {high} | Medium: {med} | Low: {low}")
    print(f"    Filed in last 90 days: {recent_90}")
    print(f"\n  Project types:")
    for t, c in types.most_common():
        print(f"    {t}: {c}")
    print(f"\n  Neighbourhoods:")
    for h, c in hoods.most_common():
        print(f"    {h}: {c}")

    # Top leads
    print(f"\n  Top 10 leads:")
    for l in all_leads[:10]:
        print(f"    [{l['lead_score']}] {l['address']} ({l['neighbourhood']})")
        print(f"         {l['app_type']} | {l['project_type']} | Filed: {l['date_filed']} ({l['days_since_filed']}d ago)")
        print(f"         {l['purpose'][:100]}")
        print()

    # Save
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "dev_application_leads.json"
    output = {
        "generated_at": datetime.now().isoformat(),
        "total_leads": len(all_leads),
        "high_leads": high,
        "medium_leads": med,
        "low_leads": low,
        "recent_90_days": recent_90,
        "project_type_breakdown": dict(types),
        "neighbourhood_breakdown": dict(hoods),
        "leads": all_leads,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved: {out_path}")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s")
    return output


if __name__ == "__main__":
    asyncio.run(run_dev_monitor())
