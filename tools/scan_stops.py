#!/usr/bin/env python3
"""One-off: build data/stops.json by walking TPL FVG stop codes.

Stop codes look like <2-digit district><3-digit seq> (01001, 10001, 51001, ...). The API has no
"list stops" endpoint, so we probe /polemonitor/info sequentially per zone and
stop after a run of misses. Throttled; run once, re-run rarely. To ship the result: cp data/stops.json seed/
"""
import json
import pathlib
import sys
import time
import urllib.request

API = "https://realtime.tplfvg.it/API/v1.0/polemonitor"
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "stops.json"
MAX_MISSES = 15
DELAY = 0.15


def info(code):
    with urllib.request.urlopen(f"{API}/info?StopCode={code}", timeout=10) as r:
        return json.loads(r.read() or b"null")


def main():
    zones = [int(z) for z in sys.argv[1:]] or list(range(0, 100))
    stops = json.loads(OUT.read_text()) if OUT.exists() else {}
    for z in zones:
        misses, n, found = 0, 1, 0
        while misses < MAX_MISSES and n < 1000:
            code = f"{z:02d}{n:03d}"
            try:
                d = info(code)
            except Exception as e:  # network hiccup: retry once later
                print("err", code, e, file=sys.stderr)
                time.sleep(2)
                continue
            if d and d.get("Latitude"):
                stops[code] = {
                    "code": code,
                    "name": d["Address"],
                    "lat": d["Latitude"],
                    "lon": d["Longitude"],
                    "urban": d["IsUrban"],
                }
                misses, found = 0, found + 1
            else:
                misses += 1
            n += 1
            time.sleep(DELAY)
        if found:
            print(f"district {z:02d}: {found} stops", flush=True)
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps(stops, indent=0, ensure_ascii=False))
    print(f"total {len(stops)} -> {OUT}")


if __name__ == "__main__":
    main()
