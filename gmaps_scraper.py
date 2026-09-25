"""
Google Maps Lead Scraper — Playwright
Extracts business listings from Google Maps searches,
visits homepages to scrape contact info, then outputs leads.csv
"""

import asyncio
import csv
import random
import re
import time
import logging
from dataclasses import dataclass, field, fields, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
SEARCH_QUERIES: list[str] = [
            "cleaning services uae",
            "home cleaning uae",
            "residential cleaning uae",
            "deep cleaning uae",
            "cleaning company dubai",
            "sofa cleaning uae",
            "carpet cleaning uae"
]

OUTPUT_FILE = "leads.csv"
DELAY_MIN = 1.0          # seconds between actions
DELAY_MAX = 3.0
MAX_SCROLL_ATTEMPTS = 40  # safety cap on scroll loop
WEBSITE_TIMEOUT = 15_000  # ms
MAPS_TIMEOUT = 20_000


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class Lead:
    name: str = ""
    phone: str = ""
    website: str = ""
    address: str = ""
    maps_link: str = ""
    email: str = ""
    site_phone: str = ""
    instagram: str = ""
    whatsapp: str = ""
    facebook: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────
def delay(lo=DELAY_MIN, hi=DELAY_MAX):
    time.sleep(random.uniform(lo, hi))


RE_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I
)
RE_PHONE = re.compile(
    r"(?:\+?\d[\d\s\-().]{6,}\d)", re.I
)
RE_WA = re.compile(
    r"(?:wa\.me/|whatsapp\.com/send\?phone=|api\.whatsapp\.com/send\?phone=)([\d+]+)", re.I
)
RE_IG = re.compile(
    r"instagram\.com/(?!p/|reel/|stories/|explore/)([A-Za-z0-9_.]+)", re.I
)
RE_FB = re.compile(
    r"facebook\.com/(?!sharer|share|dialog|plugins)([A-Za-z0-9_.]+)", re.I
)

JUNK_EMAILS = {"example", "domain", "email", "user", "info@sentry", "noreply", "no-reply"}
JUNK_IG = {"p", "reel", "stories", "explore", "share", "sharer", "login", "accounts"}


def clean_email(m: str) -> Optional[str]:
    lower = m.lower()
    if any(j in lower for j in JUNK_EMAILS):
        return None
    return lower


def clean_ig(m: str) -> Optional[str]:
    if m.lower() in JUNK_IG or len(m) < 2:
        return None
    return f"https://instagram.com/{m}"


def clean_fb(m: str) -> Optional[str]:
    junk = {"sharer", "share", "dialog", "plugins", "login", "pages", "pg", "groups"}
    if m.lower() in junk or len(m) < 2:
        return None
    return f"https://facebook.com/{m}"


def extract_from_html(html: str, base_url: str) -> dict:
    result = {}

    emails = [e for e in (clean_email(m) for m in RE_EMAIL.findall(html)) if e]
    if emails:
        result["email"] = emails[0]

    phones = RE_PHONE.findall(html)
    if phones:
        result["site_phone"] = phones[0].strip()

    wa = RE_WA.search(html)
    if wa:
        result["whatsapp"] = f"https://wa.me/{wa.group(1)}"

    ig_matches = [clean_ig(m) for m in RE_IG.findall(html)]
    ig_matches = [x for x in ig_matches if x]
    if ig_matches:
        result["instagram"] = ig_matches[0]

    fb_matches = [clean_fb(m) for m in RE_FB.findall(html)]
    fb_matches = [x for x in fb_matches if x]
    if fb_matches:
        result["facebook"] = fb_matches[0]

    return result


def passes_filter(lead: Lead) -> bool:
    return any([lead.email, lead.phone or lead.site_phone, lead.instagram, lead.whatsapp])


def dedup(leads: list[Lead]) -> list[Lead]:
    seen_names: set[str] = set()
    seen_sites: set[str] = set()
    unique = []
    for lead in leads:
        norm_name = lead.name.lower().strip()
        norm_site = lead.website.lower().strip().rstrip("/")
        if norm_name in seen_names:
            continue
        if norm_site and norm_site in seen_sites:
            continue
        seen_names.add(norm_name)
        if norm_site:
            seen_sites.add(norm_site)
        unique.append(lead)
    return unique


# ── Maps scraping ─────────────────────────────────────────────────────────────
async def scrape_maps_query(context: BrowserContext, query: str) -> list[Lead]:
    log.info(f"🔍 Searching: {query}")
    page = await context.new_page()
    leads: list[Lead] = []

    try:
        url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        await page.goto(url, timeout=MAPS_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Accept cookies if prompted
        try:
            accept = page.locator('button:has-text("Accept all"), button:has-text("Reject all")')
            if await accept.first.is_visible(timeout=3000):
                await accept.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        # Locate results panel
        results_panel = page.locator('div[role="feed"]')
        try:
            await results_panel.wait_for(timeout=MAPS_TIMEOUT)
        except PWTimeout:
            log.warning("Results panel not found — skipping query")
            return leads

        # Scroll until no new items
        prev_count = 0
        stall_count = 0
        for attempt in range(MAX_SCROLL_ATTEMPTS):
            items = await page.locator('div[role="feed"] > div > div[jsaction]').all()
            curr_count = len(items)
            log.info(f"  Scroll {attempt+1}: {curr_count} listings visible")

            # Check for "end of list" marker
            end_marker = page.locator('text="You\'ve reached the end of the list"')
            if await end_marker.is_visible(timeout=500):
                log.info("  Reached end of results.")
                break

            if curr_count == prev_count:
                stall_count += 1
                if stall_count >= 3:
                    log.info("  No new listings — stopping scroll.")
                    break
            else:
                stall_count = 0

            prev_count = curr_count

            # Scroll the panel
            await results_panel.evaluate("el => el.scrollBy(0, 1000)")
            await page.wait_for_timeout(int(random.uniform(1200, 2200)))

        # Collect listing URLs
        listing_links = await page.locator('div[role="feed"] a[href*="/maps/place/"]').all()
        hrefs = []
        for link in listing_links:
            href = await link.get_attribute("href")
            if href and href not in hrefs:
                hrefs.append(href)

        log.info(f"  Found {len(hrefs)} unique listing URLs")

        # Visit each listing
        for i, href in enumerate(hrefs, 1):
            lead = await scrape_listing(context, href, i, len(hrefs))
            if lead:
                leads.append(lead)
            delay()

    except Exception as e:
        log.error(f"Error scraping query '{query}': {e}")
    finally:
        await page.close()

    return leads


async def scrape_listing(context: BrowserContext, href: str, idx: int, total: int) -> Optional[Lead]:
    page = await context.new_page()
    lead = Lead()
    try:
        full_url = href if href.startswith("http") else f"https://www.google.com{href}"
        lead.maps_link = full_url
        await page.goto(full_url, timeout=MAPS_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # Name
        try:
            name_el = page.locator('h1.DUwDvf, h1[class*="fontHeadlineLarge"]').first
            lead.name = (await name_el.inner_text(timeout=5000)).strip()
        except Exception:
            lead.name = ""

        if not lead.name:
            log.debug(f"  [{idx}/{total}] No name found, skipping listing")
            return None

        log.info(f"  [{idx}/{total}] {lead.name}")

        # Address
        try:
            addr = page.locator('button[data-item-id="address"]').first
            lead.address = (await addr.inner_text(timeout=3000)).strip()
        except Exception:
            pass

        # Phone
        try:
            phone_btn = page.locator('button[data-item-id*="phone:tel"]').first
            if await phone_btn.is_visible(timeout=2000):
                lead.phone = (await phone_btn.get_attribute("aria-label") or "").replace("Phone:", "").strip()
        except Exception:
            pass

        # Website
        try:
            web_btn = page.locator('a[data-item-id="authority"]').first
            if await web_btn.is_visible(timeout=2000):
                lead.website = (await web_btn.get_attribute("href") or "").strip()
        except Exception:
            pass

    except Exception as e:
        log.warning(f"  Error scraping listing {href}: {e}")
    finally:
        await page.close()

    return lead


# ── Website scraping ──────────────────────────────────────────────────────────
async def scrape_website(context: BrowserContext, lead: Lead) -> Lead:
    if not lead.website:
        return lead

    # Normalise URL
    url = lead.website
    if not url.startswith("http"):
        url = "https://" + url

    # Only homepage
    parsed = urlparse(url)
    homepage = f"{parsed.scheme}://{parsed.netloc}/"

    page = await context.new_page()
    try:
        await page.goto(homepage, timeout=WEBSITE_TIMEOUT, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        html = await page.content()
        data = extract_from_html(html, homepage)
        for k, v in data.items():
            setattr(lead, k, v)
        log.info(f"    🌐 {lead.name}: email={bool(lead.email)} ig={bool(lead.instagram)} wa={bool(lead.whatsapp)}")
    except Exception as e:
        log.debug(f"    Website error for {lead.website}: {e}")
    finally:
        await page.close()

    return lead


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    all_leads: list[Lead] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        # ── Step 1: Scrape Maps ──────────────────────────────────────────────
        for query in SEARCH_QUERIES:
            leads = await scrape_maps_query(context, query)
            all_leads.extend(leads)
            delay(2, 4)

        log.info(f"\n📦 Total raw listings: {len(all_leads)}")

        # ── Step 2: Dedup before website visits ─────────────────────────────
        all_leads = dedup(all_leads)
        log.info(f"📦 After dedup: {len(all_leads)}")

        # ── Step 3: Visit websites ───────────────────────────────────────────
        log.info("🌐 Visiting websites…")
        for i, lead in enumerate(all_leads, 1):
            if lead.website:
                all_leads[i - 1] = await scrape_website(context, lead)
                delay()

        await browser.close()

    # ── Step 4: Filter ────────────────────────────────────────────────────────
    filtered = [l for l in all_leads if passes_filter(l)]
    log.info(f"✅ Leads passing filter: {len(filtered)} / {len(all_leads)}")

    # ── Step 5: Final dedup ───────────────────────────────────────────────────
    filtered = dedup(filtered)
    log.info(f"✅ After final dedup: {len(filtered)}")

    # ── Step 6: Write CSV ─────────────────────────────────────────────────────
    if not filtered:
        log.warning("No leads to export.")
        return

    col_names = [f.name for f in fields(Lead)]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=col_names)
        writer.writeheader()
        writer.writerows(asdict(l) for l in filtered)

    log.info(f"💾 Saved {len(filtered)} leads → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
