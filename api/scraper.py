import asyncio
import aiohttp
from bs4 import BeautifulSoup
import pdfplumber
from pathlib import Path

from exam_codes import EXAM_CODES
from branch_codes import get_branch_name


# =========================
# CONSTANTS
# =========================

RESULT_URL = "http://results.jntuh.ac.in/resultAction"

PAYLOADS = [
    "&degree=btech&etype=r17&result=null&grad=null&type=intgrade&htno=",
    "&degree=btech&etype=r17&result=gradercrv&grad=null&type=rcrvintgrade&htno=",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html",
}

COLLEGE_PDF = Path("data/btech_mba_centers_reviewed.pdf")


# =========================
# COLLEGE MAP (PDF → CACHE)
# =========================

_COLLEGE_MAP = None


def load_college_map():
    global _COLLEGE_MAP
    import json
    COLLEGE_CACHE = Path("data/college_cache.json")

    if COLLEGE_CACHE.exists():
        return json.loads(COLLEGE_CACHE.read_text())

    if _COLLEGE_MAP is not None:
        return _COLLEGE_MAP

    _COLLEGE_MAP = {}

    if not COLLEGE_PDF.exists():
        print("⚠️ College PDF not found")
        return _COLLEGE_MAP

    with pdfplumber.open(COLLEGE_PDF) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table:
                continue
            for row in table[1:]:
                if not row or len(row) < 2:
                    continue
                code, college_name = row[0], row[1]
                if code and college_name and len(code.strip()) == 2:
                    _COLLEGE_MAP[code.strip()] = college_name.strip()

    COLLEGE_CACHE.write_text(json.dumps(_COLLEGE_MAP))
    return _COLLEGE_MAP


# =========================
# SCRAPER
# =========================

class ResultScraper:
    def __init__(self, roll_number: str):
        self.roll_number = roll_number.upper()
        self.college_map = load_college_map()

        # THIS is what main.py expects
        self.results = []   # list of { semester, meta, subjects }
        self._meta = None   # store once


    # ---------------------
    async def fetch(self, session, semester, exam_code, payload, sem):
        url = f"{RESULT_URL}?&examCode={exam_code}{payload}{self.roll_number}"
        is_rcrv = "rcrv" in payload.lower()
        try:
            async with sem:
                async with session.get(url, ssl=False, timeout=10) as r:
                    if r.status == 200:
                        html_content = await r.text()
                        return semester, exam_code, html_content, is_rcrv
                    return None
        except Exception:
            # Silently fail on individual request errors to allow other tasks to complete
            return None


    # ---------------------
    def parse_html(self, semester, exam_code, html, is_rcrv):
        if not html or "SUBJECT CODE" not in html:
            return

        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        if len(tables) < 2:
            return

        # -------- META (ONCE) --------
        if self._meta is None:
            try:
                details = tables[0].find_all("tr")
                htno = details[0].find_all("td")[1].text.strip()
                name = details[0].find_all("td")[3].text.strip()
                father = details[1].find_all("td")[1].text.strip()
                college_code = details[1].find_all("td")[3].text.strip()

                self._meta = {
                    "hallTicket": htno,
                    "name": name,
                    "fatherName": father,
                    "collegeCode": college_code,
                    "college": self.college_map.get(college_code),
                    "branch": get_branch_name(htno),
                }
            except Exception:
                return

        # -------- SUBJECTS --------
        rows = tables[1].find_all("tr")
        header = [b.text.strip() for b in rows[0].find_all("b")]

        subjects = []

        for row in rows[1:]:
            cols = row.find_all("td")
            if not cols:
                continue

            try:
                subject = {
                    "subjectCode": cols[header.index("SUBJECT CODE")].text.strip(),
                    "subjectName": cols[header.index("SUBJECT NAME")].text.strip(),
                    "examCode": exam_code,
                    "grade": cols[header.index("GRADE")].text.strip(),
                    "credits": cols[header.index("CREDITS(C)")].text.strip(),
                    "semester": semester,
                    "attempt": "rcrv" if is_rcrv else "regular", 
                }

                if "INTERNAL" in header:
                    subject["internal"] = cols[header.index("INTERNAL")].text.strip()
                if "EXTERNAL" in header:
                    subject["external"] = cols[header.index("EXTERNAL")].text.strip()
                if "TOTAL" in header:
                    subject["total"] = cols[header.index("TOTAL")].text.strip()

                subjects.append(subject)
            except (ValueError, IndexError):
                continue

        if subjects:
            self.results.append({
                "semester": semester,
                "meta": self._meta,
                "subjects": subjects,
            })


    # ---------------------
    async def scrape_all(self):
        SEM_LIMIT = 40  # Increased concurrency for max speed
        sem = asyncio.Semaphore(SEM_LIMIT)
        connector = aiohttp.TCPConnector(limit=SEM_LIMIT, force_close=False, enable_cleanup_closed=True)

        async with aiohttp.ClientSession(connector=connector, headers=HEADERS) as session:
            tasks = []

            for semester, exam_codes in EXAM_CODES.items():
                for code in exam_codes:
                    for payload in PAYLOADS:
                        tasks.append(self.fetch(session, semester, code, payload, sem))

            responses = await asyncio.gather(*tasks)

            for item in responses:
                if item and isinstance(item, tuple) and len(item) == 4:
                    semester, exam_code, html, is_rcrv = item
                    self.parse_html(semester, exam_code, html, is_rcrv)


    # ---------------------
    async def run(self):
        await self.scrape_all()
        return self.results if self.results else None


# =========================
# USED BY main.py
# =========================

async def scrape_all_results(htno: str):
    scraper = ResultScraper(htno)
    return await scraper.run()