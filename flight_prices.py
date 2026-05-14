"""
Daily airfare price tracker.

Queries SerpAPI's Google Flights endpoint for a configured set of
(origin -> destination, date-window) pairs and posts a daily summary
to Slack (via incoming webhook) and/or Gmail (via SMTP).

Secrets are read from environment variables (see README):
    SERPAPI_KEY         - required
    SLACK_WEBHOOK_URL   - optional (delivery to Slack)
    GMAIL_USER          - optional (delivery via Gmail SMTP)
    GMAIL_APP_PASSWORD  - optional (paired with GMAIL_USER)
    GMAIL_TO            - optional (defaults to GMAIL_USER if unset)

Run locally:
    SERPAPI_KEY=... python flight_prices.py --dry-run

In GitHub Actions: see .github/workflows/daily-flights.yml
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


SERPAPI_URL = "https://serpapi.com/search.json"
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


# ---------------------------- SerpAPI ----------------------------

def query_serpapi(departure_ids, arrival_id, outbound, return_date, api_key,
                  adults=1, currency="USD", timeout=45):
    params = {
        "engine": "google_flights",
        "departure_id": ",".join(departure_ids),
        "arrival_id": arrival_id,
        "outbound_date": outbound,
        "return_date": return_date,
        "currency": currency,
        "adults": adults,
        "type": 1,           # 1 = round trip
        "hl": "en",
        "api_key": api_key,
    }
    url = f"{SERPAPI_URL}?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": "flight-tracker/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        return {"error": f"HTTP {e.code}: {body}"}
    except URLError as e:
        return {"error": f"URLError: {e.reason}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def summarize_flights(payload):
    if "error" in payload:
        return {"status": "error", "error": payload["error"]}
    sm = payload.get("search_metadata", {})
    if sm.get("error"):
        return {"status": "error", "error": sm["error"]}

    candidates = []
    for bucket in ("best_flights", "other_flights"):
        for flight in (payload.get(bucket) or []):
            price = flight.get("price")
            if price is None:
                continue
            legs = flight.get("flights", []) or []
            airlines = sorted({leg.get("airline") for leg in legs if leg.get("airline")})
            stops = max(0, len(legs) - 1)
            candidates.append({
                "price": price,
                "airlines": airlines,
                "duration_minutes": flight.get("total_duration"),
                "stops": stops,
            })

    if not candidates:
        return {"status": "no_results"}

    cheapest = min(candidates, key=lambda c: c["price"])
    return {
        "status": "ok",
        "cheapest_price": cheapest["price"],
        "currency": payload.get("search_parameters", {}).get("currency", "USD"),
        "airlines": cheapest["airlines"],
        "duration_minutes": cheapest["duration_minutes"],
        "stops": cheapest["stops"],
        "options_returned": len(candidates),
        "price_insights": payload.get("price_insights", {}),
    }


# ---------------------------- Formatters ----------------------------

def fmt_money(amount, currency="USD"):
    if amount is None:
        return "—"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{amount:,.0f}"


def fmt_duration(minutes):
    if not minutes:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m"


def _stops_str(stops):
    if stops == 0:
        return "nonstop"
    return f"{stops} stop" + ("s" if stops > 1 else "")


def _group_by_route(results):
    by_route = {}
    for r in results:
        by_route.setdefault(r["route_label"], []).append(r)
    return by_route


def build_slack_text(results, config):
    today = datetime.now().strftime("%a, %b %d, %Y")
    dest = config.get("destination_name", config["destination"])
    lines = [
        f"*Daily airfare check — {today}*",
        f"_Destination: {dest} ({config['destination']}) · {config.get('adults', 1)} adult, "
        f"{config.get('cabin_class', 'economy')}, {config.get('currency', 'USD')}_",
        "",
    ]
    for route_label, rows in _group_by_route(results).items():
        lines.append(f"*{route_label}*")
        for r in rows:
            if r.get("status") == "ok":
                price = fmt_money(r["cheapest_price"], r.get("currency", "USD"))
                dur = fmt_duration(r.get("duration_minutes"))
                airlines = ", ".join(r.get("airlines") or []) or "—"
                level = (r.get("price_insights") or {}).get("price_level")
                tag = f" · _{level}_" if level else ""
                lines.append(
                    f"  • {r['window_label']}: *{price}* — {airlines}, {dur}, {_stops_str(r['stops'])}{tag}"
                )
            elif r.get("status") == "no_results":
                lines.append(f"  • {r['window_label']}: no results")
            else:
                err = (r.get("error") or "unknown error")[:140]
                lines.append(f"  • {r['window_label']}: error — {err}")
        lines.append("")

    ok_rows = [r for r in results if r.get("status") == "ok"]
    if ok_rows:
        best = min(ok_rows, key=lambda r: r["cheapest_price"])
        lines.append(
            f"_Cheapest overall today: *{fmt_money(best['cheapest_price'])}* — "
            f"{best['route_label']}, {best['window_label']}_"
        )
    return "\n".join(lines)


def build_email_html(results, config):
    today = datetime.now().strftime("%a, %b %d, %Y")
    dest = config.get("destination_name", config["destination"])
    rows_html = []
    for route_label, rows in _group_by_route(results).items():
        rows_html.append(
            f"<tr><td colspan='5' style='padding:14px 8px 4px;font-weight:600;border-top:1px solid #ddd'>"
            f"{route_label}</td></tr>"
        )
        for r in rows:
            if r.get("status") == "ok":
                price = fmt_money(r["cheapest_price"], r.get("currency", "USD"))
                dur = fmt_duration(r.get("duration_minutes"))
                airlines = ", ".join(r.get("airlines") or []) or "—"
                level = (r.get("price_insights") or {}).get("price_level") or ""
                rows_html.append(
                    "<tr>"
                    f"<td style='padding:4px 8px'>{r['window_label']}</td>"
                    f"<td style='padding:4px 8px;font-weight:600'>{price}</td>"
                    f"<td style='padding:4px 8px'>{airlines}</td>"
                    f"<td style='padding:4px 8px'>{dur}, {_stops_str(r['stops'])}</td>"
                    f"<td style='padding:4px 8px;color:#666'>{level}</td>"
                    "</tr>"
                )
            else:
                detail = r.get("error", "")[:140] if r.get("status") == "error" else "no results"
                rows_html.append(
                    f"<tr><td style='padding:4px 8px'>{r['window_label']}</td>"
                    f"<td colspan='4' style='padding:4px 8px;color:#a00'>{detail}</td></tr>"
                )
    ok_rows = [r for r in results if r.get("status") == "ok"]
    footer = ""
    if ok_rows:
        best = min(ok_rows, key=lambda r: r["cheapest_price"])
        footer = (
            f"<p style='margin-top:16px'><b>Cheapest today:</b> "
            f"{fmt_money(best['cheapest_price'])} — {best['route_label']}, {best['window_label']}</p>"
        )

    return f"""<html><body style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#222'>
<h2 style='margin-bottom:4px'>Daily airfare check — {today}</h2>
<p style='margin-top:0;color:#666'>Destination: {dest} ({config['destination']}) ·
{config.get('adults',1)} adult, {config.get('cabin_class','economy')}, {config.get('currency','USD')}</p>
<table style='border-collapse:collapse;font-size:14px'>
<thead><tr style='background:#f2f2f2'>
<th style='text-align:left;padding:6px 8px'>Window</th>
<th style='text-align:left;padding:6px 8px'>Cheapest</th>
<th style='text-align:left;padding:6px 8px'>Airlines</th>
<th style='text-align:left;padding:6px 8px'>Duration / stops</th>
<th style='text-align:left;padding:6px 8px'>Insight</th>
</tr></thead>
<tbody>
{''.join(rows_html)}
</tbody></table>
{footer}
</body></html>"""


def build_email_plain(results, config):
    # Strip Slack markdown asterisks/underscores into something plain-text friendly.
    text = build_slack_text(results, config)
    return text.replace("*", "").replace("_", "")


# ---------------------------- Delivery ----------------------------

def post_slack(webhook_url, text, timeout=20):
    body = json.dumps({"text": text}).encode("utf-8")
    req = Request(webhook_url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status == 200, resp.read().decode("utf-8", errors="replace")[:200]
    except HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_gmail(user, app_password, to_addr, subject, html_body, plain_body):
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30) as smtp:
        smtp.login(user, app_password)
        smtp.send_message(msg)


# ---------------------------- Main ----------------------------

def run_queries(config, sleep_between=1.0):
    api_key = os.environ["SERPAPI_KEY"]
    arrival = config["destination"]
    results = []
    for route in config["routes"]:
        for window in config["date_windows"]:
            payload = query_serpapi(
                departure_ids=route["departure_ids"],
                arrival_id=arrival,
                outbound=window["outbound"],
                return_date=window["return"],
                api_key=api_key,
                adults=config.get("adults", 1),
                currency=config.get("currency", "USD"),
            )
            summary = summarize_flights(payload)
            results.append({
                "route_label": route["label"],
                "departure_ids": route["departure_ids"],
                "window_label": window["label"],
                "outbound": window["outbound"],
                "return": window["return"],
                **summary,
            })
            if sleep_between:
                time.sleep(sleep_between)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print outputs to stdout; skip Slack and Gmail delivery.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Run only the first N (route x window) combos. Useful for testing.")
    args = ap.parse_args()

    if "SERPAPI_KEY" not in os.environ:
        print("ERROR: SERPAPI_KEY env var is required.", file=sys.stderr)
        sys.exit(2)

    with open(args.config) as f:
        config = json.load(f)

    if args.limit:
        # Slice the cartesian product
        flat = [(r, w) for r in config["routes"] for w in config["date_windows"]]
        flat = flat[: args.limit]
        seen_routes, seen_windows = [], []
        for r, w in flat:
            if r not in seen_routes:
                seen_routes.append(r)
            if w not in seen_windows:
                seen_windows.append(w)
        config = dict(config, routes=seen_routes, date_windows=seen_windows)

    results = run_queries(config)
    slack_text = build_slack_text(results, config)
    email_html = build_email_html(results, config)
    email_plain = build_email_plain(results, config)
    subject = f"Daily flights → {config.get('destination_name', config['destination'])} — " \
              f"{datetime.now().strftime('%a %b %d')}"

    if args.dry_run:
        print("=== SLACK PREVIEW ===")
        print(slack_text)
        print()
        print("=== EMAIL SUBJECT ===")
        print(subject)
        print()
        print("=== EMAIL PLAIN ===")
        print(email_plain)
        return

    # Slack
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        ok, detail = post_slack(slack_url, slack_text)
        print(f"[slack] sent={ok} detail={detail}")
    else:
        print("[slack] skipped (SLACK_WEBHOOK_URL not set)")

    # Gmail
    gm_user = os.environ.get("GMAIL_USER")
    gm_pw = os.environ.get("GMAIL_APP_PASSWORD")
    gm_to = os.environ.get("GMAIL_TO") or gm_user
    if gm_user and gm_pw:
        try:
            send_gmail(gm_user, gm_pw, gm_to, subject, email_html, email_plain)
            print(f"[gmail] sent to {gm_to}")
        except Exception as e:
            print(f"[gmail] FAILED: {type(e).__name__}: {e}")
            # exit non-zero so GitHub Actions marks the run failed
            sys.exit(1)
    else:
        print("[gmail] skipped (GMAIL_USER / GMAIL_APP_PASSWORD not set)")

    # Always emit results.json as a workflow artifact
    out_path = os.environ.get("RESULTS_JSON", "results.json")
    with open(out_path, "w") as f:
        json.dump({
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "config_summary": {
                "destination": config["destination"],
                "routes": [r["label"] for r in config["routes"]],
                "windows": [w["label"] for w in config["date_windows"]],
            },
            "results": results,
        }, f, indent=2)
    print(f"[json] wrote {out_path}")


if __name__ == "__main__":
    main()
