"""Terminal Macro & Plan d'Exécution : EUR/USD & Or
Installation : pip install -r requirements.txt
Lancement    : streamlit run terminal_macro.py
Secrets (.streamlit/secrets.toml) : FRED_API_KEY, TG_TOKEN, TG_CHAT_ID
Outil d'aide à la décision : ne constitue pas un conseil financier.
"""
import io
import json
import os
from datetime import datetime, timezone

import feedparser
import pandas as pd
import plotly.graph_objects as go
import pytz
import requests
import streamlit as st
import yfinance as yf
from fredapi import Fred

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

st.set_page_config(page_title="Terminal Macro : EUR/USD & Or", layout="wide")
UA = {"User-Agent": "Mozilla/5.0"}
LEVELS_FILE = "levels.json"
tz_local = pytz.timezone("Africa/Libreville")
tz_ny = pytz.timezone("America/New_York")
now_utc = datetime.now(pytz.utc)
now_local = now_utc.astimezone(tz_local)

TICKERS = {
    "EUR/USD": "EURUSD=X",
    "Or (Futures)": "GC=F",
    "Indice Dollar (DXY)": "DX-Y.NYB",
    "Taux US 10 Ans": "^TNX",
    "Indice VIX (Stress)": "^VIX",
}
EUR, GOLD, DXY, TNX, VIX = list(TICKERS)

FEEDS = {
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "BCE": "https://www.ecb.europa.eu/rss/press.html",
    "FXStreet": "https://www.fxstreet.com/rss/news",
}
KEYWORDS = [
    "gold", "euro", "eur", "dollar", "usd", "fed", "ecb", "inflation",
    "rate", "powell", "lagarde", "treasury", "yield", "cpi", "payroll", "nfp",
]


# ------------------------------------------------------------------ utilitaires
def stretch(fn, *args, **kwargs):
    """Pleine largeur, compatible anciennes et nouvelles versions de Streamlit."""
    try:
        return fn(*args, width="stretch", **kwargs)
    except Exception:
        return fn(*args, use_container_width=True, **kwargs)


def secret(name):
    try:
        return st.secrets.get(name, os.environ.get(name, ""))
    except Exception:
        return os.environ.get(name, "")


def clip(x, lo=-2.0, hi=2.0):
    return max(lo, min(hi, x))


def forex_closed(t):
    """Fermé du vendredi 17 h au dimanche 17 h, heure de New York."""
    ny = t.astimezone(tz_ny)
    wd, hr = ny.weekday(), ny.hour
    return wd == 5 or (wd == 4 and hr >= 17) or (wd == 6 and hr < 17)


def session_open(tzname, start, end):
    t = now_utc.astimezone(pytz.timezone(tzname))
    return (not forex_closed(now_utc)) and t.weekday() < 5 and start <= t.hour < end


def send_telegram(token, chat_id, text):
    if not (token and chat_id):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=8,
        )
        return r.ok
    except requests.RequestException:
        return False


health = {}  # voyants du menu latéral

# ------------------------------------------------------------------ en-tête
jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
mois = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août",
    "Septembre", "Octobre", "Novembre", "Décembre",
]
date_fr = f"{jours[now_local.weekday()]} {now_local.day} {mois[now_local.month - 1]} {now_local.year}"


def ico(is_open):
    return "🟢 Ouverte" if is_open else "⚪ Fermée"


st.title("🏛️ Terminal Macro & Plan d'Exécution : EUR/USD & Or")
st.info(
    f"📅 **{date_fr}** | 🕒 Libreville : **{now_local:%H:%M:%S} (WAT)** | 🌍 Asie "
    f"{ico(session_open('Asia/Tokyo', 9, 18))} • Londres "
    f"{ico(session_open('Europe/London', 8, 17))} • "
    f"New York {ico(session_open('America/New_York', 8, 17))}"
    + ("  | 🔒 Marché forex fermé (week-end)" if forex_closed(now_utc) else "")
)

# ------------------------------------------------------------------ barre latérale
with st.sidebar:
    st.header("⚙️ Paramètres")
    timeframe = st.radio("Horizon d'analyse :", ["Intraday (1h)", "Swing (1j)"], index=1)
    auto_refresh = st.checkbox("Rafraîchissement auto (60 s)", value=True)
    st.divider()
    st.header("🔑 Clés & alertes")
    st.caption("Laisse vide pour utiliser les secrets enregistrés.")
    # Les secrets ne sont jamais préremplis dans les champs.
    fred_key = st.text_input("Clé API FRED", type="password") or secret("FRED_API_KEY")
    enable_tg = st.checkbox("Notifications Telegram")
    tg_token = st.text_input("Bot Token", type="password") or secret("TG_TOKEN")
    tg_chat = st.text_input("Chat ID", type="password") or secret("TG_CHAT_ID")
    if st.button("🔄 Actualiser maintenant"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    health_box = st.container()

if auto_refresh and st_autorefresh:
    st_autorefresh(interval=60_000, key="auto_refresh")
elif auto_refresh:
    st.sidebar.caption("Installe `streamlit-autorefresh` pour l'actualisation automatique.")


def notify(key, text):
    sent = st.session_state.setdefault("sent_alerts", set())
    if enable_tg and key not in sent and send_telegram(tg_token, tg_chat, text):
        sent.add(key)


# ------------------------------------------------------------------ données
intraday = timeframe.startswith("Intraday")


def safe(fn, *args):
    """Retourne (données, erreur). Une exception n'est pas mise en cache."""
    try:
        return fn(*args), None
    except Exception as e:
        msg = str(e)
        for a in args:  # ne jamais afficher une clé dans un message d'erreur
            if a:
                msg = msg.replace(str(a), "***")
        return None, msg[:80]


@st.cache_data(ttl=60, show_spinner=False)
def load_prices(period, interval):
    data, errors = {}, {}
    for name, tk in TICKERS.items():
        try:
            df = yf.Ticker(tk).history(period=period, interval=interval)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                errors[name] = "vide"
                continue
            df.index = pd.to_datetime(df.index)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            data[name] = df
        except Exception as e:
            errors[name] = str(e)[:60]
    return data, errors


@st.cache_data(ttl=900, show_spinner=False)
def load_calendar():
    r = requests.get(
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=UA, timeout=10
    )
    r.raise_for_status()
    out = []
    for ev in r.json():
        if ev.get("country") in ("USD", "EUR") and ev.get("impact") in ("High", "Medium"):
            out.append({
                "dt": datetime.fromisoformat(ev["date"]).astimezone(pytz.utc),
                "cur": ev["country"],
                "high": ev["impact"] == "High",
                "title": ev.get("title", ""),
                "fc": ev.get("forecast") or "-",
                "prev": ev.get("previous") or "-",
            })
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def load_fred(key):
    f = Fred(api_key=key)
    errors = []

    def last(code, n=1):
        try:
            s = f.get_series(code).dropna()
            return float(s.iloc[-n]), s.index[-1]
        except Exception as e:
            errors.append(str(e))
            return None, None

    ry, _ = last("DFII10")
    ry_prev, _ = last("DFII10", 6)
    fed, _ = last("FEDFUNDS")
    ecb, _ = last("ECBDFR")
    fr, d_fr = last("IRLTLT01FRM156N")
    de, _ = last("IRLTLT01DEM156N")
    res = {
        "ry": ry,
        "ry_prev": ry_prev,
        "fed": fed,
        "ecb": ecb,
        "oat_bund": (fr - de) if fr is not None and de is not None else None,
        "oat_date": d_fr.strftime("%m/%Y") if d_fr is not None else None,
    }
    if all(v is None for v in res.values()):
        raise ValueError(errors[0] if errors else "séries introuvables")
    return res


@st.cache_data(ttl=43200, show_spinner=False)
def load_cot():
    r = requests.get("https://www.cftc.gov/dea/newcot/deafut.txt", headers=UA, timeout=15)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), header=None, low_memory=False)
    names = df[0].astype(str).str.strip()
    out = {}
    for key, pat in (
        ("gold", "GOLD - COMMODITY EXCHANGE INC."),
        ("euro", "EURO FX - CHICAGO MERCANTILE EXCHANGE"),
    ):
        row = df[names.str.startswith(pat)]
        if not row.empty:  # colonnes : 8 = non-commerciaux long, 9 = short
            out[key] = float(row.iloc[0, 8]) - float(row.iloc[0, 9])
            out["date"] = str(row.iloc[0, 2]).strip()
    if not out:
        raise ValueError("contrats introuvables")
    return out


@st.cache_data(ttl=600, show_spinner=False)
def load_news():
    items, status = [], {}
    for src, url in FEEDS.items():
        try:
            r = requests.get(url, headers=UA, timeout=10)
            r.raise_for_status()
            fd = feedparser.parse(r.content)
            for e in fd.entries[:20]:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                items.append({
                    "src": src,
                    "title": e.get("title", "").strip(),
                    "link": e.get("link", ""),
                    "dt": datetime(*ts[:6], tzinfo=timezone.utc) if ts else None,
                })
            status[src] = len(fd.entries)
        except Exception:
            status[src] = None
    items.sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items, status


view, view_err = load_prices("5d" if intraday else "6mo", "1h" if intraday else "1d")
daily, daily_err = load_prices("6mo", "1d")
cal, cal_err = safe(load_calendar)
fred, fred_err = safe(load_fred, fred_key) if fred_key else (None, "clé absente")
cot, cot_err = safe(load_cot)
news, news_status = load_news()


def dclose(name):
    d = daily.get(name)
    if d is None or d.empty:
        return None
    s = d["Close"].dropna().copy()
    s.index = pd.to_datetime(s.index.strftime("%Y-%m-%d"))
    return s[~s.index.duplicated(keep="last")]


def atr(name, n=14):
    d = daily.get(name)
    if d is None or len(d) < n + 1:
        return None
    pc = d["Close"].shift(1)
    tr = pd.concat(
        [d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1
    ).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def price(name):
    for src in (view, daily):
        if name in src and not src[name].empty:
            return float(src[name]["Close"].iloc[-1])
    return None


def freshness(df):
    last = df.index[-1].astimezone(pytz.utc)
    if forex_closed(now_utc):
        return "⚪", "marché fermé"
    if intraday:
        age = (now_utc - last).total_seconds() / 60
        icon = "🟢" if age <= 90 else "🟠" if age <= 240 else "🔴"
        return icon, f"retard {age:.0f} min"
    days = (now_utc.date() - last.date()).days
    limit = 1 if now_utc.weekday() in (1, 2, 3, 4) else 3
    return ("🟢" if days <= limit else "🔴"), f"dernière bougie il y a {days} j"


# ------------------------------------------------------------------ 1. briefing
st.subheader("1. 📝 Briefing de recherche (pré-analyse)")
with st.expander("Consulter / éditer le briefing du jour", expanded=False):
    st.text_area(
        "Synthèse préparatoire (exemple à adapter, non mis à jour automatiquement) :",
        height=110,
        value="• Contexte : inflation US, discours de la Fed et de la BCE.\n"
              "• Or : achats des banques centrales, géopolitique, sensibilité aux taux réels US.\n"
              "• EUR/USD : écart de politique monétaire Fed/BCE, spread France-Allemagne.",
    )
st.divider()

# ------------------------------------------------------------------ 2. calendrier
st.subheader("2. 📅 Calendrier économique du jour (EUR / USD)")
if cal is None:
    st.warning(f"Calendrier indisponible ({cal_err}).")
    health["Calendrier"] = ("🔴", cal_err)
else:
    today = [e for e in cal if e["dt"].astimezone(tz_local).date() == now_local.date()]
    health["Calendrier"] = ("🟢", f"{len(today)} annonce(s) aujourd'hui")
    for e in today:
        mins = (e["dt"] - now_utc).total_seconds() / 60
        if 0 < mins <= (60 if e["high"] else 30):
            hhmm = e["dt"].astimezone(tz_local).strftime("%H:%M")
            txt = (f"{'🚨' if e['high'] else '⚠️'} Annonce dans {int(mins)} min "
                   f"({hhmm} WAT) : [{e['cur']}] {e['title']}")
            (st.error if e["high"] else st.warning)(txt)
            notify(f"cal|{e['dt']}|{e['title']}", txt)
    if today:
        stretch(st.dataframe, pd.DataFrame([{
            "Heure (WAT)": e["dt"].astimezone(tz_local).strftime("%H:%M"),
            "Devise": e["cur"],
            "Impact": "🔴 Élevé (alerte 1h)" if e["high"] else "🟠 Moyen (alerte 30 min)",
            "Annonce": e["title"],
            "Prévision": e["fc"],
            "Précédent": e["prev"],
        } for e in sorted(today, key=lambda x: x["dt"])]), hide_index=True)
    else:
        st.caption("Aucune annonce majeure USD/EUR aujourd'hui.")
st.divider()


# ------------------------------------------------------------------ 3. cotations & corrélations
def clean_levels(df):
    df = df.reindex(columns=["Actif", "Type", "Niveau"])
    for c in ("Actif", "Type"):  # texte, même quand le tableau est vide
        df[c] = df[c].astype(object).where(df[c].notna(), None)
    df["Niveau"] = pd.to_numeric(df["Niveau"], errors="coerce").astype(float)
    return df.reset_index(drop=True)


if "levels" not in st.session_state:
    try:
        with open(LEVELS_FILE, encoding="utf-8") as f:
            st.session_state["levels"] = clean_levels(pd.DataFrame(json.load(f)))
    except Exception:
        st.session_state["levels"] = clean_levels(pd.DataFrame())

st.subheader(f"3. 🔗 Cotations & corrélations ({timeframe}) — synchro {now_local:%H:%M:%S}")
cols = st.columns(len(TICKERS))
for col, name in zip(cols, TICKERS):
    df = view.get(name)
    if df is None or len(df) < 2:
        col.metric(name, "N/D")
        continue
    last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
    col.metric(
        name,
        f"{last:.4f}" if name == EUR else f"{last:,.2f}",
        f"{(last / prev - 1) * 100:+.2f} %",
    )
    col.caption(f"Bougie {df.index[-1].astimezone(tz_local):%d/%m %H:%M} WAT")

main = [view[n] for n in (EUR, GOLD) if n in view]
if len(main) < 2:
    health["Cotations"] = ("🔴", "EUR/USD ou Or manquant")
else:
    states = [freshness(d) for d in main]
    health["Cotations"] = max(states, key=lambda s: ["🟢", "⚪", "🟠", "🔴"].index(s[0]))
if view_err:
    st.caption("Sources en erreur : " + ", ".join(f"{k} ({v})" for k, v in view_err.items()))


def corr20(a, b):
    sa, sb = dclose(a), dclose(b)
    if sa is None or sb is None:
        return None
    j = pd.concat([sa.pct_change(), sb.pct_change()], axis=1, join="inner").dropna().tail(20)
    return float(j.iloc[:, 0].corr(j.iloc[:, 1])) if len(j) >= 10 else None


c1, c2 = st.columns(2)
for col, (a, b, label) in zip(
    (c1, c2), ((EUR, DXY, "Dollar vs EUR/USD"), (GOLD, TNX, "Taux US vs Or"))
):
    c = corr20(a, b)
    if c is None or pd.isna(c):
        col.metric(f"Corrélation 20j — {label}", "N/D")
    else:
        verdict = ("✅ relation inverse classique" if c < -0.5
                   else "↔️ relation faible" if c < -0.3 else "⚠️ découplage")
        col.metric(f"Corrélation 20j — {label}", f"{c:+.2f}", verdict, delta_color="off")


def make_chart(name):
    df = view.get(name)
    if df is None:
        st.caption("Données indisponibles.")
        return
    fig = go.Figure(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"], low=df["Low"],
        close=df["Close"], name=name,
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["Close"].rolling(20).mean(), name="MM20", line=dict(width=1)
    ))
    for _, r in st.session_state["levels"].iterrows():
        if r["Actif"] == name and pd.notna(r["Niveau"]):
            fig.add_hline(y=float(r["Niveau"]), line_dash="dot", annotation_text=str(r["Type"]))
    fig.update_layout(
        height=380, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10)
    )
    stretch(st.plotly_chart, fig)


for tab, name in zip(st.tabs(["EUR/USD", "Or"]), (EUR, GOLD)):
    with tab:
        make_chart(name)
st.divider()

# ------------------------------------------------------------------ 4. tendance
st.subheader("4. 🧭 Tendance de fond (score macro)")


def sma_gap(name, n=20):
    s = dclose(name)
    if s is None or len(s) < n:
        return None
    return (s.iloc[-1] / s.tail(n).mean() - 1) * 100


gap_dxy = sma_gap(DXY)
vix = dclose(VIX)
fd = fred or {}
ct = cot or {}
rows = []  # (actif, facteur, score, lecture)
if gap_dxy is not None:
    sc = clip(-gap_dxy / 0.5)
    rows += [
        (EUR, "Dollar (DXY)", sc, f"DXY {gap_dxy:+.2f} % vs MM20"),
        (GOLD, "Dollar (DXY)", sc, f"DXY {gap_dxy:+.2f} % vs MM20"),
    ]
if fd.get("ecb") is not None and fd.get("fed") is not None:
    sp = fd["ecb"] - fd["fed"]
    rows.append((EUR, "Écart BCE − Fed", clip(sp / 1.0), f"{sp:+.2f} pt"))
if fd.get("oat_bund") is not None:
    rows.append((EUR, "Spread OAT-Bund", clip(-(fd["oat_bund"] - 0.5) / 0.25),
                 f"{fd['oat_bund']:.2f} pt ({fd['oat_date']})"))
if "euro" in ct:
    rows.append((EUR, "COT euro (spéculateurs)", clip(ct["euro"] / 60000),
                 f"net {ct['euro']:+,.0f} ({ct['date']})"))
if fd.get("ry") is not None and fd.get("ry_prev") is not None:
    dlt = fd["ry"] - fd["ry_prev"]
    rows.append((GOLD, "Taux réels US (5 obs.)", clip(-dlt / 0.10),
                 f"{fd['ry']:.2f} % ({dlt:+.2f})"))
if "gold" in ct:
    rows.append((GOLD, "COT or (spéculateurs)", clip(ct["gold"] / 150000),
                 f"net {ct['gold']:+,.0f} ({ct['date']})"))
if vix is not None and not vix.empty:
    rows.append((GOLD, "Stress (VIX)", clip((vix.iloc[-1] - 18) / 6, -1, 1),
                 f"VIX {vix.iloc[-1]:.1f}"))

df_f = pd.DataFrame(rows, columns=["Actif", "Facteur", "Score", "Lecture"])
scores = {}
cs = st.columns(2)
for col, name in zip(cs, (EUR, GOLD)):
    sub = df_f[df_f["Actif"] == name]
    if sub.empty:
        col.metric(f"Biais {name}", "Données insuffisantes")
        continue
    avg = float(sub["Score"].mean())
    scores[name] = avg
    label = "Haussier 📈" if avg >= 0.5 else "Baissier 📉" if avg <= -0.5 else "Neutre ↔️"
    col.metric(f"Biais {name}", label, f"score {avg:+.2f} / 2 ({len(sub)} facteurs)",
               delta_color="off")
    top = sub.reindex(sub["Score"].abs().sort_values(ascending=False).index).head(3)
    col.caption("Justification : " + " ; ".join(
        f"{r.Facteur} ({r.Lecture}) "
        f"{'soutient' if r.Score > 0 else 'pèse sur' if r.Score < 0 else 'est neutre pour'} {name}"
        for r in top.itertuples()
    ))
if not df_f.empty:
    stretch(st.dataframe, df_f.assign(Score=df_f["Score"].astype(float).round(2)),
            hide_index=True)
st.caption("Score de −2 (baissier) à +2 (haussier), moyenne des facteurs disponibles ; "
           "seuils de normalisation à ajuster dans le code. Les facteurs sans donnée sont "
           "exclus. COT : publication hebdomadaire.")
health["FRED"] = (("🟢", "séries chargées") if fred
                  else ("🔴" if fred_key else "🟠", fred_err or ""))
health["COT"] = (("🟢", f"rapport du {cot.get('date', '?')}") if cot
                 else ("🔴", cot_err))
st.divider()

# ------------------------------------------------------------------ 5. niveaux & scénarios
st.subheader("5. 🎯 Niveaux clés & scénarios « Si… Alors »")
st.caption("Niveaux personnalisés (modifiables) + pivots automatiques calculés sur la dernière séance.")
version = st.session_state.setdefault("levels_version", 0)
edited = stretch(
    st.data_editor,
    st.session_state["levels"],
    num_rows="dynamic",
    key=f"levels_editor_{version}",
    column_config={
        "Actif": st.column_config.SelectboxColumn(options=[EUR, GOLD]),
        "Type": st
