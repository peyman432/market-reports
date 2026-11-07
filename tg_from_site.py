# -*- coding: utf-8 -*-
"""
tg_from_site.py — ارسال نمودارها و گزارش‌های بازار به کانال تلگرام، با خواندن data.json از سایت GitHub Pages.
این برنامه در GitHub Actions (سرور خارج از ایران) اجرا می‌شود؛ پس به پروکسی/فیلترشکن نیاز ندارد.

اجرای محلی برای تست (بدون ارسال):   python tg_from_site.py --dry --file data.json
اجرای واقعی بیرون از ساعت کاری:      python tg_from_site.py --force
اجرای یک جلسه‌ی کامل بازار:          python tg_from_site.py --loop   (تا پایان ساعت کاری هر ۵ دقیقه؛ در GitHub Actions)

متغیرهای محیطی:
  TG_TOKEN, TG_CHAT          توکن ربات و شناسه‌ی کانال (در GitHub به‌صورت Secrets)
  SITE_URL                   آدرس data.json (پیش‌فرض: سایت شما)
  WINDOW_START / WINDOW_END  بازه‌ی مجاز به وقت تهران (پیش‌فرض 08:45 تا 12:45)
  MAX_AGE                    اگر داده‌ی سایت قدیمی‌تر از این ثانیه باشد (پیش‌فرض 1200) ارسال نمی‌شود (تعطیلی/قطع ناشر)
  TG_PROXY                   اختیاری، فقط برای اجرا روی کامپیوتر داخل ایران
"""
import glob, hashlib, html as _html, io, json, os, re, sys, textwrap, time, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
import arabic_reshaper
from bidi.algorithm import get_display

BASE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Tehran")
SITE_URL = os.environ.get("SITE_URL", "https://peyman432.github.io/market-reports/data.json")
TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
PROXY = os.environ.get("TG_PROXY", "")
WIN_S = os.environ.get("WINDOW_START", "08:45")
WIN_E = os.environ.get("WINDOW_END", "12:45")
CHART_START = os.environ.get("CHART_START", "09:00")   # نمودارها از این ساعت شروع می‌شوند
MARKET_END = os.environ.get("MARKET_END", "12:30")     # و در این ساعت پایان می‌یابند
UPDATE_MINUTES = int(os.environ.get("UPDATE_MINUTES", 5))   # عدد اعلام‌شده در پیام زمان به‌روزرسانی
MAX_AGE = int(os.environ.get("MAX_AGE", 1200))
GAP = float(os.environ.get("TG_GAP", 3.0))   # کانال‌ها حدوداً ۲۰ پیام در دقیقه را می‌پذیرند
STATE_FILE = os.path.join(BASE, "tg_state.json")
RESET = "--reset" in sys.argv                      # پیام‌های قبلی ربات را پاک و همه چیز را از نو (به ترتیب) می‌فرستد
PROBE = RESET or "--force" in sys.argv             # وجود پیام‌های ثبت‌شده را در کانال راستی‌آزمایی می‌کند (پیام پاک‌شده دوباره ساخته می‌شود)
DPI = 150

BG, SF, SF2, GRID = "#15161a", "#1e2025", "#24262c", "#2f323a"
TX, T2, UP, DN, AC = "#f2f3f5", "#9aa0ab", "#33d17a", "#ff4d5e", "#3ea6ff"


def skip(reason):
    """دلیل ارسال‌نشدن را در لاگ و در خلاصه‌ی اجرای GitHub Actions ثبت می‌کند"""
    log(reason)
    print(f"::notice title=ارسال انجام نشد::{reason}", flush=True)
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm:
        try:
            with open(sm, "a", encoding="utf-8") as f:
                f.write(f"### ⏸ ارسال انجام نشد\n{reason}\n\nبرای ارسال اجباری: Run workflow ← تیک force\n")
        except Exception:
            pass


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


# ============================ فونت و متن فارسی ============================
def setup_font():
    path = os.environ.get("FONT_PATH")
    if not (path and os.path.exists(path)):
        found = glob.glob(os.path.join(BASE, "fonts", "*.ttf")) + glob.glob(os.path.join(BASE, "*.ttf"))
        path = next((p for p in found if "vazir" in os.path.basename(p).lower()), found[0] if found else None)
    if path:
        fm.fontManager.addfont(path)
        name = fm.FontProperties(fname=path).get_name()
    else:
        have = {f.name for f in fm.fontManager.ttflist}
        name = next((n for n in ("Vazirmatn", "Vazir", "Noto Sans Arabic", "Noto Naskh Arabic", "Tahoma",
                                 "Segoe UI", "Arial", "DejaVu Sans") if n in have), "DejaVu Sans")
    plt.rcParams["font.family"] = name
    plt.rcParams["axes.unicode_minus"] = False
    log("فونت نمودار:", name)


try:   # matplotlib ≥ 3.11 با libraqm خودش فارسی را درست حروف‌چینی می‌کند؛ نسخه‌های قدیمی‌تر به reshape + bidi نیاز دارند
    import matplotlib.ft2font as _ft
    NATIVE_RTL = hasattr(_ft, "LayoutItem")
except Exception:
    NATIVE_RTL = False


def fa(s):
    s = str(s)
    return s if NATIVE_RTL else get_display(arabic_reshaper.reshape(s))


def arr(v):
    return np.array([np.nan if x is None else x for x in v], dtype=float)


def hhmm(t):
    return [str(x)[:5] for x in t]


# ============================ ابزار رسم ============================
def style(ax):
    ax.set_facecolor(SF)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=T2, labelsize=9)
    ax.grid(True, color=GRID, alpha=0.55, lw=0.7)
    ax.set_axisbelow(True)


def frame(title, sub, stamp, size=(10, 5.6), left=0.08, right=0.95, bottom=0.12, top=None):
    W, H = size
    fig, ax = plt.subplots(figsize=size, dpi=DPI, facecolor=BG)
    hdr = 1.25                       # ارتفاع سربرگ بر حسب اینچ (ثابت، مستقل از ارتفاع شکل)
    top = 1 - (hdr + 0.12) / H
    fig.subplots_adjust(left=left, right=right, bottom=bottom, top=top)
    fig.text(0.97, 1 - 0.20 / H, fa(title), ha="right", va="top", color=TX, fontsize=19, fontweight="bold")
    fig.text(0.97, 1 - 0.78 / H, fa(sub), ha="right", va="top", color=T2, fontsize=10.5)
    fig.text(0.03, 1 - 0.24 / H, stamp, ha="left", va="top", color=AC, fontsize=10)
    fig.add_artist(Line2D([0.03, 0.97], [1 - hdr / H] * 2, color=AC, lw=1.3, transform=fig.transFigure))
    style(ax)
    return fig, ax


def trim(t, *series):
    """نگه داشتن فقط نقطه‌های بین CHART_START و MARKET_END (۰۹:۰۰ تا ۱۲:۳۰)"""
    idx = [i for i, x in enumerate(t) if CHART_START <= str(x)[:5] <= MARKET_END]
    if not idx:
        idx = list(range(len(t)))
    a, b = idx[0], idx[-1] + 1
    return [t[a:b]] + [x[a:b] for x in series]


def to_png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def time_axis(ax, t):
    n = len(t)
    step = max(1, int(np.ceil(n / 8)))
    idx = list(range(0, n, step))
    ax.set_xticks(idx)
    ax.set_xticklabels([t[i] for i in idx])
    ax.set_xlim(-0.5, n - 1 + max(1.5, n * 0.09))


def end_label(ax, y, color, d=2):
    ok = np.where(np.isfinite(y))[0]
    if not len(ok):
        return
    i = ok[-1]
    ax.annotate(f"{y[i]:,.{d}f}", (i, y[i]), xytext=(7, 0), textcoords="offset points",
                color=color, fontsize=10, fontweight="bold", va="center")


def legend(ax, items, loc="upper left"):
    hs = [Line2D([0], [0], color=c, lw=3) for _, c in items]
    lg = ax.legend(hs, [fa(n) for n, _ in items], loc=loc, fontsize=9.5, framealpha=0.9)
    lg.get_frame().set_facecolor(SF2)
    lg.get_frame().set_edgecolor(GRID)
    for t in lg.get_texts():
        t.set_color(TX)


# ============================ نمودارها ============================
def ch_index(c):
    h2 = c["h2"]
    t, i1, i2 = trim(hhmm(h2["t"]), arr(h2["s"]["idx"]), arr(h2["s"]["eq"]))
    fig, ax = frame("شاخص کل و شاخص هم‌وزن", "درصد تغییر در طول روز", c["st2"])
    x = np.arange(len(t))
    for y, color in ((i1, AC), (i2, "#f39c12")):
        ax.plot(x, y, color=color, lw=2.4)
        end_label(ax, y, color)
    ax.axhline(0, color=DN, lw=1.2, ls="--")
    time_axis(ax, t)
    legend(ax, [("شاخص کل", AC), ("شاخص هم‌وزن", "#f39c12")], loc="lower right")
    return to_png(fig)


def hbar(title, sub, stamp, labels, values, fmt="{:+,.0f}", left=0.22):
    n = len(labels)
    fig, ax = frame(title, sub, stamp, size=(10, max(4.8, 0.40 * n + 2.0)), left=left, right=0.93,
                    bottom=0.06)
    v = np.nan_to_num(arr(values))
    ax.barh(range(n), v, color=[UP if a >= 0 else DN for a in v], height=0.66)
    ax.set_yticks(range(n))
    ax.set_yticklabels([fa(s if len(str(s)) <= 22 else str(s)[:21] + "…") for s in labels], color=TX, fontsize=10)
    ax.invert_yaxis()
    lo, hi = min(0, v.min()), max(0, v.max())
    pad = ((hi - lo) or 1) * 0.2
    ax.set_xlim(lo - (pad if lo < 0 else 0), hi + pad)
    off = ((hi - lo) or 1) * 0.012
    for i, a in enumerate(v):
        ax.text(a + (off if a >= 0 else -off), i, fmt.format(a), va="center", ha="left" if a >= 0 else "right",
                color=TX, fontsize=9.5)
    ax.axvline(0, color=T2, lw=1)
    ax.grid(axis="y", visible=False)
    return to_png(fig)


def ch_status(c):
    l = c["h2"]["last"]
    names = ["مثبت", "منفی", "صف خرید", "صف فروش", "بدون تغییر"]
    vals = [l["مثبت"], l["منفی"], l["صف_خرید"], l["صف_فروش"], l["صفر"]]
    fig, ax = frame("وضعیت کلی سهم‌ها", "مثبت، منفی، صف‌ها و بدون‌تغییر در آخرین لحظه", c["st2"],
                    size=(10, 5.2), left=0.17, right=0.93, bottom=0.08)
    ax.barh(range(5), vals, color=["#2ecc71", "#e74c3c", "#27ae60", "#c0392b", "#95a5a6"], height=0.62)
    ax.set_yticks(range(5))
    ax.set_yticklabels([fa(s) for s in names], color=TX, fontsize=12)
    ax.invert_yaxis()
    mx = max(vals) or 1
    ax.set_xlim(0, mx * 1.15)
    for i, v in enumerate(vals):
        ax.text(v + mx * 0.01, i, f"{v:,.0f}", va="center", color=TX, fontsize=12, fontweight="bold")
    ax.grid(axis="y", visible=False)
    return to_png(fig)


def _mins(ts):
    ts = str(ts)
    return int(ts[:2]) * 60 + int(ts[3:5]) + (int(ts[6:8]) / 60 if len(ts) >= 8 else 0)


def half_flow(h2, step=15):
    """تغییر خالص پول در هر بازه‌ی ۱۵ دقیقه‌ای بین CHART_START و MARKET_END؛ مبنا = آخرین مقدار تا ساعت شروع"""
    cap, st = _mins(MARKET_END[:5] + ":00"), _mins(CHART_START[:5] + ":00")
    base, last = 0.0, {}
    for ts, v in zip(h2["t"], arr(h2["s"]["m_all"])):
        if not np.isfinite(v):
            continue
        m = _mins(ts)
        if m <= st:
            base = v
        elif m <= cap + 0.999:
            last[int(np.ceil(min(m, cap) / step - 1e-9))] = v
    ks = sorted(last)
    vals = [last[k] for k in ks]
    delta = np.diff(vals, prepend=base) if vals else np.array([])
    return [f"{k * step // 60:02d}:{k * step % 60:02d}" for k in ks], delta


def ch_half(c):
    labels, v = half_flow(c["h2"])
    if not len(v):
        raise ValueError("بدون داده")
    x = np.arange(len(v))
    fig, ax = frame("جریان پول هر ۱۵ دقیقه", "ورود و خروج خالص پول کل بازار در پایان هر بازه (میلیارد تومان)", c["st2"])
    ax.fill_between(x, v, 0, where=v >= 0, interpolate=True, color=UP, alpha=0.25)
    ax.fill_between(x, v, 0, where=v < 0, interpolate=True, color=DN, alpha=0.25)
    ax.plot(x, v, color=AC, lw=2.4, zorder=3)
    ax.scatter(x, v, c=[UP if a >= 0 else DN for a in v], s=48, zorder=4, edgecolors=BG)
    for i, a in enumerate(v):
        ax.annotate(f"{a:+,.0f}", (i, a), xytext=(0, 9 if a >= 0 else -9), textcoords="offset points",
                    ha="center", va="bottom" if a >= 0 else "top", color=UP if a >= 0 else DN,
                    fontsize=9.5, fontweight="bold")
    ax.axhline(0, color=T2, lw=1, ls="--")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45 if len(x) > 10 else 0)
    ax.set_xlim(-0.6, len(x) - 0.4)
    rng = (v.max() - v.min()) or 1
    ax.set_ylim(min(v.min(), 0) - rng * 0.18, max(v.max(), 0) + rng * 0.18)
    return to_png(fig)


def area(c, key, title, sub):
    h2 = c["h2"]
    t, y = trim(hhmm(h2["t"]), np.nan_to_num(arr(h2["s"][key])))
    x = np.arange(len(y))
    fig, ax = frame(title, sub, c["st2"])
    ax.fill_between(x, y, 0, where=y >= 0, interpolate=True, color=UP, alpha=0.38)
    ax.fill_between(x, y, 0, where=y < 0, interpolate=True, color=DN, alpha=0.38)
    ax.plot(x, y, color="#1abc9c", lw=2.2)
    ax.axhline(0, color=DN, lw=1.4, ls="--")
    end_label(ax, y, "#1abc9c", 0)
    ip, it = int(np.argmax(y)), int(np.argmin(y))
    ax.annotate(f"{y[ip]:+,.0f}", (ip, y[ip]), xytext=(0, 8), textcoords="offset points", ha="center", color=UP, fontsize=9)
    if y[it] < 0:
        ax.annotate(f"{y[it]:+,.0f}", (it, y[it]), xytext=(0, -8), textcoords="offset points", ha="center",
                    va="top", color=DN, fontsize=9)
    time_axis(ax, t)
    rng = (y.max() - y.min()) or 1
    ax.set_ylim(min(y.min(), 0) - rng * 0.12, max(y.max(), 0) + rng * 0.12)
    legend(ax, [("تحرک خالص", "#1abc9c"), ("خط صفر", DN)])
    return to_png(fig)


def ch_net_all(c):
    return area(c, "net_all", "تحرک خالص کلی بازار", "تحرک مثبت منهای تحرک منفی")


def ch_net_big(c):
    return area(c, "net_big", "تحرک خالص سهام بزرگ", "تحرک مثبت منهای منفی در ۳۰ سهم بزرگ")


def ch_net_etf(c):
    return area(c, "net_etf", "تحرک خالص سهام اهرمی", "تحرک مثبت منهای منفی در صندوق‌های اهرمی")


def ch_inflow(c):
    d = c["h3"]["inflow"]
    return hbar("بیشترین ورود پول", "۱۲ نماد برتر (میلیون تومان)", c["st3"], d["l"], d["v"])


def ch_heavy(c):
    d = c["h3"]["heavy"]
    return hbar("بیشترین خالص معاملات درشت", "تعداد خرید منهای فروش در معاملات بالای ۱۰۰، ۲۰۰ و ۵۰۰ میلیون",
                c["st3"], d["l"], d["v"])


def ch_sector(c):
    d = c["h3"]["sec_div"]
    if not d["l"]:
        raise ValueError("بدون داده‌ی صنایع")
    return hbar("ورود و خروج پول صنایع", "۱۰ صنعت با بیشترین ورود و ۱۰ صنعت با بیشترین خروج (میلیارد تومان)",
                c["st3"], d["l"], d["v"], left=0.26)


def ch_table(c):
    rows_, V = c["h3"]["gold"], c["h3"]["colors"]
    heads = ["نماد", "٪ آخرین", "امتیاز کوانت", "امتیاز سهم", "قدرت خرید", "سیگنال تعداد", "سیگنال درشت", "ورود پول", "وضعیت"]
    widths = [0.09, 0.08, 0.10, 0.09, 0.09, 0.11, 0.11, 0.13, 0.20]
    n = len(rows_)
    if not n:
        raise ValueError("بدون داده")
    fig, ax = frame("برترین نمادها", "بالاترین امتیاز کوانت", c["st3"], size=(10, 0.45 * (n + 1) + 1.7),
                    left=0.02, right=0.98, bottom=0.03)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    h = 1 / (n + 1)

    def cells(y, vals, colors, bold=False, bg=None):
        if bg:
            ax.add_patch(Rectangle((0, y - h / 2), 1, h, color=bg, zorder=0, lw=0))
        xr = 1.0
        for w, v, col in zip(widths, vals, colors):
            ax.text(xr - w / 2, y, v, ha="center", va="center", color=col, fontsize=9 if bold else 10,
                    fontweight="bold" if bold else "normal")
            xr -= w

    cells(1 - h / 2, [fa(x) for x in heads], [T2] * 9, True, SF2)
    g = lambda r, k: r.get(k) or 0
    for i, r in enumerate(rows_):
        vc = V.get(r["final_verdict"], TX)
        dp = g(r, "darsad_akarin")
        pc = UP if dp > 0 else DN if dp < 0 else TX
        mc = UP if g(r, "vorod_pol") >= 0 else DN
        cells(1 - h * (i + 1.5),
              [fa(r["name"]), f"{dp:+.2f}", f"{g(r, 'quant_score'):.2f}", f"{g(r, 'emtiaz_sahm'):.1f}",
               f"{g(r, 'buy_power'):.2f}", f"{g(r, 'signal_tedad'):.0f}", f"{g(r, 'signal_big_m'):.0f}",
               f"{g(r, 'vorod_pol'):+,.0f}", fa(r["final_verdict"])],
              [TX, pc, vc, TX, TX, TX, TX, mc, vc], False, SF if i % 2 == 0 else BG)
    return to_png(fig)


# ============================ تقویم و سلب مسئولیت ============================
J_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAY = {5: "شنبه", 6: "یکشنبه", 0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه"}
WD_ORDER = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]   # ستون اول = شنبه (سمت راست)
FA_DIG = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def pdg(x):
    return str(x).translate(FA_DIG)


def g2j(gy, gm, gd):
    """تبدیل تاریخ میلادی به شمسی"""
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def ch_calendar(now):
    """تقویم ساده شبیه استیکر (برگه‌ی تقویم، پس‌زمینه‌ی شفاف ۵۱۲×۵۱۲)؛ خروجی: (webp, png)"""
    from matplotlib.patches import Circle, FancyBboxPatch
    jy, jm, jd = g2j(now.year, now.month, now.day)
    wd = WEEKDAY[now.weekday()]
    band = "#e5484d" if now.weekday() == 4 else "#2f80ed"      # جمعه قرمز
    fig = plt.figure(figsize=(5.12, 5.12), dpi=100)
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.095, 0.05), 0.84, 0.86, boxstyle="round,pad=0,rounding_size=0.08",
                                fc="black", ec="none", alpha=0.16))
    card = FancyBboxPatch((0.075, 0.075), 0.85, 0.85, boxstyle="round,pad=0,rounding_size=0.08",
                          fc="#ffffff", ec="#d5d9e2", lw=2)
    ax.add_patch(card)
    top = Rectangle((0.075, 0.70), 0.85, 0.225, fc=band, ec="none")
    ax.add_patch(top)
    top.set_clip_path(card)
    for x in (0.30, 0.70):
        ax.add_patch(Circle((x, 0.925), 0.026, fc="#ffffff", ec="#8a93a6", lw=2.5, zorder=5))
    ax.text(0.5, 0.80, fa(f"{J_MONTHS[jm - 1]} {pdg(jy)}"), ha="center", va="center", color="white", fontsize=30,
            fontweight="bold")
    ax.text(0.5, 0.49, pdg(jd), ha="center", va="center", color="#1f2330", fontsize=150, fontweight="bold")
    ax.text(0.5, 0.235, fa(wd), ha="center", va="center", color=band, fontsize=36, fontweight="bold")
    ax.text(0.5, 0.135, now.strftime("%Y-%m-%d"), ha="center", va="center", color="#8a93a6", fontsize=16)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True, dpi=100)
    plt.close(fig)
    png = buf.getvalue()
    webp = None
    try:
        from PIL import Image
        w = io.BytesIO()
        Image.open(io.BytesIO(png)).convert("RGBA").save(w, "WEBP", quality=92, method=6)
        webp = w.getvalue()
    except Exception as e:
        log("تبدیل به webp ممکن نشد (به‌جای استیکر، عکس ارسال می‌شود):", e)
    return webp, png


DISCLAIMER = [
    "این گزارش‌ها و نمودارها به‌صورت خودکار و بر پایه‌ی داده‌های لحظه‌ای تولید می‌شوند و صرفاً جنبه‌ی اطلاع‌رسانی و آموزشی دارند.",
    "هیچ‌یک از مطالب این کانال توصیه‌ی مالی، پیشنهاد خرید یا فروش، یا دعوت به سرمایه‌گذاری نیست.",
    "امتیازها و سیگنال‌ها بر پایه‌ی قواعد ساده‌ی آماری ساخته می‌شوند و ممکن است ناقص، دارای تأخیر یا خطا باشند.",
    "سرمایه‌گذاری در بازار سرمایه با ریسک همراه است و ممکن است به زیان بینجامد؛ بازدهی گذشته تضمین‌کننده‌ی آینده نیست.",
    "تصمیم نهایی و مسئولیت پیامدهای آن بر عهده‌ی خود شماست؛ پیش از هر معامله تحلیل شخصی انجام دهید یا با مشاور مالی دارای مجوز مشورت کنید.",
    "تهیه‌کننده و ناشر این کانال هیچ مسئولیتی در قبال زیان‌های ناشی از استفاده از این اطلاعات ندارد.",
]


def disclaimer_html():
    """متن سلب مسئولیت؛ تلگرام رنگ متن را پشتیبانی نمی‌کند، پس با نشانگرهای رنگی، برجسته‌سازی و کادر نقل‌قول نمایش داده می‌شود"""
    import html
    icons = ["🔵", "🔴", "🟠", "🟡", "🟢", "⚪"]
    body = "\n\n".join(f"{ic} {html.escape(t)}" for ic, t in zip(icons, DISCLAIMER))
    return [f"⚠️ <b>سلب مسئولیت</b>\n\n<blockquote>{body}</blockquote>"]


# (کلید، عنوان کپشن، تابع، منبع داده)
CHARTS = [
    ("index", "📊 شاخص کل و هم‌وزن", ch_index, 2),
    ("status", "📊 وضعیت کلی سهم‌ها", ch_status, 2),
    ("half", "💰 جریان پول هر ۱۵ دقیقه", ch_half, 2),
    ("net_all", "📈 تحرک خالص کلی بازار", ch_net_all, 2),
    ("net_big", "🏛️ تحرک خالص سهام بزرگ", ch_net_big, 2),
    ("net_etf", "⚡ تحرک خالص سهام اهرمی", ch_net_etf, 2),
    ("inflow", "💵 بیشترین ورود پول", ch_inflow, 3),
    ("heavy", "🐋 بیشترین خالص معاملات درشت", ch_heavy, 3),
    ("sector", "🏭 ورود/خروج پول صنایع", ch_sector, 3),
    ("table", "🏆 برترین نمادها (امتیاز کوانت)", ch_table, 3),
]


# ============================ متن‌ها ============================
def summary_text(h2):
    a = h2["a"]
    lines = [f"📅 {h2['time']}", "", f"📊 امتیاز کلی: {a['score']}/100  —  {a['status']}",
             f"💡 {a['recommendation']}", "", "🔍 عوامل امتیازدهی:"]
    lines += a.get("factors") or ["— (هیچ عامل مثبتی فعال نیست)"]
    lines += ["", "⚠️ هشدارها:"] + ([f"• {w}" for w in a.get("warnings") or []] or ["✅ هیچ هشداری وجود ندارد"])
    if a.get("notes"):
        lines += ["", "💡 نکات تحلیلی:"] + [f"• {n}" for n in a["notes"]]
    return "\n".join(lines)


def events_text(h2):
    ev = h2.get("events") or {}
    out = [f"⏰ {h2['time']}", ""] + list(ev.get("lines") or [])
    if ev.get("notes"):
        out += ["", "💡 جمع‌بندی:"] + [f"• {n}" for n in ev["notes"]]
    return "\n".join(out)


def chunk(text, limit=3800):
    text = re.sub(r"^=+$", "=" * 28, text, flags=re.M)
    parts, cur = [], ""
    for ln in text.split("\n"):
        while len(ln) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(ln[:limit])
            ln = ln[limit:]
        if len(cur) + len(ln) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        parts.append(cur)
    return [p.strip("\n") for p in parts] or [text[:limit]]


# ============================ قالب‌بندی متن‌ها (مثل سلب مسئولیت: عنوان پررنگ + کادر نقل‌قول + نشانگر رنگی) ============================
def _line_html(raw):
    t = raw.rstrip()
    if not t.strip():
        return ""
    st = t.strip()
    esc = _html.escape(st, quote=False)
    indent = "    " if raw.startswith("   ") else ""
    is_bullet = st[:1] in "•-–" and st[1:2] == " "
    if is_bullet:
        body = _html.escape(st[2:].strip(), quote=False)
        m = re.search(r":\s*([+\-−])\s?\d", st)
        mark = ("🟢" if m.group(1) == "+" else "🔴") if m else "▫️"
        return f"{indent}{mark} {body}"
    if st.endswith(":") and len(st) < 70:
        return f"<b>{esc}</b>"
    return indent + esc


def html_msgs(title, body, limit=3500):
    """متن ساده ← لیست پیام‌های HTML (هر کدام حداکثر ~limit نویسه)؛ همه‌ی تگ‌ها در هر پیام بسته می‌شوند"""
    raw = re.sub(r"^[=─\-]{6,}$", "\u0000", body, flags=re.M).split("\n")
    out, i = [], 0
    while i < len(raw):                                     # سربرگ‌های بین دو خط ===== پررنگ شوند
        if raw[i] == "\u0000" and i + 2 < len(raw) and raw[i + 2] == "\u0000" and raw[i + 1].strip():
            out += ["", f"<b>{_html.escape(raw[i + 1].strip(), quote=False)}</b>"]
            i += 3
        elif raw[i] == "\u0000":
            i += 1
        else:
            out.append(_line_html(raw[i]))
            i += 1
    col = []
    for ln in out:                                          # چند خط خالی پشت‌سرهم ← یک خط خالی
        if ln == "" and col and col[-1] == "":
            continue
        col.append(ln)
    out = col
    msgs, cur = [], []
    for ln in out:
        if sum(len(x) + 1 for x in cur) + len(ln) > limit and cur:
            msgs.append(cur)
            cur = []
        cur.append(ln)
    msgs.append(cur)
    res = []
    for k, ls in enumerate(msgs):
        txt = "\n".join(ls).strip("\n")
        head = f"<b>{_html.escape(title, quote=False)}</b>\n\n" if k == 0 else ""
        res.append(f"{head}<blockquote>{txt}</blockquote>")
    return res


def split_title(report):
    """عنوان گزارش = اولین خط غیرخالی که از جنس ===== نیست"""
    lines = report.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() and not re.fullmatch(r"[=─\-]{6,}", ln.strip()):
            return ln.strip(), "\n".join(lines[i + 1:])
    return "گزارش", report


def _kv(text):
    """خطوط «• برچسب: مقدار» و «- برچسب: مقدار» یک گزارش متنی ← دیکشنری"""
    d = {}
    for ln in (text or "").split("\n"):
        m = re.match(r"^\s*[•\-]\s*(.+?):\s*(.+?)\s*$", ln)
        if m:
            d.setdefault(m.group(1).strip(), m.group(2).strip())
    return d


def _sgn(v, d=0):
    return f"{v:+,.{d}f}"


def _num(txt):
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(txt or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _trend(x):
    return "↗" if x > 0 else "↘" if x < 0 else "→"


def status_text(h2):
    """وضعیت کلی بازار: تصویر لحظه‌ای کوتاه (امتیاز، آمار، شاخص‌ها، جریان پول، روند امروز، دلایل، هشدارها)"""
    a, last, kv = h2["a"], h2["last"], _kv(h2.get("report", ""))
    money = lambda k: (kv.get(k) or "—").replace(" میلیارد تومان", "")
    L = [f"📅 {h2['time']}", "",
         "🎯 امتیاز بازار:",
         f"• امتیاز کلی: {a['score']}/100 — {a['status']}",
         f"• توصیه: {a['recommendation']}", "",
         "📊 وضعیت بازار:",
         f"• نمادها: مثبت {last['مثبت']:,.0f} | منفی {last['منفی']:,.0f} | بدون تغییر {last['صفر']:,.0f}",
         f"• صف‌ها: خرید {last['صف_خرید']:,.0f} | فروش {last['صف_فروش']:,.0f} | خالص احساسات {a.get('net_sentiment', 0):+,.0f}",
         f"• شاخص کل: {_sgn(last['شاخص_کل'], 2)}٪ | هم‌وزن: {_sgn(last['شاخص_هموزن'], 2)}٪", "",
         "💰 ورود/خروج پول (میلیارد تومان):",
         f"• کل بازار: {money('ورود یا خروج پول به کل بازار')}",
         f"• سهام بزرگ: {money('ورود یا خروج پول به سهام بزرگ')}",
         f"• صندوق‌های اهرمی: {money('ورود یا خروج پول به صندوق‌های اهرمی')}", ""]
    gs = (h2.get("events") or {}).get("groups") or []
    if gs:
        L.append("🧭 روند امروز (تحرک خالص):")
        for g in gs:
            L.append(f"• {g['name']}: {_sgn(g['cur'])} (اوج {_sgn(g['peak'])} | ۳۰ دقیقه اخیر {_sgn(g['d30'])} {_trend(g['d30'])})")
        L.append("")
    L += ["🔍 عوامل امتیازدهی:"] + (a.get("factors") or ["— (هیچ عامل مثبتی فعال نیست)"]) + [""]
    L += ["⚠️ هشدارها:"] + ([f"• {w}" for w in a["warnings"]] if a.get("warnings") else ["✅ هیچ هشداری وجود ندارد"])
    if a.get("notes"):
        L += ["", "💡 نکات تحلیلی:"] + [f"• {n}" for n in a["notes"]]
    return "\n".join(L)


def full_report_text(h2, h3):
    """گزارش تحلیلی جامع: جزئیات تکنیکال، رویدادهای روز، سیگنال‌ها، صنایع و سهام برتر (بدون تکرار مطالب «وضعیت کلی»)"""
    k2, L = _kv(h2.get("report", "")), []
    g = lambda k: k2.get(k, "—")
    L += [f"📅 {h2['time']}", "",
          "📈 تحلیل تکنیکال:",
          f"• شاخص قدرت نسبی (RSI): کل {g('کل بازار')} | بزرگ {g('سهام بزرگ')} | اهرمی {g('صندوق‌های اهرمی')}",
          f"• میانگین قدرت خریدار: کل {g('میانگین قدرت کل')} | بزرگ {g('میانگین قدرت سهام بزرگ')} | صندوق‌ها {g('میانگین قدرت صندوق‌ها')}",
          "• معاملات بزرگ (خرید / فروش): " + " | ".join(
              f"بالای {n} {g(f'خرید بالای {n}')} / {g(f'فروش بالای {n}')}" for n in (100, 200, 500)),
          f"• نسبت خرید به فروش درشت: {g('نسبت خرید به فروش')}", "",
          "💎 شاخص‌های پیشرفته:",
          f"• اطمینان {g('شاخص اطمینان')} | مومنتوم {g('مومنتوم بازار')} | نوسان {g('نوسان بازار')}",
          f"• فشار نقدینگی {g('فشار نقدینگی')} | هماهنگی {g('هماهنگی بازار')} | قدرت نهادی {g('قدرت نهادی')}", ""]
    ev = h2.get("events") or {}
    if ev.get("groups"):
        L.append("🧭 رویدادهای کلیدی روز:")
        for e in ev["groups"]:
            cross = f"عبور از صفر {str(e['cross_t'])[:5]}" if e.get("cross_t") else "بدون عبور از صفر"
            pull = f" | افت از اوج {e['pull']:.0f}٪" if e.get("pull", 0) >= 10 else ""
            L.append(f"• {e['name']}: اوج {_sgn(e['peak'])} ({str(e['peak_t'])[:5]}) | کف {_sgn(e['low'])} ({str(e['low_t'])[:5]}) | {cross}{pull}")
        for n in ev.get("notes") or []:
            L.append(f"• {n}")
        L.append("")
    if h3:
        k3, rep3, c = _kv(h3.get("report", "")), h3.get("report", ""), h3.get("counts") or {}
        n = c.get("n") or 1
        pc = lambda v: f"{v:,} ({v * 100 / n:.1f}٪)"
        L += [f"🎯 سیگنال‌های {n:,} سهم:",
              f"• قوی خرید {pc(c.get('gold', 0))} | خرید {pc(c.get('strong_buy', 0))} | ضعیف خرید {pc(c.get('weak_buy', 0))}",
              f"• خنثی {pc(c.get('neutral', 0))} | فروش {pc(c.get('sell', 0))}"]
        k = lambda key: k3.get(key, "—")
        vin, vout = _num(k("مجموع ورود پول")), _num(k("مجموع خروج پول"))
        if vin is not None and vout is not None:
            L += ["", "💰 جریان نقدینگی سهام (میلیون تومان):",
                  f"• خالص: {_sgn(vin + vout)}",
                  f"• ورود: {_sgn(vin)} در {k('تعداد سهام با ورود پول')} سهم",
                  f"• خروج: {_sgn(vout)} در {k('تعداد سهام با خروج پول')} سهم"]
        names = lambda hdr: re.findall(r"\[(.+?) \((?:امتیاز|امتیاز ریسک): (-?[\d.]+)\)\]",
                                       (re.search(re.escape(hdr) + r"\s*\n(.+)", rep3) or [None, ""])[1])
        buy, sell = names("🎯 سهام با قوی‌ترین سیگنال خرید:"), names("⚠️ سهام با سیگنال فروش:")
        if buy:
            L += ["", "🔥 قوی‌ترین سیگنال خرید:", "• " + "، ".join(x for x, _ in buy)]
        if sell:
            L += ["", "⚠️ سیگنال فروش و ریسک:", "• " + "، ".join(f"{x} ({y})" for x, y in sell)]
        secs = h3.get("sectors") or []
        if secs:
            L += ["", "🏢 صنایع با بیشترین ورود پول (میلیون تومان):"]
            L += [f"• {x['sector']}: {_sgn(x['vorod_pol'])} (امتیاز {x['quant_score']:.2f})" for x in secs[:5]]
        ob, os_ = k("سهام با RSI اشباع خرید (>70)"), k("سهام با RSI اشباع فروش (<30)")
        L += ["", "📊 RSI سهام:", f"• اشباع خرید (بالای ۷۰): {ob} سهم | اشباع فروش (زیر ۳۰): {os_} سهم"]
        gold = h3.get("gold") or []
        if gold:
            L += ["", "⭐ برترین نمادها (امتیاز کوانت):"]
            L += [f"• {r['name']}: امتیاز {r['quant_score']:.2f} | قدرت خریدار {r['buy_power']:.2f} | ورود پول {_sgn(r['vorod_pol'])}"
                  for r in gold[:5]]
    return "\n".join(L).strip()


def update_notice(h2):
    last = (h2 or {}).get("time", "")
    body = "\n\n".join([
        f"🟢 اطلاعات هر {pdg(UPDATE_MINUTES)} دقیقه به‌روزرسانی می‌شود و پیام‌ها و نمودارهای قبلی جایگزین می‌شوند.",
        f"🔵 ساعت به‌روزرسانی: {WIN_S} تا {WIN_E} (نمودارها از {CHART_START} تا {MARKET_END})",
        f"🟠 آخرین داده‌ی دریافت‌شده: {last}",
    ])
    return [f"🔄 <b>زمان به‌روزرسانی</b>\n\n<blockquote>{_html.escape(body, quote=False)}</blockquote>"]


# ============================ تلگرام ============================
class Gone(Exception):
    pass


class TG:
    def __init__(self, token, chat, proxy=""):
        self.base = f"{os.environ.get('TG_API_BASE', 'https://api.telegram.org').rstrip('/')}/bot{token}"
        self.chat = chat
        self.s = requests.Session()
        self.s.trust_env = False
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def call(self, method, data=None, files=None):
        net = 0
        for _ in range(12):          # خطای ۴۲۹ (محدودیت نرخ تلگرام) از شمار تلاش‌های شبکه کم نمی‌شود
            try:
                j = self.s.post(f"{self.base}/{method}", data=data, files=files, timeout=(15, 90)).json()
            except Exception as e:
                net += 1
                if net >= 4:
                    raise RuntimeError(f"خطای شبکه: {e}")
                time.sleep(4 * net)
                continue
            if j.get("ok"):
                time.sleep(GAP)
                return j["result"]
            desc = j.get("description", "")
            if j.get("error_code") == 429:
                time.sleep(min(j.get("parameters", {}).get("retry_after", 5), 60) + 1)
                continue
            low = desc.lower()
            if "message is not modified" in low:
                return None
            if any(k in low for k in ("message to edit not found", "message_id_invalid", "can't be edited",
                                      "message to delete not found")):
                raise Gone(desc)
            raise RuntimeError(desc)
        raise RuntimeError("درخواست پس از چند تلاش ناموفق ماند")

    def send_photo(self, png, cap):
        r = self.call("sendPhoto", {"chat_id": self.chat, "caption": cap[:1000]}, {"photo": ("c.png", png, "image/png")})
        return r["message_id"]

    def edit_photo(self, mid, png, cap):
        media = json.dumps({"type": "photo", "media": "attach://p", "caption": cap[:1000]}, ensure_ascii=False)
        self.call("editMessageMedia", {"chat_id": self.chat, "message_id": mid, "media": media}, {"p": ("c.png", png, "image/png")})

    def send_sticker(self, webp):
        r = self.call("sendSticker", {"chat_id": self.chat}, {"sticker": ("s.webp", webp, "image/webp")})
        return r["message_id"]

    def send_text(self, text, html=False):
        d = {"chat_id": self.chat, "text": text, "disable_web_page_preview": True}
        if html:
            d["parse_mode"] = "HTML"
        return self.call("sendMessage", d)["message_id"]

    def edit_text(self, mid, text, html=False):
        d = {"chat_id": self.chat, "message_id": mid, "text": text, "disable_web_page_preview": True}
        if html:
            d["parse_mode"] = "HTML"
        self.call("editMessageText", d)

    def exists(self, mid):
        """بررسی ارزان وجود پیام در کانال (ویرایش بدون تغییر: «not modified» یعنی هست، «not found» یعنی پاک شده)"""
        try:
            self.call("editMessageReplyMarkup", {"chat_id": self.chat, "message_id": mid,
                                                 "reply_markup": json.dumps({"inline_keyboard": []})})
            return True
        except Gone:
            return False
        except Exception:
            return True

    def delete(self, mid):
        try:
            self.call("deleteMessage", {"chat_id": self.chat, "message_id": mid})
        except Exception:
            pass


# ============================ وضعیت پیام‌ها (برای جایگزینی) ============================
def load_state(today):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("day") == today:
            return st
    except Exception:
        pass
    return {"day": today, "items": {}}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def sha(b):
    return hashlib.sha1(b if isinstance(b, bytes) else b.encode("utf-8")).hexdigest()


def upsert_photo(tg, st, key, png, cap):
    it, h = st["items"].get(key), sha(png)
    if it and it["h"] == [h]:
        if not PROBE or tg.exists(it["ids"][0]):
            return
        it = None                                   # در کانال پاک شده؛ دوباره ارسال می‌شود
    if it:
        try:
            tg.edit_photo(it["ids"][0], png, cap)
            it["h"] = [h]
            save_state(st)
            return
        except Gone:
            pass
    st["items"][key] = {"ids": [tg.send_photo(png, cap)], "h": [h]}
    save_state(st)


def upsert_text(tg, st, key, text, html=False):
    parts = list(text) if isinstance(text, list) else ([text] if html else chunk(text))
    it = st["items"].setdefault(key, {"ids": [], "h": []})
    for i, p in enumerate(parts):
        h = sha(p)
        if i < len(it["ids"]):
            if it["h"][i] == h and not (PROBE and not tg.exists(it["ids"][i])):
                continue
            try:
                tg.edit_text(it["ids"][i], p, html)
                it["h"][i] = h
            except Gone:
                it["ids"][i], it["h"][i] = tg.send_text(p, html), h
        else:
            it["ids"].append(tg.send_text(p, html))
            it["h"].append(h)
        save_state(st)
    while len(it["ids"]) > len(parts):
        tg.delete(it["ids"].pop())
        it["h"].pop()
        save_state(st)


def upsert_sticker(tg, st, key, webp, png, cap):
    """استیکر قابل ویرایش نیست؛ فقط یک بار در روز (یا با تغییر تصویر) ارسال می‌شود. اگر استیکر پذیرفته نشد، عکس می‌فرستد."""
    h = sha(png)
    it = st["items"].get(key)
    if it and it["h"] == [h] and not (PROBE and not tg.exists(it["ids"][0])):
        return
    if it:
        tg.delete(it["ids"][0])
    mid = None
    if webp:
        try:
            mid = tg.send_sticker(webp)
        except Exception as e:
            log("استیکر ارسال نشد، به‌جایش عکس ارسال می‌شود:", e)
    if mid is None:
        mid = tg.send_photo(png, cap)
    st["items"][key] = {"ids": [mid], "h": [h]}
    save_state(st)


# ============================ اجرا ============================
def load_data():
    if "--file" in sys.argv:
        with open(sys.argv[sys.argv.index("--file") + 1], encoding="utf-8") as f:
            return json.load(f)
    r = requests.get(SITE_URL, params={"t": int(time.time())}, headers={"Cache-Control": "no-cache"}, timeout=30)
    r.raise_for_status()
    return r.json()


def build_items(d):
    h2, h3 = d.get("h2"), d.get("h3")
    date2 = (h2 or {}).get("time", "")
    hm = str(d.get("updated", ""))[11:16]
    c = {"h2": h2, "h3": h3, "st2": date2, "st3": f"{date2.split()[0] if date2 else ''}  {hm}".strip()}
    items = []
    try:
        items.append(("calendar", "sticker", ch_calendar(datetime.now(TZ)), "📅 تقویم امروز"))
    except Exception as e:
        log("❌ تقویم:", e)
        traceback.print_exc()
    items.append(("t_update", "html", update_notice(h2), None))
    for key, title, fn, src in CHARTS:
        if not (h2 if src == 2 else h3):
            continue
        try:
            items.append((key, "photo", fn(c), f"{title}\n🕒 {c['st2' if src == 2 else 'st3']}"))
        except Exception as e:
            log(f"❌ نمودار {key}: {e}")
            if not isinstance(e, ValueError):
                traceback.print_exc()
    if h2:
        try:
            items.append(("t_status", "html", html_msgs("🎯 وضعیت کلی بازار", status_text(h2)), None))
            items.append(("t_report", "html", html_msgs("📊 گزارش تحلیلی جامع بازار بورس", full_report_text(h2, h3)), None))
        except Exception as e:
            log("❌ متن‌های گزارش:", e)
            traceback.print_exc()
    try:
        items.append(("t_disclaimer", "html", disclaimer_html(), None))
    except Exception as e:
        log("❌ سلب مسئولیت:", e)
        traceback.print_exc()
    return items


_font_ready = False


def cycle():
    """یک دور کامل: خواندن data.json ← ساخت نمودارها و متن‌ها ← ارسال/ویرایش در تلگرام. خروجی: تعداد موارد ناموفق"""
    global _font_ready
    force, dry = ("--force" in sys.argv or RESET), "--dry" in sys.argv
    now = datetime.now(TZ)
    if not (force or dry) and not (WIN_S <= now.strftime("%H:%M") <= WIN_E):
        skip(f"خارج از ساعت کاری ({WIN_S} تا {WIN_E} تهران) — ساعت فعلی {now.strftime('%H:%M')}")
        return
    try:
        d = load_data()
    except Exception as e:
        skip(f"دریافت data.json از سایت ناموفق بود: {e}")
        return
    if not force and not dry and "--file" not in sys.argv:
        try:
            age = (now - datetime.strptime(d["updated"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)).total_seconds()
        except Exception:
            age = 0
        if age > MAX_AGE:
            skip(f"داده‌ی سایت قدیمی است (آخرین به‌روزرسانی: {d.get('updated')} — {age / 60:.0f} دقیقه پیش). بازار تعطیل است یا publisher.py روی کامپیوتر شما اجرا نمی‌شود")
            return
    if not _font_ready:
        setup_font()
        _font_ready = True
    items = build_items(d)
    if dry:
        out = os.path.join(BASE, "dry_out")
        os.makedirs(out, exist_ok=True)
        for key, kind, payload, _ in items:
            if kind == "sticker":
                for ext, b_ in (("webp", payload[0]), ("png", payload[1])):
                    if b_:
                        with open(os.path.join(out, f"{key}.{ext}"), "wb") as f:
                            f.write(b_)
            elif kind == "photo":
                with open(os.path.join(out, key + ".png"), "wb") as f:
                    f.write(payload)
            else:
                log(f"متن {key}: {sum(len(x) for x in payload)} نویسه در {len(payload)} پیام")
        log("خروجی آزمایشی در", out)
        return
    if not TOKEN or not CHAT:
        log("❌ TG_TOKEN و TG_CHAT تنظیم نشده‌اند")
        sys.exit(1)
    tg, st = TG(TOKEN, CHAT, PROXY), load_state(now.strftime("%Y-%m-%d"))
    for old in ("t_summary", "t_events", "t_h2", "t_h3"):       # چهار متن قدیمی (جایگزین‌شده با دو متن ادغام‌شده)
        it = st["items"].pop(old, None)
        if it:
            for mid in it["ids"]:
                tg.delete(mid)
            save_state(st)
    if RESET:
        n = 0
        for it in st["items"].values():
            for mid in it["ids"]:
                tg.delete(mid)
                n += 1
        st = {"day": now.strftime("%Y-%m-%d"), "items": {}}
        save_state(st)
        log(f"حالت reset: {n} پیام قبلی حذف شد؛ همه‌ی پیام‌ها از نو و به ترتیب ارسال می‌شوند")

    def send(key, kind, payload, cap):
        if kind == "sticker":
            upsert_sticker(tg, st, key, payload[0], payload[1], cap)
        elif kind == "photo":
            upsert_photo(tg, st, key, payload, cap)
        else:
            upsert_text(tg, st, key, payload, html=(kind == "html"))

    ok, errs = 0, {}
    for key, kind, payload, cap in items:
        try:
            send(key, kind, payload, cap)
            ok += 1
        except Exception as e:
            errs[key] = (e, kind, payload, cap)
            log(f"⚠️ {key}: {e} — در پایان دوباره تلاش می‌شود")
    if errs:                                          # دور دوم برای موارد ناموفق (مثلاً محدودیت نرخ تلگرام)
        time.sleep(30)
        for key, (_, kind, payload, cap) in list(errs.items()):
            try:
                send(key, kind, payload, cap)
                ok += 1
                del errs[key]
            except Exception as e:
                errs[key] = (e, kind, payload, cap)
                log(f"❌ {key}: {e}")
                print(f"::error title=ارسال {key} ناموفق بود::{e}", flush=True)
    log(f"پایان — موفق: {ok} | ناموفق: {len(errs)}" + (f" ({', '.join(errs)})" if errs else ""))
    return len(errs)


def _hm_to_min(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def main():
    for s_ in (sys.stdout, sys.stderr):
        try:
            s_.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--loop" not in sys.argv:                  # اجرای تکی (تست / force / reset)
        if cycle():
            sys.exit(1)                           # اجرا قرمز می‌شود تا خطا از چشم پنهان نماند
        return
    # حالت جلسه: یک اجرا تمام ساعت کاری را خودش هر EVERY ثانیه می‌چرخد (بدون وابستگی به زمان‌بند GitHub)
    every = int(os.environ.get("TG_EVERY", 300))
    log(f"حالت جلسه: از {WIN_S} تا {WIN_E} هر {every // 60} دقیقه")
    n = 0
    while True:
        now = datetime.now(TZ)
        hm = now.strftime("%H:%M")
        if hm > WIN_E:
            log(f"⏹ پایان ساعت کاری ({WIN_E}) — خروج پس از {n} دور")
            return
        if hm < WIN_S:
            wait = (_hm_to_min(WIN_S) - _hm_to_min(hm)) * 60 - now.second
            log(f"⏳ تا شروع ساعت کاری ({WIN_S}) صبر می‌کنم: {wait // 60} دقیقه")
            time.sleep(max(5, min(wait, 300)))
            continue
        t0 = time.time()
        try:
            cycle()
            n += 1
        except SystemExit:
            raise
        except Exception:
            log("❌ خطای پیش‌بینی‌نشده در این دور؛ دور بعد دوباره تلاش می‌شود")
            traceback.print_exc()
        time.sleep(max(5, every - (time.time() - t0)))


if __name__ == "__main__":
    main()
