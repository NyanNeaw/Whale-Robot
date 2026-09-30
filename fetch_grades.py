#!/usr/bin/env python3
"""Fetch one term's grades from dograde.online (Loeipit) and save grades.html.

Follows the normal browser flow of the ASP.NET WebForms page:

  1. GET  default.aspx  -> read __VIEWSTATE, __VIEWSTATEGENERATOR,
                           __EVENTVALIDATION
  2. POST default.aspx  with TxtUser (student ID), txtPassword (birthdate,
                           dd/mm/BE-year) and the term button's name=value.
                           There is no separate login button: the term
                           buttons (ButtonX1..ButtonX6) submit the login.
                           On success the server 301s to DooTermX.aspx and
                           sets the SSUser cookie; requests follows it.
  3. Save the DooTermX.aspx response to grades.html.

Credentials come from .env (or the environment), never from this file:

    DOGRADE_STUDENT_ID=...
    DOGRADE_BIRTHDATE=dd/mm/BEyear

Requests are strictly sequential, on a single requests.Session.
"""
import argparse
import random
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

URL = "https://www.dograde.online/loeipit/default.aspx"
HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
TIMEOUT = 10  # seconds per request
FAST_DELAY = (3, 6)  # random jitter between retries
SLOW_AFTER = 120  # seconds of continuous failure before slowing down
SLOW_DELAY = 15
MAX_WORKERS = 5  # upper bound on parallel attempts

# The server reports login problems by emitting a call to one of these JS
# functions in the response. The modal texts themselves are always in the
# HTML, so only an actual call (not "function ShowFailN() {") counts.
_CALL = r"(?<!function )(?<!function\t)\b{}\(\)"
REJECTED = {
    "ShowFail2": "ID/birthdate has no permission to view grades",
    "ShowFail3": "no student record found for this ID/birthdate",
}
RETRYABLE_MSG = {
    "ShowFail1": "server says the student database is unavailable right now",
}


class LoginRejected(Exception):
    """The site clearly said the ID/birthdate is wrong. Do not retry."""


class AttemptFailed(Exception):
    """Transient/unclear failure. Retry."""


def load_env(path=".env"):
    values = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip("'\"")
    return values


def get_credentials():
    import os

    env = load_env(Path(__file__).with_name(".env"))
    sid = os.environ.get("DOGRADE_STUDENT_ID") or env.get("DOGRADE_STUDENT_ID")
    bday = os.environ.get("DOGRADE_BIRTHDATE") or env.get("DOGRADE_BIRTHDATE")
    if not sid or not bday:
        sys.exit("Set DOGRADE_STUDENT_ID and DOGRADE_BIRTHDATE in .env (see .env.example)")
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", bday):
        sys.exit("DOGRADE_BIRTHDATE must look like dd/mm/BEyear, e.g. 13/01/2553")
    return sid, bday


def find_calls(html, names):
    return [n for n in names if re.search(_CALL.format(n), html)]


def attempt(session, sid, bday, term):
    """One full GET -> POST -> (redirect) pass. Returns the grades page bytes."""
    try:
        r = session.get(URL, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        data = {}
        for name in HIDDEN_FIELDS:
            tag = soup.find("input", {"name": name})
            if tag is None:
                raise AttemptFailed(f"{name} not found on login page")
            data[name] = tag.get("value", "")

        button = soup.find("input", {"type": "submit", "value": term})
        if button is None:
            raise AttemptFailed(f"term button {term!r} not found on login page")
        data.update({"TxtUser": sid, "txtPassword": bday, button["name"]: term})

        r = session.post(URL, data=data, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise AttemptFailed(f"{type(e).__name__}: {e}") from None

    if urlparse(r.url).path.lower().endswith("/default.aspx"):
        # Not redirected to the term page: the login page came back.
        text = r.content.decode(r.encoding or "utf-8", errors="replace")
        rejected = find_calls(text, REJECTED)
        if rejected:
            raise LoginRejected(REJECTED[rejected[0]])
        soft = find_calls(text, RETRYABLE_MSG)
        if soft:
            raise AttemptFailed(RETRYABLE_MSG[soft[0]])
        raise AttemptFailed("login page returned without a redirect or a known message")
    return r.content


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--term", default="ปี2ภาค1", help="term button label (default: %(default)s)")
    ap.add_argument("--out", default="grades.html", help="output file (default: %(default)s)")
    ap.add_argument("--give-up-after", type=float, metavar="MIN",
                    help="stop retrying after this many minutes (default: never)")
    ap.add_argument("--workers", type=int, default=3, metavar="N",
                    help=f"parallel attempts, 1-{MAX_WORKERS} (default: %(default)s)")
    args = ap.parse_args()
    workers = max(1, min(args.workers, MAX_WORKERS))

    sid, bday = get_credentials()
    started = time.monotonic()
    stop = threading.Event()
    lock = threading.Lock()
    result = {}  # "page" on success, "error" on a reason to abort

    def worker(wid):
        # One Session per worker: each needs its own cookies/ViewState.
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) grades-fetch/1.0"
        n = 0
        while not stop.is_set():
            n += 1
            try:
                page = attempt(session, sid, bday, args.term)
            except LoginRejected as e:
                with lock:
                    result.setdefault("error", f"Login rejected by the site: {e}. Check .env; not retrying.")
                stop.set()
                return
            except AttemptFailed as e:
                elapsed = time.monotonic() - started
                if args.give_up_after and elapsed > args.give_up_after * 60:
                    with lock:
                        result.setdefault("error", f"Giving up after {elapsed:.0f}s: {e}")
                    stop.set()
                    return
                delay = SLOW_DELAY if elapsed >= SLOW_AFTER else random.uniform(*FAST_DELAY)
                print(f"[w{wid} attempt {n}, {elapsed:.0f}s] {e} -- retrying in {delay:.1f}s", file=sys.stderr)
                stop.wait(delay)
                continue
            with lock:
                if "page" not in result:
                    result["page"] = page
                    result["who"] = f"w{wid} attempt {n}"
            stop.set()
            return

    threads = [threading.Thread(target=worker, args=(i + 1,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        stop.set()
        sys.exit("\nInterrupted.")

    if "page" in result:
        Path(args.out).write_bytes(result["page"])
        print(f"Saved {args.term} to {args.out} ({len(result['page'])} bytes) via {result['who']}")
    else:
        sys.exit(result.get("error", "Stopped without a result."))


if __name__ == "__main__":
    main()
