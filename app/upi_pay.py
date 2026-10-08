"""UPI app-redirect page (/pay) for the Goflix bot's PhonePe / GPay / Paytm / Navi buttons.

Put this file at  app/upi_pay.py  in the STREAM repo (the Dockerfile only copies the app/ folder),
then in app/main.py add these two lines RIGHT AFTER  app = FastAPI(...)  and BEFORE any @app.get routes
(main.py ends with catch-all routes like /{hashid} that would otherwise answer /pay with "Not Found"):

    from app.upi_pay import router as upi_pay_router
    app.include_router(upi_pay_router)

Environment variables on the stream service
-------------------------------------------
UPI_ID           same UPI id the bot uses, e.g. name@okaxis        (required)
UPI_PAYEE_NAME   shown in the UPI app, default "Goflix"             (optional)
GOFLIX_BOT_TOKEN the token of the main Goflix bot (the one that sends the QR / pay buttons)
  or PAY_SECRET  64-hex key derived from that token (keeps the token itself off this server)

NOTE: this server's own BOT_TOKEN belongs to the stream bot. It is NOT used here on purpose
(it is a different bot, so links signed by the Goflix bot would never match it).

Get PAY_SECRET by running, anywhere:   python upi_pay.py "<Goflix bot token>"

How the buttons behave
-----------------------
Telegram only allows https:// links on buttons, so a tap always hits this server first.
* Android + a specific app (PhonePe/GPay/Paytm/Navi): instant HTTP redirect straight into
  that app, no page shown. If the app isn't installed, Chrome lands on the page below
  instead (with a short "couldn't open it" note and the other app buttons).
* iPhone: a tiny page that opens the app by itself (plus buttons as a backup).
* Anything else (desktop, no app chosen): the page with all the app buttons.

Two kinds of pages
------------------
/open/<app> and /open  - the Telegram app buttons. They only OPEN the UPI app (nothing is passed to it, so
                         the app shows no payment-link warning); the user then taps Scan QR in the app and scans the QR by hand.
/pay                   - signed payment links (amount + payee pre-filled). Still works for older messages.

The bot signs every /pay link (amount + note) with that key. This page refuses anything
whose signature doesn't match, and the payee is ALWAYS the UPI_ID from this server's
environment, so the page can't be used to send money anywhere else or to change the amount.
"""
import hashlib
import hmac
import html
import json
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

UPI_ID = os.environ.get("UPI_ID", "").strip()
UPI_PAYEE_NAME = os.environ.get("UPI_PAYEE_NAME", "Goflix").strip()
_BOT_TOKEN = os.environ.get("GOFLIX_BOT_TOKEN", "")   # not stripped: must match the bot byte-for-byte
_PAY_SECRET = os.environ.get("PAY_SECRET", "").strip()

_VPA_RE = re.compile(r"^[A-Za-z0-9.\-_]{2,256}@[A-Za-z][A-Za-z0-9]{1,64}$")
_MAX_AMOUNT = Decimal("100000")

UPI_APPS = {
    "phonepe": {"name": "PhonePe",    "package": "com.phonepe.app",                         "ios": "phonepe://pay", "scheme": "phonepe", "open_path": "pay"},
    "gpay":    {"name": "Google Pay", "package": "com.google.android.apps.nbu.paisa.user", "ios": "tez://upi/pay", "scheme": "tez", "open_path": "upi/pay"},
    "paytm":   {"name": "Paytm",      "package": "net.one97.paytm",                         "ios": "paytmmp://upi/pay", "scheme": "paytmmp", "open_path": "upi/pay"},
    "navi":    {"name": "Navi",       "package": "com.naviapp",                             "ios": "navipay://pay", "scheme": "navipay", "open_path": "pay"},
    # extra apps, shown on the "Other UPI apps" page. Delete any line you don't want listed.
    "bhim":    {"name": "BHIM",       "package": "in.org.npci.upiapp",                      "ios": None},
    "amazon":  {"name": "Amazon Pay", "package": "in.amazon.mShop.android.shopping",        "ios": None},
    "cred":    {"name": "CRED",       "package": "com.dreamplug.androidapp",                "ios": None},
    "mobikwik": {"name": "MobiKwik",  "package": "com.mobikwik_new",                        "ios": None},
    "freecharge": {"name": "Freecharge", "package": "com.freecharge.android",              "ios": None},
}


# ───────────────────────────── signing (identical to the bot) ─────────────────────────────
def derive_secret(bot_token: str) -> bytes:
    return hashlib.sha256(("goflix-upi-pay:" + (bot_token or "")).encode()).digest()


def _load_secret():
    """bytes, or None when nothing usable is configured (the page then answers 404)."""
    if _PAY_SECRET:
        try:
            key = bytes.fromhex(_PAY_SECRET)
            return key if len(key) == 32 else None
        except ValueError:
            return None
    if _BOT_TOKEN:
        return derive_secret(_BOT_TOKEN)
    return None


_SECRET = _load_secret()


def pay_enabled() -> bool:
    return bool(_SECRET and UPI_ID and _VPA_RE.match(UPI_ID))


def format_amount(amount) -> str:
    """'15' -> '15.00'. Raises ValueError for junk, zero, negative or huge values."""
    try:
        d = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid amount: {amount!r}")
    if not d.is_finite() or d <= 0 or d > _MAX_AMOUNT:
        raise ValueError(f"amount out of range: {amount!r}")
    return str(d.quantize(Decimal("0.01")))


def sign_pay(amount, note: str) -> str:
    msg = f"{format_amount(amount)}|{note}".encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()[:24]


def verify_pay_sig(amount, note: str, sig: str) -> bool:
    if not _SECRET:
        return False
    try:
        return hmac.compare_digest(sign_pay(amount, note), sig or "")
    except ValueError:
        return False


# ───────────────────────────── page rendering (same as the bot's) ─────────────────────────────
def build_upi_query(amount, note: str = "") -> str:
    """pa=<id>&pn=<name>&am=<amount>&cu=INR&tn=<note>   (the part after upi://pay?)"""
    params = [("pa", UPI_ID), ("pn", UPI_PAYEE_NAME or "Goflix"), ("am", format_amount(amount)), ("cu", "INR")]
    if note:
        params.append(("tn", note[:50]))
    return "&".join(f"{k}={quote(v, safe='@.-_')}" for k, v in params)


def detect_platform(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "android"
    if any(k in ua for k in ("iphone", "ipad", "ipod")):
        return "ios"
    return "desktop"


def app_url(app_key: str, platform: str, query: str, fallback: str = None) -> str:
    meta = UPI_APPS[app_key]
    if platform == "android":
        fb = f";S.browser_fallback_url={quote(fallback, safe='')}" if fallback else ""
        return f"intent://pay?{query}#Intent;scheme=upi;package={meta['package']}{fb};end"
    if platform == "ios" and meta.get("ios"):
        return f"{meta['ios']}?{query}"
    return f"upi://pay?{query}"


def render_pay_page(amount, note: str, app_key: str = None, user_agent: str = "", failed: bool = False) -> str:
    amt = format_amount(amount)
    query = build_upi_query(amt, note)
    platform = detect_platform(user_agent)
    list_mode = app_key not in UPI_APPS            # "Other UPI apps": no app chosen -> show them all
    # iPhone can only open apps that have their own URL scheme; the rest would all open the same generic link
    keys = [k for k in UPI_APPS if platform != "ios" or UPI_APPS[k].get("ios")]
    if not list_mode:
        if app_key in keys:
            keys.remove(app_key)
        keys.insert(0, app_key)
    esc = lambda s: html.escape(s, quote=True)
    generic = esc("upi://pay?" + query)

    buttons = []
    if list_mode:
        # the system chooser lists EVERY UPI app installed on this phone (Android) - the real "show all apps"
        buttons.append(f'<a class="btn main" href="{generic}">All UPI apps on this phone</a>')
        buttons.append('<p class="hint">Or open a specific app:</p>')
    for i, k in enumerate(keys):
        cls = "btn main" if (not list_mode and i == 0) else "btn"
        buttons.append(f'<a class="{cls}" href="{esc(app_url(k, platform, query))}">Open {esc(UPI_APPS[k]["name"])}</a>')
    if not list_mode:
        buttons.append(f'<a class="btn alt" href="{generic}">Any other UPI app</a>')

    if failed and not list_mode:
        hint = (f"Couldn't open {UPI_APPS[app_key]['name']}. Make sure it is installed on this phone, "
                "or pick another UPI app above.")
    elif platform == "desktop":
        hint = "This page is meant for your phone. On a computer, scan the QR code shown in Telegram instead."
    else:
        hint = ("Nothing opened? Open this page in Chrome (⋮ → Open in browser) and tap the button again. "
                "The app must be installed on this phone.")
    auto = ""
    if not list_mode and platform in ("android", "ios") and not failed:
        target = json.dumps(app_url(app_key, platform, query)).replace("</", "<\\/")
        auto = f"<script>setTimeout(function(){{window.location.href={target};}},250);</script>"
    pick = '<p class="pick">Choose your UPI app</p>' if list_mode else ""

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Pay ₹{esc(amt)} — Goflix</title><style>"
        "body{margin:0;background:#0f1115;color:#f2f4f8;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
        "display:flex;justify-content:center}main{width:100%;max-width:420px;padding:28px 18px;text-align:center}"
        "h1{font-size:32px;margin:6px 0}.sub{color:#9aa3b2;margin:0 0 22px}"
        ".pick{font-size:18px;font-weight:600;margin:0 0 14px}"
        ".btn{display:block;margin:10px 0;padding:15px;border-radius:12px;background:#1d2330;color:#fff;"
        "text-decoration:none;font-weight:600;font-size:17px;border:1px solid #2c3446}"
        ".btn.main{background:#2f6bff;border-color:#2f6bff}.btn.alt{background:transparent;color:#9aa3b2}"
        ".hint{color:#9aa3b2;font-size:13px;line-height:1.5;margin:18px 0 0}"
        f'</style></head><body><main><h1>Pay ₹{esc(amt)}</h1><p class="sub">{esc(note)}</p>{pick}'
        + "".join(buttons)
        + f'<p class="hint">{esc(hint)}</p>'
        '<p class="hint">After paying, go back to Telegram, tap “I\'ve paid” and send the payment screenshot.</p>'
        + auto + "</main></body></html>"
    )


# ───────────────────── "just open the app" (no payment details) ─────────────────────
# Used by the Telegram app buttons: the app is only OPENED, nothing (amount / UPI id) is passed to it, so the
# app shows none of its "payment from a link" warnings. The user then taps Scan QR in the app and scans by hand.
def play_store_url(app_key: str) -> str:
    return f"https://play.google.com/store/apps/details?id={UPI_APPS[app_key]['package']}"


def open_app_url(app_key: str, platform: str):
    """Link that only opens the app (no amount, no UPI id). None when this device can't do it (e.g. a computer).
    Uses the link each app itself registers (scheme + its own path); a bare "scheme://" matches nothing in
    PhonePe / GPay and makes Paytm show "App upgrade required"."""
    meta = UPI_APPS[app_key]
    scheme = meta.get("scheme")
    if platform == "android":
        store = play_store_url(app_key)
        if not scheme:                              # no known app link: the Play Store page has an "Open" button
            return store
        path = meta.get("open_path", "")
        return f"intent://{path}#Intent;scheme={scheme};package={meta['package']};S.browser_fallback_url={quote(store, safe='')};end"
    if platform == "ios" and scheme:
        return f"{scheme}://{meta.get('open_path', '')}"
    return None


def render_open_page(app_key: str = None, user_agent: str = "", failed: bool = False) -> str:
    platform = detect_platform(user_agent)
    chosen = app_key if app_key in UPI_APPS else None
    esc = lambda s: html.escape(s, quote=True)
    keys = [k for k in UPI_APPS if platform == "android" or UPI_APPS[k].get("scheme")]
    if chosen and chosen in keys:
        keys.remove(chosen)
        keys.insert(0, chosen)
    buttons = []
    for i, k in enumerate(keys):
        href = open_app_url(k, platform)
        if not href:
            continue
        cls = "btn main" if (chosen and i == 0) else "btn"
        buttons.append(f'<a class="{cls}" href="{esc(href)}">Open {esc(UPI_APPS[k]["name"])}</a>')
    if platform == "desktop":
        hint = "This page is meant for your phone. On a computer, scan the QR code shown in Telegram with your phone."
    elif failed and chosen:
        hint = (f"Couldn't open {UPI_APPS[chosen]['name']} automatically. Tap its button above, "
                "or open it from your phone's home screen.")
    else:
        hint = "If the app shows a message about this link, ignore it: go to its home screen and tap Scan QR."
    auto = ""
    first = open_app_url(chosen, platform) if chosen else None
    if first and not failed and platform == "ios":
        auto = ("<script>setTimeout(function(){window.location.href="
                + json.dumps(first).replace("</", "<\\/") + ";},250);</script>")
    store_link = ""
    if chosen and platform == "android":
        store_link = f'<a class="btn alt" href="{esc(play_store_url(chosen))}">Still not opening? Open from Google Play</a>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Open your UPI app — Goflix</title><style>"
        "body{margin:0;background:#0f1115;color:#f2f4f8;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
        "display:flex;justify-content:center}main{width:100%;max-width:420px;padding:28px 18px;text-align:center}"
        "h1{font-size:26px;margin:6px 0}.sub{color:#9aa3b2;margin:0 0 22px;line-height:1.5}"
        ".btn{display:block;margin:10px 0;padding:15px;border-radius:12px;background:#1d2330;color:#fff;"
        "text-decoration:none;font-weight:600;font-size:17px;border:1px solid #2c3446}"
        ".btn.main{background:#2f6bff;border-color:#2f6bff}.btn.alt{background:transparent;color:#9aa3b2;font-weight:500;font-size:15px}"
        ".hint{color:#9aa3b2;font-size:13px;line-height:1.5;margin:18px 0 0}"
        '</style></head><body><main><h1>Open your UPI app</h1>'
        '<p class="sub">Tap the button to open the app. Then tap <b>Scan QR</b> and scan the payment QR from Telegram.</p>'
        + "".join(buttons) + store_link
        + f'<p class="hint">{esc(hint)}</p>'
        '<p class="hint">After paying, go back to Telegram, tap “I\'ve paid” and send the payment screenshot.</p>'
        + auto + "</main></body></html>"
    )


# ───────────────────────────── the route ─────────────────────────────
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")


def _self_url(request: Request, **extra):
    """Absolute https URL of this same page (behind Koyeb's proxy request.url can say http).
    Returns None when the Host header looks wrong, so a crafted header can never end up in the link."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    if proto not in ("http", "https"):
        proto = "https"
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not _HOST_RE.match(host):
        return None
    q = dict(request.query_params)
    q.update(extra)
    return f"{proto}://{host}{request.url.path}?{urlencode(q)}"


router = APIRouter()


@router.api_route("/pay", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def upi_pay_page(
    request: Request,
    am: str = Query(""),
    tn: str = Query(""),
    sig: str = Query(""),
    app: str = Query(""),
    nr: str = Query(""),          # "1" = we already tried the app and it didn't open
):
    if not pay_enabled() or not verify_pay_sig(am, tn, sig):
        raise HTTPException(status_code=404, detail="Not Found")
    ua = request.headers.get("user-agent", "")
    headers = {"Cache-Control": "no-store"}
    try:
        # Android + chosen app: jump straight into the app, no page in between.
        if request.method == "GET" and not nr and app in UPI_APPS and detect_platform(ua) == "android":
            target = app_url(app, "android", build_upi_query(am, tn), fallback=_self_url(request, nr="1"))
            return RedirectResponse(target, status_code=302, headers=headers)
        page = render_pay_page(am, tn, app, ua, failed=bool(nr))
    except ValueError:
        raise HTTPException(status_code=404, detail="Not Found")
    return HTMLResponse(page, headers=headers)


async def _open_response(request: Request, app_key: str, failed: bool):
    if app_key and app_key not in UPI_APPS:
        raise HTTPException(status_code=404, detail="Not Found")
    ua = request.headers.get("user-agent", "")
    # Always a page (not a silent redirect): it tries to open the app by itself AND shows a big button, because
    # a real tap is what Chrome / Telegram's browser trust most. If the app doesn't answer, the user lands on
    # its Play Store page, which has an Open button.
    return HTMLResponse(render_open_page(app_key or None, ua, failed), headers={"Cache-Control": "no-store"})


@router.api_route("/open", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def upi_open_list(request: Request):
    return await _open_response(request, "", False)


@router.api_route("/open/{app_key}", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def upi_open_app(request: Request, app_key: str, failed: str = Query("")):
    return await _open_response(request, app_key, bool(failed))


@router.get("/pay/check", include_in_schema=False)
async def upi_pay_check(am: str = Query(""), tn: str = Query(""), sig: str = Query("")):
    """Diagnostics only: says WHY /pay answers 404. Shows no secrets. Safe to delete later."""
    if _PAY_SECRET:
        secret = "ok (from PAY_SECRET)" if _SECRET else "PAY_SECRET is set but is not a valid 64-hex value"
    elif _BOT_TOKEN:
        secret = "ok (from GOFLIX_BOT_TOKEN)"
    else:
        secret = "MISSING - add GOFLIX_BOT_TOKEN to this service"
    if not _SECRET:
        signature = "cannot check: no signing secret on this server"
    elif not (am and sig):
        signature = "not checked (add am=, tn=, sig= from the button link)"
    else:
        signature = "match" if verify_pay_sig(am, tn, sig) else \
            "MISMATCH - GOFLIX_BOT_TOKEN here is not the same token the bot is using"
    return JSONResponse({
        "page": "upi_pay.py is live on this server",
        "UPI_ID": "ok" if (UPI_ID and _VPA_RE.match(UPI_ID)) else "MISSING or invalid - add UPI_ID to this service",
        "secret": secret,
        "signature": signature,
    })


if __name__ == "__main__":
    # python upi_pay.py "<Goflix bot token>"   -> prints the PAY_SECRET value to put on this service
    import sys
    if len(sys.argv) != 2:
        sys.exit('usage: python upi_pay.py "<BOT_TOKEN>"')
    print(derive_secret(sys.argv[1]).hex())
