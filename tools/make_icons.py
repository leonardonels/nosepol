#!/usr/bin/env python3
"""NoSePol icons: Golfo. After the Prismark glass icon: a solid tile, solid pieces with
soft shadows, and one clear glass piece that shows what's under it.

The tile is the gulf and its shore: sea above, land below, the coastline between. The
orange route runs along the coast, the heading wedge ahead of the bus, and the bus
itself is the glass disc, the shore and route seen through it.

Writes SVG masters for the four looks a future app needs (default, dark, tinted, clear),
each rounded and full bleed, plus the small favicon, and renders PNGs with headless
Chromium:

    python3 tools/make_icons.py            # SVGs + PNGs into web/icons/
    python3 tools/make_icons.py --svg-only

Full bleed is for platforms that mask the icon themselves (iOS, Android maskable);
everything that matters stays inside the central 80%.
"""
import argparse
import math
import pathlib
import shutil
import subprocess
import tempfile

OUT = pathlib.Path(__file__).resolve().parent.parent / "web" / "icons"

# sea top/bottom, land, coastline (colour, opacity), route, waves (opacity),
# glass tint and opacity (top, bottom), shadow (colour, opacity), glow
LOOKS = {
    "default": dict(sea=("#3B9BF5", "#0A5FD0"), land="#F3EEE3", coast=("#FFFFFF", .8), route="#F07B3F",
                    waves=.55, tint="#FFFFFF", glass=(.78, .3), shadow=("#021A3F", .42), glow=None),
    "dark": dict(sea=("#1E5FA8", "#0B2F66"), land="#1D2430", coast=("#FFFFFF", .25), route="#FF8A4C",
                 waves=.45, tint="#C9D2E3", glass=(.9, .5), shadow=("#000000", .6), glow="#FF8A4C"),
    "tinted": dict(sea=("#3A3A40", "#222226"), land="#5C5C64", coast=("#FFFFFF", .35), route="#ECECF0",
                   waves=.4, tint="#FFFFFF", glass=(.6, .2), shadow=("#000000", .5), glow=None),
    "clear": dict(sea=("rgba(255,255,255,.30)", "rgba(255,255,255,.14)"), land="rgba(255,255,255,.5)",
                  coast=("#FFFFFF", .7), route="#FFFFFF", waves=.6, tint="#FFFFFF", glass=(.55, .14),
                  shadow=("#000000", .22), glow=None),
}

COAST = "M-40 300 C 180 330 300 420 400 520 S 620 760 1064 820"
ROUTE = "M-20 420 C 170 440 290 520 390 620 S 600 850 1044 905"   # on land, a little inland
BUS = (470, 685)    # the glass disc on the route
R = 150
HEADING = 118       # degrees clockwise from north: along the route, down the coast
WAVES = [(180, 190, 1.0), (300, 250, .75)]


def disc_path(cx, cy, r):
    return f"M{cx - r} {cy} A{r} {r} 0 1 0 {cx + r} {cy} A{r} {r} 0 1 0 {cx - r} {cy} Z"


def wedge_path(cx, cy, r, gap=22, length=80, half=70, heading=HEADING):
    """The heading wedge, a notched arrowhead ahead of the disc."""
    a = math.radians(heading)
    rot = lambda x, y: (cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a))
    base = r + gap
    pts = [rot(0, -(base + length)), rot(half, -base), rot(0, -(base + length * .32)), rot(-half, -base)]
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z"


def icon(look, rounded):
    L = LOOKS[look]
    disc, wedge = disc_path(*BUS, R), wedge_path(*BUS, R)
    land = COAST + " L1064 1064 L-40 1064 Z"
    sc, so = L["shadow"]
    g0, g1 = L["glass"]
    o = L["route"]
    full = 'filterUnits="userSpaceOnUse" x="-200" y="-200" width="1424" height="1424"'
    glow = (f'<path d="{ROUTE}" fill="none" stroke="{L["glow"]}" stroke-opacity=".5" stroke-width="110" filter="url(#soft)"/>'
            f'<path d="{wedge}" fill="{L["glow"]}" fill-opacity=".5" filter="url(#soft)"/>' if L["glow"] else "")
    waves = "".join(f'<path d="M{x} {y} q 40 -26 80 0 t 80 0" fill="none" stroke="#FFFFFF" stroke-opacity="{L["waves"] * k:.2f}" '
                    f'stroke-width="16" stroke-linecap="round"/>' for x, y, k in WAVES)
    seen = f'<path d="{land}" fill="{L["land"]}"/><path d="{ROUTE}" fill="none" stroke="{o}" stroke-width="70" stroke-linecap="round"/>'
    rx = 230 if rounded else 0
    tile_rim = ('<rect x="3" y="3" width="1018" height="1018" rx="227" fill="none" stroke="url(#tileRim)" stroke-width="6"/>'
                if rounded else "")
    hx, hy = BUS[0] - 52, BUS[1] - 70
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <defs>
    <linearGradient id="sea" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{L["sea"][0]}"/><stop offset="1" stop-color="{L["sea"][1]}"/>
    </linearGradient>
    <linearGradient id="tileRim" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity=".55"/><stop offset=".35" stop-color="#FFFFFF" stop-opacity="0"/>
      <stop offset=".7" stop-color="#FFFFFF" stop-opacity="0"/><stop offset="1" stop-color="#FFFFFF" stop-opacity=".3"/>
    </linearGradient>
    <linearGradient id="glass" x1=".15" y1="0" x2=".85" y2="1">
      <stop offset="0" stop-color="{L["tint"]}" stop-opacity="{g0}"/><stop offset="1" stop-color="{L["tint"]}" stop-opacity="{g1}"/>
    </linearGradient>
    <linearGradient id="rim" x1=".1" y1="0" x2=".9" y2="1">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity=".95"/><stop offset=".4" stop-color="#FFFFFF" stop-opacity=".15"/>
      <stop offset=".75" stop-color="#FFFFFF" stop-opacity=".1"/><stop offset="1" stop-color="#FFFFFF" stop-opacity=".65"/>
    </linearGradient>
    <clipPath id="tileClip"><rect width="1024" height="1024" rx="{rx}"/></clipPath>
    <clipPath id="inDisc"><path d="{disc}"/></clipPath>
    <filter id="soft" {full}><feGaussianBlur stdDeviation="24"/></filter>
    <filter id="lift" {full}><feGaussianBlur stdDeviation="10"/></filter>
    <filter id="refract" {full}><feGaussianBlur stdDeviation="7"/></filter>
    <filter id="drop" {full}><feGaussianBlur stdDeviation="22"/></filter>
  </defs>
  <g clip-path="url(#tileClip)">
    <rect width="1024" height="1024" fill="url(#sea)"/>
    {waves}
    <path d="{land}" fill="{L["land"]}"/>
    <path d="{COAST}" fill="none" stroke="{L["coast"][0]}" stroke-opacity="{L["coast"][1]}" stroke-width="8"/>
    <!-- solid pieces: the route and the heading wedge, a soft shadow under each, glowing in dark mode -->
    {glow}
    <path d="{ROUTE}" fill="none" stroke="{sc}" stroke-opacity="{so * .6:.2f}" stroke-width="70" stroke-linecap="round" transform="translate(0 14)" filter="url(#lift)"/>
    <path d="{ROUTE}" fill="none" stroke="{o}" stroke-width="70" stroke-linecap="round"/>
    <path d="{wedge}" fill="{sc}" fill-opacity="{so * .6:.2f}" transform="translate(0 14)" filter="url(#lift)"/>
    <path d="{wedge}" fill="{o}" stroke="{o}" stroke-width="8" stroke-linejoin="round"/>
    <!-- the glass bus: its shadow, the shore and route seen through it (blurred, magnified a touch), the glass, its rim -->
    <path d="{disc}" fill="{sc}" fill-opacity="{so}" transform="translate(0 28)" filter="url(#drop)"/>
    <g clip-path="url(#inDisc)">
      <rect width="1024" height="1024" fill="url(#sea)"/>
      <g filter="url(#refract)" transform="translate({BUS[0]} {BUS[1]}) scale(1.12) translate({-BUS[0]} {-BUS[1]})">{seen}</g>
    </g>
    <path d="{disc}" fill="url(#glass)"/>
    <path d="{disc}" fill="none" stroke="url(#rim)" stroke-width="7"/>
    <ellipse cx="{hx}" cy="{hy}" rx="70" ry="28" fill="#FFFFFF" fill-opacity=".3" transform="rotate(-34 {hx} {hy})"/>
  </g>
  {tile_rim}
</svg>
'''


def favicon():
    """32 px and below: no glass. Sea, shore, the orange route, and the bus as the map draws it:
    an orange disc with a white ring (a white disc would vanish into the shore)."""
    s = 1 / 16  # the 1024 drawing scaled to 64
    cx, cy, r = BUS[0] * s, BUS[1] * s, 10.5
    w = wedge_path(cx, cy, r, gap=1.6, length=6.5, half=5.8)
    coast = "M-3 19 C 11 21 19 26 25 32.5 S 39 47.5 67 51"
    route = "M-2 26 C 11 27.5 18 32.5 24.5 39 S 37.5 53 66 56.5"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3B9BF5"/><stop offset="1" stop-color="#0A5FD0"/></linearGradient>
    <clipPath id="c"><rect width="64" height="64" rx="15"/></clipPath>
  </defs>
  <g clip-path="url(#c)">
    <rect width="64" height="64" fill="url(#s)"/>
    <path d="{coast} L67 67 L-3 67 Z" fill="#F3EEE3"/>
    <path d="{route}" fill="none" stroke="#F07B3F" stroke-width="5.5" stroke-linecap="round"/>
    <path d="{w}" fill="#F07B3F"/>
    <circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="#F07B3F" stroke="#FFFFFF" stroke-width="3"/>
  </g>
</svg>
'''


def render(svgs, sizes):
    chrome = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not chrome:
        raise SystemExit("no Chromium found: run with --svg-only")
    # snap Chromium reads neither /tmp nor hidden folders: work in a plain folder in $HOME
    with tempfile.TemporaryDirectory(dir=pathlib.Path.home(), prefix="nosepol-icons-") as tmp:
        for name, size in sizes:
            src = svgs[name]
            page = pathlib.Path(tmp) / f"{name}-{size}.html"
            page.write_text(f'<html><body style="margin:0;background:transparent">'
                            f'<img src="file://{src}" width="{size}" height="{size}" style="display:block"></body></html>')
            png = pathlib.Path(tmp) / "shot.png"
            subprocess.run([chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                            "--default-background-color=00000000", f"--window-size={size},{size}",
                            f"--screenshot={png}", f"file://{page}"], check=True, capture_output=True)
            shutil.copyfile(png, OUT / f"{name}-{size}.png")
            print(f"icons/{name}-{size}.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--svg-only", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    svgs = {}
    for look in LOOKS:
        for rounded in (True, False):
            name = f"app-{look}" + ("" if rounded else "-full")
            path = OUT / f"{name}.svg"
            path.write_text(icon(look, rounded))
            svgs[name] = path
            print(f"icons/{name}.svg")
    (OUT.parent / "favicon.svg").write_text(favicon())
    print("favicon.svg")
    if not args.svg_only:
        render(svgs, [("app-default", 192), ("app-default", 512), ("app-default-full", 512),
                      ("app-default-full", 180), ("app-dark", 512), ("app-tinted", 512), ("app-clear", 512)])


if __name__ == "__main__":
    main()
