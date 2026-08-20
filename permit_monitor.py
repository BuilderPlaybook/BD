"""
Building Permit Monitor — lead generation from municipal open data.

Pulls building permits from the City of Victoria's open-data ArcGIS REST
API, filters for residential projects without a premium builder of record,
and scores leads for outreach.

Standalone version — no database, no external services. Outputs a JSON
file to ./output/permit_leads.json.

Usage:
    python permit_monitor.py
    python permit_monitor.py --lookback 1_year
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
# Data source registry — extensible per municipality
# ═══════════════════════════════════════════════════════

PERMIT_SOURCES = {
    "victoria": {
        "name": "City of Victoria",
        "type": "arcgis_rest",
        "base_url": "https://maps.victoria.ca/server/rest/services/OpenData/OpenData_PermitsAndLicences/MapServer",
        "layers": {
            "60_days": 4,
            "1_year": 3,
        },
        "relevant_permit_types": [
            "Building Permit (BP)",
        ],
        "exclude_permit_types": [
            "Electrical Permit",
            "Plumbing Permit",
            "Sign Permit",
        ],
    },
}

# Known premium builders in Greater Victoria (excluded / down-scored as leads).
# Includes "lida homes" so your own permits don't show up as leads.
# Edit this list to suit your market.
KNOWN_PREMIUM_BUILDERS = [
    "lida homes", "lida", "coast prestige", "lineal homes",
    "verity construction", "westhills", "abstract developments",
    "aryze", "citta", "bosa", "concert properties",
    "jawl residential", "zebra design", "pilot homes",
    "campbell construction", "kinetic construction",
]


# ═══════════════════════════════════════════════════════
# ArcGIS REST API fetcher
# ═══════════════════════════════════════════════════════

async def fetch_arcgis_permits(source: dict, lookback: str = "60_days") -> list[dict]:
    """Fetch permits from an ArcGIS REST API endpoint."""
    layer_id = source["layers"].get(lookback, source["layers"]["60_days"])
    url = f"{source['base_url']}/{layer_id}/query"

    all_records = []
    offset = 0
    batch_size = 200

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "where": "1=1",
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json",
                "resultRecordCount": batch_size,
                "resultOffset": offset,
            }
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"  Error at offset {offset}: {e}")
                break

            features = data.get("features", [])
            if not features:
                break

            for f in features:
                attrs = f.get("attributes", {})
                all_records.append(attrs)

            if len(features) < batch_size:
                break
            offset += batch_size

    return all_records


# ═══════════════════════════════════════════════════════
# Normalize permits to standard format
# ═══════════════════════════════════════════════════════

def normalize_victoria_permit(raw: dict) -> dict:
    """Convert Victoria ArcGIS permit record to standard format."""
    # Parse date — may be epoch ms (int) or ISO string
    issued_raw = raw.get("IssuedDate")
    issued = None
    if issued_raw:
        if isinstance(issued_raw, (int, float)):
            issued = datetime.fromtimestamp(issued_raw / 1000).strftime("%Y-%m-%d")
        elif isinstance(issued_raw, str) and issued_raw.strip():
            try:
                issued = datetime.fromisoformat(issued_raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except ValueError:
                issued = issued_raw[:10]  # fallback: take first 10 chars

    # Build address
    house = str(raw.get("House", "") or "").strip()
    unit = str(raw.get("Unit", "") or "").strip()
    street = str(raw.get("Street", "") or "").strip()
    address = f"{house} {street}".strip()
    if unit:
        address = f"{unit}-{address}"

    # Contractor/builder name
    builder = str(raw.get("Name", "") or "").strip()

    # Value — may be string with commas or dollar signs
    raw_val = raw.get("BldgValue", 0) or 0
    try:
        if isinstance(raw_val, str):
            raw_val = raw_val.replace("$", "").replace(",", "").strip()
        value = float(raw_val)
    except (ValueError, TypeError):
        value = 0

    return {
        "source": "victoria",
        "permit_number": str(raw.get("OBJECTID", "")),
        "permit_type": str(raw.get("PermitType", "") or ""),
        "description": str(raw.get("Purpose", "") or ""),
        "subject": str(raw.get("SUBJECT", "") or ""),
        "address": address,
        "neighbourhood": str(raw.get("Neighbourhood", "") or ""),
        "estimated_value": value,
        "date_issued": issued,
        "builder_name": builder,
        "builder_phone": str(raw.get("phone", "") or "").strip(),
        "builder_cell": str(raw.get("cell", "") or "").strip(),
        "builder_email": str(raw.get("email", "") or "").strip(),
        "actual_use": str(raw.get("ActualUse", "") or ""),
        "lat": raw.get("Y_LAT"),
        "lon": raw.get("X_LONG"),
    }


# ═══════════════════════════════════════════════════════
# Filter and score permits
# ═══════════════════════════════════════════════════════

def is_relevant_permit(permit: dict, source_cfg: dict) -> bool:
    """Check if permit is relevant (residential, significant value)."""
    pt = permit["permit_type"]

    # Exclude by prefix
    for excl in source_cfg.get("exclude_permit_types", []):
        if excl.endswith("-") and pt.startswith(excl):
            return False
        if pt == excl:
            return False

    # Include only relevant types
    relevant = source_cfg.get("relevant_permit_types", [])
    if relevant and pt not in relevant:
        return False

    return True


def classify_work_type(permit: dict) -> str:
    """Classify permit into work type categories."""
    desc = (permit.get("description", "") + " " + permit.get("subject", "")).lower()
    pt = permit.get("permit_type", "")

    if "new" in desc and ("construct" in desc or "build" in desc or "house" in desc or "dwell" in desc):
        return "new_construction"
    if "sfd" in pt.lower() or "single family" in desc:
        if "addition" in desc or "alter" in desc:
            return "addition"
        return "new_construction"
    if "addition" in desc or "add" in desc:
        return "addition"
    if "suite" in desc or "adu" in desc or "secondary" in desc or "carriage" in desc or "laneway" in desc:
        return "adu"
    if "renovat" in desc or "remodel" in desc or "interior" in desc:
        return "renovation"
    if "complex" in pt.lower():
        return "complex"
    return "other"


def is_known_premium_builder(name: str) -> bool:
    """Check if builder is a known premium/large firm."""
    if not name:
        return False
    name_lower = name.lower().strip()
    return any(kb in name_lower for kb in KNOWN_PREMIUM_BUILDERS)


def score_lead(permit: dict) -> dict:
    """Score a permit as a potential lead."""
    builder = permit.get("builder_name", "").strip()
    value = permit.get("estimated_value", 0)
    work_type = classify_work_type(permit)

    has_builder = bool(builder)
    is_premium = is_known_premium_builder(builder)

    # Score: high = no builder, medium = small/unknown builder, low = premium builder
    if is_premium:
        score = "low"
        reason = f"Premium builder: {builder}"
    elif not has_builder and value >= 200000:
        score = "high"
        reason = "No builder of record on high-value project"
    elif not has_builder:
        score = "high"
        reason = "No builder of record"
    elif has_builder and not is_premium and value >= 200000:
        score = "medium"
        reason = f"Small/unknown builder ({builder}) on high-value project"
    elif has_builder and not is_premium:
        score = "medium"
        reason = f"Small/unknown builder: {builder}"
    else:
        score = "medium"
        reason = "Unknown builder status"

    return {
        **permit,
        "work_type": work_type,
        "has_builder": has_builder,
        "is_premium_builder": is_premium,
        "lead_score": score,
        "lead_reason": reason,
    }


# ═══════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════

async def run_permit_monitor(lookback: str = "60_days"):
    start = time.time()
    print("Permit Monitor")
    print(f"Lookback: {lookback}")

    all_leads = []

    for source_key, source_cfg in PERMIT_SOURCES.items():
        if source_cfg.get("status", "").startswith("planned"):
            print(f"\n  [{source_cfg['name']}] Skipped — {source_cfg['status']}")
            continue

        if source_cfg["type"] == "arcgis_rest":
            print(f"\n  [{source_cfg['name']}] Fetching via ArcGIS REST API...")
            raw_permits = await fetch_arcgis_permits(source_cfg, lookback)
            print(f"    Raw permits: {len(raw_permits)}")

            # Normalize
            normalized = [normalize_victoria_permit(r) for r in raw_permits]

            # Filter
            relevant = [p for p in normalized if is_relevant_permit(p, source_cfg)]
            print(f"    After filtering: {len(relevant)} relevant permits")

            # Score
            scored = [score_lead(p) for p in relevant]
            all_leads.extend(scored)

    # Sort by score then value
    score_order = {"high": 0, "medium": 1, "low": 2}
    all_leads.sort(key=lambda x: (score_order.get(x["lead_score"], 3), -x["estimated_value"]))

    # Stats
    high = sum(1 for l in all_leads if l["lead_score"] == "high")
    med = sum(1 for l in all_leads if l["lead_score"] == "medium")
    low = sum(1 for l in all_leads if l["lead_score"] == "low")

    print(f"\n  Results: {len(all_leads)} leads")
    print(f"    High: {high} | Medium: {med} | Low: {low}")

    # Work type breakdown
    types = Counter(l["work_type"] for l in all_leads)
    print(f"    Types: {dict(types)}")

    # Show top leads
    print(f"\n  Top leads:")
    for l in all_leads[:10]:
        print(f"    [{l['lead_score']}] ${l['estimated_value']:,.0f} — {l['address']} ({l['neighbourhood']})")
        print(f"         {l['work_type']} | Builder: {l['builder_name'] or '(none)'} | {l['date_issued']}")

    # Save
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "permit_leads.json"
    output = {
        "generated_at": datetime.utcnow().isoformat(),
        "lookback": lookback,
        "sources_checked": list(PERMIT_SOURCES.keys()),
        "total_leads": len(all_leads),
        "high_leads": high,
        "medium_leads": med,
        "low_leads": low,
        "work_type_breakdown": dict(types),
        "leads": all_leads,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved: {out_path}")

    # Competitor intelligence: who's pulling permits?
    builders = Counter(l["builder_name"] for l in all_leads if l["builder_name"])
    if builders:
        print(f"\n  Active builders in market:")
        for builder, count in builders.most_common(15):
            total_val = sum(l["estimated_value"] for l in all_leads if l["builder_name"] == builder)
            print(f"    {builder}: {count} permits, ${total_val:,.0f} total value")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.0f}s")

    return output


async def main():
    parser = argparse.ArgumentParser(description="Building Permit Monitor — lead generation")
    parser.add_argument("--lookback", default="60_days", choices=["60_days", "1_year"])
    args = parser.parse_args()

    await run_permit_monitor(args.lookback)


if __name__ == "__main__":
    asyncio.run(main())
