"""
Fetches public Meal Train page and writes mealtrain-data.json.
Run by the GitHub Action .github/workflows/mealtrain-sync.yml
"""
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://www.mealtrain.com/trains/6y2kwq/"
OUT = Path(__file__).parent.parent / "mealtrain-data.json"
KNOWN_DEPOSITS = [16.28, 34.70]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; recovery-dashboard/1.0; +https://github.com/irvinginsights/irving-agents)"}


def fetch_html():
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    data = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raised": None,
        "goal": None,
        "donors_count": None,
        "all_booked": None,
        "open_slots": None,
        "donors": [],
        "meals": [],
        "known_deposits": KNOWN_DEPOSITS,
    }

    # Raised amount — "$406 raised" or "raised $406"
    m = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)\s+raised', text, re.I)
    if not m:
        m = re.search(r'raised\s+\$\s*([\d,]+(?:\.\d{2})?)', text, re.I)
    if m:
        data["raised"] = float(m.group(1).replace(",", ""))

    # Goal
    m = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)\s+goal', text, re.I)
    if not m:
        m = re.search(r'goal\s+of\s+\$\s*([\d,]+(?:\.\d{2})?)', text, re.I)
    if m:
        data["goal"] = float(m.group(1).replace(",", ""))

    # Donor count
    m = re.search(r'(\d+)\s+(?:people|donors?)\s+(?:donated|have given|raised)', text, re.I)
    if not m:
        m = re.search(r'(\d+)\s+donors?', text, re.I)
    if m:
        data["donors_count"] = int(m.group(1))

    # Booked status
    data["all_booked"] = bool(re.search(r'all dates? are booked|no (?:open )?slots', text, re.I))

    # Open slot count — "3 open dates" or "3 slots available"
    m = re.search(r'(\d+)\s+open\s+(?:dates?|slots?)', text, re.I)
    if m:
        data["open_slots"] = int(m.group(1))
    elif data["all_booked"]:
        data["open_slots"] = 0

    # Donors — look for donation entries in list items or paragraphs
    donors = []
    for tag in soup.find_all(["li", "div", "p"]):
        t = tag.get_text(" ", strip=True)
        m = re.match(r'^(.+?)\s+(?:donated|gave)?\s*\$\s*([\d,]+(?:\.\d{2})?)', t, re.I)
        if m:
            name = m.group(1).strip()
            amount = float(m.group(2).replace(",", ""))
            if 1 <= amount <= 10000 and len(name) <= 60:
                donors.append({"name": name, "amount": amount})
    if donors:
        data["donors"] = donors[:20]  # cap at 20

    return data


def main():
    try:
        html = fetch_html()
    except Exception as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    data = parse(html)

    # Preserve known_deposits from existing file if present
    if OUT.exists():
        try:
            existing = json.loads(OUT.read_text())
            if existing.get("known_deposits"):
                data["known_deposits"] = existing["known_deposits"]
        except Exception:
            pass

    OUT.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {OUT}: raised={data['raised']}, slots={data['open_slots']}, donors={len(data['donors'])}")


if __name__ == "__main__":
    main()
