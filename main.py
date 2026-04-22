import asyncio
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
DB_PATH = DATA_DIR / "apps.db"

DATA_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# ── Data from CSVs ────────────────────────────────────────────────────────────

COUNTRIES = [
    ("Albania", "al"), ("Argentina", "ar"), ("Australia", "au"),
    ("Austria", "at"), ("Azerbaijan", "az"), ("Bangladesh", "bd"),
    ("Belgium", "be"), ("Brazil", "br"), ("Bulgaria", "bg"),
    ("Canada", "ca"), ("Chile", "cl"), ("Colombia", "co"),
    ("Croatia", "hr"), ("Czechia", "cz"), ("Denmark", "dk"),
    ("Dominican Republic", "do"), ("Ecuador", "ec"), ("Egypt", "eg"),
    ("Estonia", "ee"), ("Finland", "fi"), ("France", "fr"),
    ("Germany", "de"), ("Ghana", "gh"), ("Greece", "gr"),
    ("Guatemala", "gt"), ("Hong Kong", "hk"), ("Hungary", "hu"),
    ("India", "in"), ("Indonesia", "id"), ("Ireland", "ie"),
    ("Israel", "il"), ("Italy", "it"), ("Japan", "jp"),
    ("Kazakhstan", "kz"), ("Kenya", "ke"), ("Korea", "kr"),
    ("Kuwait", "kw"), ("Latvia", "lv"), ("Lithuania", "lt"),
    ("Malaysia", "my"), ("Malta", "mt"), ("Mexico", "mx"),
    ("Montenegro", "me"), ("Morocco", "ma"), ("Netherlands", "nl"),
    ("New Zealand", "nz"), ("Nigeria", "ng"), ("Norway", "no"),
    ("Pakistan", "pk"), ("Peru", "pe"), ("Philippines", "ph"),
    ("Poland", "pl"), ("Portugal", "pt"), ("Qatar", "qa"),
    ("Romania", "ro"), ("Saudi Arabia", "sa"), ("Serbia", "rs"),
    ("Singapore", "sg"), ("Slovakia", "sk"), ("Slovenia", "si"),
    ("South Africa", "za"), ("Spain", "es"), ("Sri Lanka", "lk"),
    ("Sweden", "se"), ("Switzerland", "ch"), ("Taiwan", "tw"),
    ("Thailand", "th"), ("Tunisia", "tn"), ("Turkey", "tr"),
    ("Ukraine", "ua"), ("United Arab Emirates", "ae"),
    ("Great Britain", "gb"), ("United Kingdom", "uk"),
    ("United States of America", "us"), ("Uzbekistan", "uz"),
    ("Viet Nam", "vn"),
]

LANGUAGES = [
    ("Albanian", "sq"), ("Arabic", "ar"), ("Azerbaijani", "az"),
    ("Bengali", "bn"), ("Bulgarian", "bg"), ("Chinese (Traditional)", "zh"),
    ("Croatian", "hr"), ("Czech", "cs"), ("Danish", "da"),
    ("Dutch", "nl"), ("English", "en"), ("Estonian", "et"),
    ("Filipino (Tagalog)", "tl"), ("Finnish", "fi"), ("French", "fr"),
    ("German", "de"), ("Greek", "el"), ("Hebrew", "he"),
    ("Hindi", "hi"), ("Hungarian", "hu"), ("Indonesian", "id"),
    ("Italian", "it"), ("Japanese", "ja"), ("Kazakh", "kk"),
    ("Korean", "ko"), ("Latvian", "lv"), ("Lithuanian", "lt"),
    ("Malay", "ms"), ("Maltese", "mt"), ("Norwegian", "no"),
    ("Polish", "pl"), ("Portuguese", "pt"), ("Romanian", "ro"),
    ("Serbian", "sr"), ("Sinhala", "si"), ("Slovak", "sk"),
    ("Slovenian", "sl"), ("Spanish", "es"), ("Swedish", "sv"),
    ("Thai", "th"), ("Turkish", "tr"), ("Ukrainian", "uk"),
    ("Urdu", "ur"), ("Uzbek", "uz"), ("Vietnamese", "vi"),
    ("Zulu", "zu"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id        TEXT PRIMARY KEY,
            type      TEXT NOT NULL,
            input_url TEXT NOT NULL,
            status    TEXT DEFAULT 'running',
            phase     TEXT DEFAULT '',
            progress  INTEGER DEFAULT 0,
            total     INTEGER DEFAULT 0,
            message   TEXT DEFAULT '',
            result_app_ids TEXT DEFAULT '[]',
            error     TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS apps (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            package_name TEXT NOT NULL UNIQUE,
            original_url TEXT NOT NULL,
            checked_at   TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS designs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id          INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
            scan_type       TEXT NOT NULL,
            app_name        TEXT NOT NULL,
            screenshot_path TEXT,
            entries         TEXT NOT NULL DEFAULT '[]',
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    conn.close()


init_db()


# ── Scraping helpers ──────────────────────────────────────────────────────────

def extract_package(url: str) -> Optional[str]:
    m = re.search(r'[?&]id=([^&\s]+)', url)
    return m.group(1) if m else None


async def dismiss_consent(page):
    for sel in ['button[aria-label="Accept all"]', 'button[jsname="b3VHJd"]',
                'button[data-type="accept"]']:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass


async def get_title(page) -> Optional[str]:
    candidates = [
        ('h1[itemprop="name"]', 'text'),
        ('h1.Fd93Bb', 'text'),
        ('h1', 'text'),
        ('meta[property="og:title"]', 'content'),
        ('title', 'text'),
    ]
    for sel, mode in candidates:
        try:
            el = await page.query_selector(sel)
            if not el:
                continue
            text = (await el.inner_text()).strip() if mode == 'text' else \
                   (await el.get_attribute('content') or '').strip()
            text = re.sub(r'\s*[-–]\s*(Apps on Google Play.*)?$', '', text).strip()
            if len(text) > 1:
                return text
        except Exception:
            continue
    return None


async def scrape_one(browser, url: str, shot_path: str) -> Optional[str]:
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT,
    )
    page = await ctx.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if resp and resp.status == 404:
            return None
        await page.wait_for_timeout(2_500)
        await dismiss_consent(page)
        title = await get_title(page)
        await page.screenshot(
            path=shot_path,
            clip={"x": 0, "y": 0, "width": 1280, "height": 850},
        )
        return title
    except Exception as exc:
        print(f"[scrape] {url} → {exc}")
        return None
    finally:
        await ctx.close()


# ── Phase runner ──────────────────────────────────────────────────────────────

async def run_phase(
    browser,
    db: sqlite3.Connection,
    package: str,
    job_id: str,
    app_id: int,
    scan_type: str,           # 'country' | 'language'
    items: list[tuple],       # [(display_name, code), ...]
    offset: int,              # progress offset
    phase_label: str,
) -> bool:
    """Scan one phase. Returns False if job was cancelled."""
    name_entries: dict[str, list[dict]] = {}
    name_shot:    dict[str, str]        = {}

    for i, (display_name, code) in enumerate(items):
        # Check for cancellation
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] == "cancelled":
            return False

        if scan_type == "country":
            url       = f"https://play.google.com/store/apps/details?id={package}&gl={code}"
            shot_file = f"{package}_gl_{code}.png"
        else:
            url       = f"https://play.google.com/store/apps/details?id={package}&hl={code}"
            shot_file = f"{package}_hl_{code}.png"

        shot_path = str(SCREENSHOTS_DIR / shot_file)

        db.execute(
            "UPDATE jobs SET progress=?, phase=?, message=? WHERE id=?",
            (offset + i, phase_label,
             f"{display_name} ({code})", job_id),
        )
        db.commit()

        title = await scrape_one(browser, url, shot_path)
        if title:
            if title not in name_entries:
                name_entries[title] = []
                name_shot[title]    = shot_file
            name_entries[title].append({"code": code, "name": display_name})

        await asyncio.sleep(0.8)

    for app_name, entries in name_entries.items():
        db.execute(
            "INSERT INTO designs (app_id, scan_type, app_name, screenshot_path, entries) "
            "VALUES (?,?,?,?,?)",
            (app_id, scan_type, app_name, name_shot[app_name], json.dumps(entries)),
        )
    db.commit()
    return True


async def scrape_developer_apps(browser, dev_url: str) -> list[str]:
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT,
    )
    page = await ctx.new_page()
    try:
        await page.goto(dev_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)
        for _ in range(14):
            await page.keyboard.press("End")
            await page.wait_for_timeout(700)
        links = await page.query_selector_all('a[href*="/store/apps/details?id="]')
        packages: set[str] = set()
        for link in links:
            href = await link.get_attribute("href") or ""
            m = re.search(r'[?&]id=([^&]+)', href)
            if m:
                packages.add(m.group(1))
        return list(packages)
    except Exception as exc:
        print(f"[dev scrape] {dev_url} → {exc}")
        return []
    finally:
        await ctx.close()


# ── Core app processor ────────────────────────────────────────────────────────

async def process_app(
    browser,
    db: sqlite3.Connection,
    package: str,
    pkg_url: str,
    job_id: str,
    offset: int,
) -> Optional[int]:
    """Upsert app, delete old designs, run both phases. Returns app_id or None."""
    db.execute(
        "INSERT INTO apps (package_name, original_url, checked_at) "
        "VALUES (?,?,datetime('now','localtime')) "
        "ON CONFLICT(package_name) DO UPDATE SET "
        "  original_url=excluded.original_url, checked_at=excluded.checked_at",
        (package, pkg_url),
    )
    db.commit()
    app_id = db.execute(
        "SELECT id FROM apps WHERE package_name=?", (package,)
    ).fetchone()["id"]
    db.execute("DELETE FROM designs WHERE app_id=?", (app_id,))
    db.commit()

    ok = await run_phase(browser, db, package, job_id, app_id,
                         "country", COUNTRIES,
                         offset, "Phase 1/2 — Countries")
    if not ok:
        return None

    ok = await run_phase(browser, db, package, job_id, app_id,
                         "language", LANGUAGES,
                         offset + len(COUNTRIES), "Phase 2/2 — Languages")
    if not ok:
        return None

    return app_id


# ── Background jobs ───────────────────────────────────────────────────────────

PHASE_TOTAL = len(COUNTRIES) + len(LANGUAGES)   # 76 + 47 = 123


async def job_check_app(job_id: str, package: str, url: str):
    db = get_db()
    try:
        db.execute("UPDATE jobs SET total=? WHERE id=?", (PHASE_TOTAL, job_id))
        db.commit()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                app_id = await process_app(browser, db, package, url, job_id, 0)
            finally:
                await browser.close()

        if app_id is None:
            return   # cancelled

        db.execute(
            "UPDATE jobs SET status='completed', progress=?, phase='', "
            "message='Done!', result_app_ids=? WHERE id=?",
            (PHASE_TOTAL, json.dumps([app_id]), job_id),
        )
        db.commit()
    except Exception as exc:
        print(f"[job] {job_id} failed: {exc}")
        db.execute("UPDATE jobs SET status='failed', error=? WHERE id=?",
                   (str(exc), job_id))
        db.commit()
    finally:
        db.close()


async def job_check_developer(job_id: str, dev_url: str):
    db = get_db()
    try:
        db.execute("UPDATE jobs SET message='Fetching developer apps…' WHERE id=?",
                   (job_id,))
        db.commit()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                packages = await scrape_developer_apps(browser, dev_url)
                if not packages:
                    db.execute(
                        "UPDATE jobs SET status='failed', "
                        "error='No apps found on developer page' WHERE id=?",
                        (job_id,),
                    )
                    db.commit()
                    return

                total = len(packages) * PHASE_TOTAL
                db.execute(
                    "UPDATE jobs SET total=?, message=? WHERE id=?",
                    (total, f"Found {len(packages)} apps…", job_id),
                )
                db.commit()

                result_ids = []
                for idx, pkg in enumerate(packages):
                    pkg_url = f"https://play.google.com/store/apps/details?id={pkg}"
                    offset  = idx * PHASE_TOTAL
                    app_id  = await process_app(
                        browser, db, pkg, pkg_url, job_id, offset
                    )
                    if app_id is None:
                        return   # cancelled
                    result_ids.append(app_id)

            finally:
                await browser.close()

        db.execute(
            "UPDATE jobs SET status='completed', progress=?, phase='', "
            "message='Done!', result_app_ids=? WHERE id=?",
            (total, json.dumps(result_ids), job_id),
        )
        db.commit()
    except Exception as exc:
        print(f"[dev job] {job_id} failed: {exc}")
        db.execute("UPDATE jobs SET status='failed', error=? WHERE id=?",
                   (str(exc), job_id))
        db.commit()
    finally:
        db.close()


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI()


class AppRequest(BaseModel):
    url: str


class DeveloperRequest(BaseModel):
    url: str


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/check-app")
async def check_app(req: AppRequest, bg: BackgroundTasks):
    package = extract_package(req.url)
    if not package:
        raise HTTPException(400, "Invalid Google Play app URL")
    job_id = str(uuid.uuid4())
    db = get_db()
    db.execute("INSERT INTO jobs (id, type, input_url) VALUES (?, 'app', ?)",
               (job_id, req.url))
    db.commit()
    db.close()
    bg.add_task(job_check_app, job_id, package, req.url)
    return {"job_id": job_id}


@app.post("/api/check-developer")
async def check_developer(req: DeveloperRequest, bg: BackgroundTasks):
    if not re.search(r'play\.google\.com/store/apps/dev', req.url):
        raise HTTPException(400, "Invalid Google Play developer URL")
    job_id = str(uuid.uuid4())
    db = get_db()
    db.execute("INSERT INTO jobs (id, type, input_url) VALUES (?, 'developer', ?)",
               (job_id, req.url))
    db.commit()
    db.close()
    bg.add_task(job_check_developer, job_id, req.url)
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    db = get_db()
    db.execute(
        "UPDATE jobs SET status='cancelled' WHERE id=? AND status='running'",
        (job_id,),
    )
    db.commit()
    db.close()
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    db = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Job not found")
    return dict(row)


@app.get("/api/apps")
async def list_apps():
    db = get_db()
    rows = db.execute("""
        SELECT a.*,
               COUNT(CASE WHEN d.scan_type='country'  THEN 1 END) AS country_designs,
               COUNT(CASE WHEN d.scan_type='language' THEN 1 END) AS language_designs
        FROM apps a
        LEFT JOIN designs d ON d.app_id = a.id
        GROUP BY a.id
        ORDER BY a.checked_at DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get("/api/apps/{app_id}")
async def get_app(app_id: int):
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
    if not app_row:
        raise HTTPException(404, "App not found")
    designs = db.execute(
        "SELECT * FROM designs WHERE app_id=? ORDER BY scan_type, id",
        (app_id,),
    ).fetchall()
    db.close()
    result = dict(app_row)
    result["designs"] = [
        {**dict(d), "entries": json.loads(d["entries"])} for d in designs
    ]
    return result


@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: int):
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
    if not app_row:
        raise HTTPException(404, "App not found")
    designs = db.execute(
        "SELECT screenshot_path FROM designs WHERE app_id=?", (app_id,)
    ).fetchall()
    for d in designs:
        if d["screenshot_path"]:
            p = SCREENSHOTS_DIR / d["screenshot_path"]
            if p.exists():
                p.unlink()
    db.execute("DELETE FROM apps WHERE id=?", (app_id,))
    db.commit()
    db.close()
    return {"ok": True}


# Serve screenshots and SPA
app.mount("/screenshots",
          StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
