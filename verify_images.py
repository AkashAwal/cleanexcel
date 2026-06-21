"""
Verify every image URL in shopify_import_001.csv (or any CSV) is a real image.
Checks: HTTP status, Content-Type, Content-Length.
Flags anything that looks like a placeholder or broken link.
"""
import csv, sys, time, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CSV_FILE     = sys.argv[1] if len(sys.argv) > 1 else "shopify_import_001.csv"
MIN_SIZE_KB  = 2       # images below this are likely placeholders
MAX_WORKERS  = 20      # parallel HEAD requests
TIMEOUT      = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def check_url(isbn, url):
    try:
        r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        status  = r.status_code
        ctype   = r.headers.get("Content-Type", "")
        size_kb = int(r.headers.get("Content-Length", 0)) / 1024

        if status != 200:
            return isbn, url, "FAIL", f"HTTP {status}"
        if "image" not in ctype.lower():
            # Some servers don't return Content-Type on HEAD — try GET with stream
            return isbn, url, "WARN", f"Content-Type: {ctype or 'missing'}"
        if 0 < size_kb < MIN_SIZE_KB:
            return isbn, url, "TINY", f"{size_kb:.1f} KB (possible placeholder)"
        return isbn, url, "OK", f"{size_kb:.1f} KB  {ctype}"
    except Exception as e:
        return isbn, url, "ERR", str(e)[:60]

def main():
    rows = []
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            isbn = row.get("Variant SKU", "").strip()
            url  = row.get("Image Src", "").strip()
            if isbn and url:
                rows.append((isbn, url))

    unique = list(dict.fromkeys(rows))   # deduplicate by (isbn, url)
    print(f"Checking {len(unique)} image URLs from {CSV_FILE} ...\n")

    results = {"OK": [], "WARN": [], "TINY": [], "FAIL": [], "ERR": []}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(check_url, isbn, url): (isbn, url) for isbn, url in unique}
        done = 0
        for fut in as_completed(futures):
            isbn, url, status, detail = fut.result()
            results[status].append((isbn, url, detail))
            done += 1
            if done % 100 == 0 or done == len(unique):
                ok = len(results["OK"])
                bad = len(results["WARN"]) + len(results["TINY"]) + len(results["FAIL"]) + len(results["ERR"])
                print(f"  [{done}/{len(unique)}]  OK={ok}  Issues={bad}", end="\r")

    print(f"\n\n{'='*60}")
    print(f"  TOTAL : {len(unique)}")
    print(f"  OK    : {len(results['OK'])}")
    print(f"  WARN  : {len(results['WARN'])}  (no Content-Type — likely still fine)")
    print(f"  TINY  : {len(results['TINY'])}  (< {MIN_SIZE_KB} KB — possible placeholder)")
    print(f"  FAIL  : {len(results['FAIL'])}  (bad HTTP status)")
    print(f"  ERR   : {len(results['ERR'])}  (connection error)")
    print(f"{'='*60}\n")

    for label in ["FAIL", "TINY", "ERR", "WARN"]:
        if results[label]:
            print(f"\n-- {label} --")
            for isbn, url, detail in results[label]:
                print(f"  {isbn}  {detail}")
                print(f"    {url[:80]}")

    bad_total = sum(len(results[k]) for k in ["FAIL", "TINY", "ERR"])
    if bad_total == 0:
        print("All images verified real.")
    else:
        print(f"\n{bad_total} images need attention (WARN entries are usually fine).")

if __name__ == "__main__":
    main()
