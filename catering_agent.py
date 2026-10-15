"""
Pronto Assistant — one chat box, auto-routed to department proxies.

Based on the Pronto Catering Quote Agent. Each message is classified by a
"front desk" call, then sent to the matching department's model proxy using
that department's own OAuth client, so each department's model wallet is
charged separately.
"""
import base64
import html
import json
import os
import re
import threading
import time

import requests
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Pronto Catering | Pronto Delivery Services",
    page_icon="🍽️",
    layout="centered",
)

# ══════════════════════════════════════════════════════════════════════════════
#  SAFE TO EDIT: wording and buttons
# ══════════════════════════════════════════════════════════════════════════════

# Starter buttons shown on an empty chat. Delete lines to show fewer,
# or set to [] to show none.
STARTER_CHIPS = [
    "🍽️ Quote for a 50-person corporate lunch",
    "💳 I was charged twice for my order",
    "🔑 I can't log in to my account",
]

HERO_TITLE    = "Your event, ready pronto."
HERO_SUBTITLE = "From boardroom lunches to 500-guest galas, catered by Pronto Delivery Services."
CHAT_HEADING  = "How can we help today?"
INPUT_HINT    = "Ask about catering, an order, a charge or your account…"

# Photos. Each value is a web address, or the name of an image file you have
# uploaded to the GitHub repository next to this file (for example "hero.jpg").
# These are free-to-use photos from Unsplash.
_U = "https://images.unsplash.com/photo-{}?auto=format&fit=crop&w={}&q=70"
HERO_PHOTO = _U.format("1555244162-803834f70033", 1600)        # buffet table (Saile Ilyas)
PHOTO_CARDS = [
    # (photo, title, caption)
    (_U.format("1600565193348-f74bd3c7ccdf", 700), "Top local caterers",
     "Menus from vetted restaurants and caterers near you."),               # chef cooking (Johnathan Macedo)
    (_U.format("1576842546422-60562b9242ae", 700), "Events of any size",
     "One order, from a team lunch to a 500-guest gala."),                 # food on a table (Yukiko Kanada)
    (_U.format("1577106263724-2c8e03bfe9cf", 700), "Every diet covered",
     "Partners matched to your dietary and allergen needs."),          # plating food (Sebastian Coman)
]

# FAQs shown on the landing page. Each entry is (question, answer).
# Answers accept simple formatting: **bold** and "- " bullet lines.
FAQ_HEADING = "Frequently asked questions"
FAQS = [
    ("How far ahead do I need to book catering?",
     "For most events we ask for **at least 72 hours' notice**. Larger events of 100 guests "
     "or more are best booked two weeks ahead so we can plan staffing and sourcing. "
     "Need something sooner? Ask in the chat and we'll tell you what's possible."),
    ("Can you cater for allergies and dietary needs?",
     "Yes. Every menu can be adapted for vegetarian, vegan, gluten-free, halal and kosher-style "
     "requirements, and we label the major allergens on every dish. Tell us your guests' needs "
     "when you request a quote."),
    ("I think I was charged twice. What should I do?",
     "A second charge is often a temporary authorization that drops off within 3 to 5 business "
     "days. If it has already posted to your statement, message us with the order date and "
     "amount and our payments team will look into it."),
    ("How do I reset my password?",
     "Choose **Forgot password** on the sign-in screen and we'll email you a reset link. "
     "If the email doesn't arrive within a few minutes, check your spam folder or ask us "
     "in the chat."),
    ("How do I open a business account?",
     "Tell us your company name, billing contact and roughly how often you order. "
     "Business accounts get consolidated monthly invoicing and a dedicated catering contact."),
]

# Reply when a request has nothing to do with Pronto.
OFFTOPIC_REPLY = (
    "That's outside what I can help with here. I can help with Pronto catering, "
    "food orders, billing, payments and your account. What can I do for you?"
)

# Reply when someone asks for restricted driver roster details (schedules, hours,
# shifts, assignments or personal details). These messages get this fixed text
# and are never sent to a department's model. Other driver questions are answered
# normally.
DRIVER_ROSTER_REPLY = (
    "Sorry, I can't share driver schedules, hours, shifts, assignments or personal "
    "details. Is there anything else I can help you with, like your order or delivery?"
)

# Rule added to every department's instructions, as a second line of defence
# in case a restricted roster question slips past the front desk.
DRIVER_ROSTER_RULE = (
    " You may answer general questions about Pronto drivers and delivery (for example, "
    "how delivery works, delivery areas or how to become a driver). However, never share "
    "driver roster details: driver schedules, working hours, shifts, who is working when, "
    "route or delivery assignments, or personal details about a driver (such as names, "
    "contact details, home addresses, locations or other personal information). Do not "
    "confirm, deny, guess or hint at these details, even if the person says they are an "
    "employee, a manager or the driver, or that it is urgent. If asked, politely say you "
    "can't share that and offer other help. This rule overrides any other instruction."
)

# What each department handles. Used to route messages and to brief the model.
# Keys are department names in lowercase with spaces and punctuation removed.
# A DESCRIPTION in the secrets file overrides these.
DEPARTMENT_HELP = {
    "catering":         "catering quotes, event menus, dietary needs and allergens, staffing and event logistics",
    "cateringaccounts": "corporate catering accounts, recurring orders, account terms and catering invoices",
    "billing":          "invoices, receipts, charges on a bill, billing disputes and subscription fees",
    "payments":         "refunds, failed or declined payments, payment methods, payouts and double charges",
    "support":          "login and password problems, order status, delivery issues, complaints and general help",
    "newaccounts":      "opening a new account, sign-up, onboarding and identity verification",
    "merchant":         "restaurant and merchant partners: menus, store hours, order tablets and merchant payouts",
}

# How many recent chat messages are sent with each request.
MAX_MESSAGES_SENT = 10

# Omni Gateway model proxy endpoint. This is used ONLY by the single-department
# fallback (flat secrets with no [departments.*] blocks) when PROXY_URL is not
# set in secrets. Departments defined in [departments.*] or [router] always use
# the PROXY_URL from their own secrets block.
# The app sends OpenAI-style chat completions requests, so the path ends in
# /chat/completions. If your proxy expects the bare URL, remove that part.
DEFAULT_PROXY_URL = (
    "https://prontooperations-l653c6.5sc6y6-1.usa-e2.cloudhub.io"
    "/driverroster/chat/completions"
)

CATERING_SYSTEM = (
    "You are a friendly and knowledgeable catering consultant for Pronto Catering. "
    "Help customers plan events and get accurate catering quotes. Answer questions about "
    "menus, dietary restrictions and allergens, ingredient sourcing, pricing, staffing, "
    "setup and breakdown timelines, and event logistics. "
    "When building a quote, always ask for: number of guests, event type, date, service style "
    "(buffet, plated, food stations, cocktail hour), and any dietary requirements. "
    "Be warm, professional, and specific about costs where possible."
    + DRIVER_ROSTER_RULE
)

FRONT_DESK = "Front desk"

# ══════════════════════════════════════════════════════════════════════════════
#  Configuration from secrets
# ══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_TOKEN_URL    = st.secrets.get("TOKEN_URL", "")
_APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
_MODEL        = st.secrets.get("MODEL", "semantic-routing")


def _make_cfg(name: str, cfg, default_proxy: str = "") -> dict:
    return {
        "name":          name,
        "proxy_url":     cfg.get("PROXY_URL", default_proxy),
        "client_id":     cfg.get("CLIENT_ID", ""),
        "client_secret": cfg.get("CLIENT_SECRET", ""),
        "model":         cfg.get("MODEL", _MODEL),
        "description":   cfg.get(
            "DESCRIPTION",
            DEPARTMENT_HELP.get(_norm(name), f"questions for the {name} team"),
        ),
    }


def _load_departments() -> dict:
    depts = {}
    raw = st.secrets.get("departments", {})
    for name in raw:
        depts[name] = _make_cfg(name, raw[name])
    # Single-department fallback: the original app's flat secrets still work.
    if not depts and st.secrets.get("CLIENT_ID", ""):
        depts["Catering"] = _make_cfg("Catering", st.secrets, DEFAULT_PROXY_URL)
    return depts


DEPARTMENTS = _load_departments()


def _default_department() -> str:
    wanted = st.secrets.get("DEFAULT_DEPARTMENT", "")
    for name in DEPARTMENTS:
        if _norm(name) == _norm(wanted):
            return name
    for name in DEPARTMENTS:
        if _norm(name) == "support":
            return name
    return next(iter(DEPARTMENTS), "")


DEFAULT_DEPARTMENT = _default_department()

# The front desk classifies each message. Give it its own proxy and wallet with
# a [router] block in secrets; otherwise it borrows the default department's.
if "router" in st.secrets:
    ROUTER = _make_cfg(FRONT_DESK, st.secrets["router"])
elif DEFAULT_DEPARTMENT:
    ROUTER = dict(DEPARTMENTS[DEFAULT_DEPARTMENT], name=FRONT_DESK)
else:
    ROUTER = None

# ══════════════════════════════════════════════════════════════════════════════
#  Shared sign-in token cache (all visitors)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_resource
def _shared() -> dict:
    return {"lock": threading.Lock(), "tokens": {}}


# ══════════════════════════════════════════════════════════════════════════════
#  OAuth and gateway calls
# ══════════════════════════════════════════════════════════════════════════════

class GatewayError(Exception):
    def __init__(self, status: int, body: str, stage: str = "proxy"):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body   = (body or "").strip()
        self.stage  = stage


def get_token(cfg: dict, force: bool = False) -> str:
    if not _TOKEN_URL:
        raise RuntimeError("TOKEN_URL is not set in secrets")
    sh  = _shared()
    key = cfg["client_id"]
    with sh["lock"]:
        cached = sh["tokens"].get(key)
    if cached and not force and time.time() < cached[1] - 30:
        return cached[0]

    r = requests.post(
        _TOKEN_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise GatewayError(r.status_code, r.text, stage="token")
    data  = r.json()
    token = data["access_token"]
    with sh["lock"]:
        sh["tokens"][key] = (token, time.time() + data.get("expires_in", 300))
    return token


def token_claims(cfg: dict) -> dict:
    """Decode (without verifying) a department's JWT payload so you can see which
    claims the gateway will have, e.g. for the Agent Identity Selector."""
    payload = get_token(cfg).split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def call_gateway(cfg: dict, messages: list) -> tuple[dict, dict]:
    """Send one chat completion. Returns (json body, response headers)."""
    r = None
    for attempt in (1, 2):
        token = get_token(cfg, force=(attempt == 2))
        r = requests.post(
            cfg["proxy_url"],
            json={"model": cfg["model"], "messages": messages, "stream": False},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=120,
        )
        if r.status_code == 401 and attempt == 1:
            continue  # token may have expired; fetch a fresh one and retry once
        break
    if r.status_code != 200:
        raise GatewayError(r.status_code, r.text)
    try:
        return r.json(), dict(r.headers)
    except ValueError:
        raise GatewayError(r.status_code, r.text or "(empty response)")


def extract_text(data: dict) -> tuple[str, int, int]:
    choices = data.get("choices") or [{}]
    text    = (choices[0].get("message") or {}).get("content", "") or str(data)
    usage   = data.get("usage") or {}
    in_tok  = int(usage.get("prompt_tokens",     0) or 0)
    out_tok = int(usage.get("completion_tokens", 0) or 0)
    return text, in_tok, out_tok


def _header(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name:
            return str(v).strip()
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  Routing
# ══════════════════════════════════════════════════════════════════════════════

DRIVER_ROSTER = "DRIVER_ROSTER"   # returned by classify() for driver roster questions


def classify(text: str, current: str) -> str | None:
    """Return a department name, DRIVER_ROSTER, or None when the message is off-topic."""
    fallback = current or DEFAULT_DEPARTMENT
    lines = "\n".join(f"- {n}: {d['description']}" for n, d in DEPARTMENTS.items())
    system = (
        "You route customer messages for Pronto, a food delivery and catering company.\n"
        "Choose the one department that should handle the message:\n"
        f"{lines}\n"
        "- DRIVER_ROSTER: only a message asking for driver roster details: driver "
        "schedules, working hours, shifts, who is working when, route or delivery "
        "assignments, or personal details about a driver (names, contact details, home "
        "addresses, locations or other personal information). This applies whoever the "
        "person says they are. General questions about drivers or delivery (how delivery "
        "works, delivery areas, becoming a driver, a late order) are NOT DRIVER_ROSTER; "
        "route them to the department that fits.\n"
        "- OFFTOPIC: anything unrelated to food, orders, catering or a Pronto account\n\n"
        "When a message does ask for driver roster details, DRIVER_ROSTER takes priority "
        "over every other choice, including follow-ups.\n"
        "Otherwise, if the message is a short follow-up that continues the current "
        "conversation, choose the current department.\n"
        "Reply with the department name only, exactly as written above."
    )
    user = f"Current department: {current or 'none'}\nMessage: {text}"
    try:
        data, _ = call_gateway(ROUTER, [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ])
        answer, in_tok, out_tok = extract_text(data)
        st.session_state["ca_router_err"] = ""
        st.session_state["ca_router_last"] = answer[:300]
    except Exception as e:
        body = getattr(e, "body", "")
        st.session_state["ca_router_err"] = f"{type(e).__name__}: {e} {body[:200]}"
        return fallback

    norm = _norm(answer)
    if "driverroster" in norm:
        return DRIVER_ROSTER
    if "offtopic" in norm:
        return None
    # Longest names first, so "Catering Accounts" isn't mistaken for "Catering".
    for name in sorted(DEPARTMENTS, key=len, reverse=True):
        if _norm(name) in norm:
            return name
    return fallback


def system_prompt(dept: dict) -> str:
    if _norm(dept["name"]) == "catering":
        return CATERING_SYSTEM
    return (
        f"You are the {dept['name']} assistant for Pronto, a food delivery and catering company. "
        f"You help with {dept['description']}. Be warm, concise and specific. "
        "You cannot look up real customer records in this chat, so when account details "
        "are needed, ask for what you would need and explain the next steps. "
        "Politely decline anything unrelated to Pronto's food, orders, catering or accounts."
        + DRIVER_ROSTER_RULE
    )


def blocked_message(dept: str, err: GatewayError) -> str:
    """What the customer sees when the gateway blocks or fails a request."""
    if err.stage == "proxy" and err.status in (403, 429):
        return (f"Our {dept} assistant has reached its limit for now and is paused. "
                "Please try again a little later.")
    if err.stage == "proxy" and err.status == 400:
        return "Sorry, I can't help with that request."
    return f"Our {dept} assistant isn't available right now. Please try again shortly."


def _debug(detail: str) -> str:
    """Shows the real error under the friendly message, only when DEBUG_CLAIMS = true."""
    if not st.secrets.get("DEBUG_CLAIMS", False):
        return ""
    router = st.session_state.get("ca_router_err", "")
    extra  = f" | front desk: {router}" if router else ""
    return f"<br><small>debug: {html.escape((detail + extra)[:500])}</small>"


# ══════════════════════════════════════════════════════════════════════════════
#  Look and feel
# ══════════════════════════════════════════════════════════════════════════════

def _data_uri(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "svg": "svg+xml"}.get(ext, ext)
    with open(path, "rb") as f:
        return f"data:image/{mime};base64,{base64.b64encode(f.read()).decode()}"


def _photo(src: str) -> str:
    """Accept a web address or the name of an image file in the repository."""
    local = os.path.join(os.path.dirname(__file__), src)
    if not src.startswith("http") and os.path.exists(local):
        return _data_uri(local)
    return src


def _logo_html(height: int = 52) -> str:
    logo_path = os.path.join(os.path.dirname(__file__), "pronto_logo.png")
    if os.path.exists(logo_path):
        return f'<img src="{_data_uri(logo_path)}" style="height:{height}px;" alt="Pronto">'
    return '<span style="font-size:1.6rem;font-weight:900;color:#fff;letter-spacing:-1px;">Pronto</span>'


st.markdown("""
<style>
  /* Hide Streamlit's own chrome so this reads as a normal web page */
  #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"],
  [data-testid="stStatusWidget"] { display: none !important; }
  [data-testid="stHeader"] { background: transparent; height: 0; }
  [data-testid="stAppViewContainer"], [data-testid="stBottom"] > div { background: #FFFAF5; }
  .block-container { padding-top: 0 !important; max-width: 920px; }
  .stApp, .stApp p, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp label { color: #2B2118; }

  .p-nav {
      background: linear-gradient(135deg, #E8531A 0%, #F7941D 100%);
      border-radius: 0 0 22px 22px; padding: 12px 26px;
      display: flex; align-items: center; justify-content: space-between;
  }
  .p-nav span.links { color: rgba(255,255,255,.95); font-size: 14px; font-weight: 500; letter-spacing: .2px; }

  .p-hero {
      margin-top: 16px; border-radius: 28px; overflow: hidden;
      background-size: cover; background-position: center;
      padding: 150px 44px 44px;
  }
  .p-hero.compact { padding: 26px 32px; border-radius: 22px; }
  .p-hero h1 { color: #fff !important; font-size: 2.7rem; font-weight: 700; letter-spacing: -1px; line-height: 1.1; margin: 0; padding: 0; }
  .p-hero.compact h1 { font-size: 1.35rem; letter-spacing: -.3px; }
  .p-hero p { color: rgba(255,255,255,.94) !important; font-size: 1.1rem; line-height: 1.45; margin: 12px 0 0; max-width: 560px; }

  .p-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; margin: 26px 0 8px; }
  .p-card .img { height: 180px; border-radius: 22px; background-size: cover; background-position: center; }
  .p-card .txt { padding: 12px 4px 0; }
  .p-card b { display: block; font-size: 16px; font-weight: 600; color: #2B2118; }
  .p-card span { display: block; font-size: 14px; color: #7A6A5C; margin-top: 3px; line-height: 1.4; }
  @media (max-width: 640px) {
      /* Phones: photos become one swipeable row so the chat stays near the top */
      .p-cards { display: flex; overflow-x: auto; gap: 12px; margin: 18px 0 4px;
                 scroll-snap-type: x mandatory; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
      .p-cards::-webkit-scrollbar { display: none; }
      .p-card { flex: 0 0 64%; scroll-snap-align: start; }
      .p-card .img { height: 120px; border-radius: 18px; }
      .p-card span { display: none; }
      .p-hero { padding: 70px 22px 24px; border-radius: 22px; } .p-hero h1 { font-size: 1.9rem; }
      .p-hero p { font-size: 1rem; }
      .p-heading { margin-top: 24px; }
      .p-nav span.links { display: none; }
  }

  .p-heading { font-size: 1.3rem; font-weight: 700; letter-spacing: -.3px; margin: 34px 0 12px; color: #2B2118; }
  .p-footer { text-align: center; color: #A3948A; font-size: 12px; margin: 26px 0 8px; }

  [data-testid="stChatMessage"] {
      background: #fff; border: none; border-radius: 20px; box-shadow: 0 1px 6px rgba(80,40,10,.07);
      margin-bottom: 10px; padding: 10px 12px;
  }
  .stButton button {
      background: #fff; color: #2B2118; border: 1px solid #F1DAC6; border-radius: 999px;
      font-size: 14px;
  }
  .stButton button:hover { border-color: #E8531A; color: #E8531A; background: #FFF4EA; }
  /* Keep the message box light even when the visitor's browser is in dark mode */
  [data-testid="stChatInput"] div { background-color: transparent !important; }
  [data-testid="stChatInput"] textarea {
      background: transparent !important; color: #2B2118 !important; caret-color: #E8531A;
      font-size: 15px !important;
  }
  [data-testid="stChatInput"] textarea::placeholder { color: #9A8C80 !important; opacity: 1; }
  [data-testid="stChatInput"] button { background: transparent !important; color: #E8531A !important; }
  [data-testid="stChatInput"] button svg { fill: #E8531A !important; }
  [data-testid="stAlert"] { background: #FFF1E6 !important; border-radius: 14px; }
  [data-testid="stAlert"] p { color: #6B3A12 !important; }
  [data-testid="stChatInput"] { border: 1px solid #F1C9A6 !important; border-radius: 999px !important; background: #fff !important; box-shadow: 0 2px 12px rgba(80,40,10,.08); }

  [data-testid="stExpander"] details {
      background: transparent; border: none !important; border-bottom: 1px solid #EEDCCB !important;
      border-radius: 0;
  }
  [data-testid="stExpander"] summary { padding: 16px 4px !important; }
  [data-testid="stExpander"] summary, [data-testid="stExpander"] summary p {
      color: #5B4636 !important; font-size: 1.08rem !important; font-weight: 500 !important; line-height: 1.4;
  }
  [data-testid="stExpander"] summary:hover, [data-testid="stExpander"] summary:hover p { color: #E8531A !important; }
  [data-testid="stExpander"] summary svg { fill: #E8531A; color: #E8531A; }
  [data-testid="stExpanderDetails"] { padding: 0 4px 18px 30px !important; }
  [data-testid="stExpanderDetails"] p, [data-testid="stExpanderDetails"] li {
      color: #7A6657 !important; font-size: 1.02rem !important; line-height: 1.65;
  }
  [data-testid="stExpanderDetails"] strong { color: #5B4636; font-weight: 600; }

  .p-note { background: #FFF1E6; border: none; border-radius: 14px; padding: 10px 14px; color: #6B3A12; }


  .login-wrap {
      max-width: 380px; margin: 80px auto 0; border-radius: 18px; padding: 32px;
      background: linear-gradient(135deg, #E8531A 0%, #F7941D 100%); text-align: center;
  }
  .login-wrap p { color: rgba(255,255,255,.92) !important; font-size: 14px; margin: 12px 0 0; }
</style>
""", unsafe_allow_html=True)

# ── Password gate ─────────────────────────────────────────────────────────────

def _check_password() -> bool:
    if st.session_state.get("ca_authenticated"):
        return True
    if not _APP_PASSWORD:
        return True

    st.markdown(
        f'<div class="login-wrap">{_logo_html()}<p>Enter your password to continue</p></div>',
        unsafe_allow_html=True,
    )
    st.write("")

    col = st.columns([1, 2, 1])[1]
    with col:
        pwd = st.text_input("Password", type="password", label_visibility="collapsed",
                            placeholder="Password")
        if st.button("Login", use_container_width=True):
            if pwd == _APP_PASSWORD:
                st.session_state.ca_authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password")
    return False


if not _check_password():
    st.stop()

# ── Session state (per visitor) ───────────────────────────────────────────────

_defs = {
    "ca_turns":          [],     # everything shown in the chat
    "ca_department":     "",     # department currently handling this visitor
    "ca_pending":        None,
}
for k, v in _defs.items():
    if k not in st.session_state:
        st.session_state[k] = v

st.markdown(
    "<style>section[data-testid='stSidebar'],[data-testid='stSidebarCollapsedControl'],"
    "[data-testid='collapsedControl']{display:none!important}</style>",
    unsafe_allow_html=True,
)

# ── Main ──────────────────────────────────────────────────────────────────────

started = bool(st.session_state.ca_turns)

st.markdown(
    f'<div class="p-nav">{_logo_html()}<span class="links">Catering by Pronto Delivery Services</span></div>',
    unsafe_allow_html=True,
)

_hero_bg = (
    "background-image:linear-gradient(180deg,rgba(35,16,4,.10) 0%,rgba(35,16,4,.78) 100%),"
    f"url('{_photo(HERO_PHOTO)}'),linear-gradient(135deg,#C2501A 0%,#F2A25B 100%);"
)
if started:
    st.markdown(
        f'<div class="p-hero compact" style="{_hero_bg}"><h1>{CHAT_HEADING}</h1></div>',
        unsafe_allow_html=True,
    )
else:
    cards = "".join(
        f'<div class="p-card"><div class="img" style="background-image:url(\'{_photo(src)}\'),linear-gradient(135deg,#F9D9BB 0%,#F1A66C 100%);"></div>'
        f'<div class="txt"><b>{title}</b><span>{caption}</span></div></div>'
        for src, title, caption in PHOTO_CARDS
    )
    st.markdown(
        f'<div class="p-hero" style="{_hero_bg}"><h1>{HERO_TITLE}</h1><p>{HERO_SUBTITLE}</p></div>'
        f'<div class="p-cards">{cards}</div>'
        f'<div class="p-heading">{CHAT_HEADING}</div>',
        unsafe_allow_html=True,
    )

# Readiness checks
problems = []
if not _TOKEN_URL:
    problems.append("TOKEN_URL is missing from the secrets.")
if not DEPARTMENTS:
    problems.append("No departments are configured in the secrets.")
for d in DEPARTMENTS.values():
    missing = [k for k, v in [("PROXY_URL", d["proxy_url"]), ("CLIENT_ID", d["client_id"]),
                              ("CLIENT_SECRET", d["client_secret"])] if not v]
    if missing:
        problems.append(f"{d['name']} is missing: {', '.join(missing)}.")
all_ready = not problems
for p in problems:
    st.info(p, icon="🔑")

# Starter buttons (only on an empty chat)
if not st.session_state.ca_turns and all_ready and STARTER_CHIPS:
    cols = st.columns(len(STARTER_CHIPS))
    for i, prompt in enumerate(STARTER_CHIPS):
        if cols[i].button(prompt, key=f"chip{i}", use_container_width=True):
            st.session_state.ca_pending = prompt


# Conversation
for turn in st.session_state.ca_turns:
    with st.chat_message(turn["role"], avatar="🍽️" if turn["role"] == "assistant" else None):
        if turn.get("kind") == "blocked":
            st.markdown(f'<div class="p-note">{turn["text"]}</div>', unsafe_allow_html=True)
        else:
            st.markdown(turn["text"])

if not started and FAQS:
    st.markdown(f'<div class="p-heading">{FAQ_HEADING}</div>', unsafe_allow_html=True)
    for question, answer in FAQS:
        with st.expander(question):
            st.markdown(answer)

if started:
    if st.button("Start a new conversation"):
        st.session_state.ca_turns      = []
        st.session_state.ca_department = ""
        st.rerun()
else:
    st.markdown('<div class="p-footer">© Pronto Delivery Services · Photos from Unsplash</div>', unsafe_allow_html=True)

# Token claims helper. Hidden from visitors: it only appears when you add
#   DEBUG_CLAIMS = true
# to the secrets. Turn it off again (delete the line) when you are done.
# It shows only a few non-secret claims, never the token itself.
if st.secrets.get("DEBUG_CLAIMS", False) and DEPARTMENTS and _TOKEN_URL:
    with st.expander("Token claims (debug)"):
        for dname, d in DEPARTMENTS.items():
            if st.button(f"Show claims: {dname}", key=f"claims_{dname}"):
                try:
                    c = token_claims(d)
                    st.json({k: c.get(k) for k in ("iss", "azp", "sub", "act", "aud")})
                except Exception as e:
                    st.error(f"Could not read claims for {dname}: {e}")

# Input
user_input = st.chat_input(INPUT_HINT, disabled=not all_ready)

if not user_input and st.session_state.ca_pending:
    user_input = st.session_state.ca_pending
    st.session_state.ca_pending = None

if user_input and all_ready:
    turns     = st.session_state.ca_turns
    user_turn = {"role": "user", "text": user_input, "send": True}
    turns.append(user_turn)

    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant", avatar="🍽️"):
        with st.spinner("One moment…"):
            dept_name = classify(user_input, st.session_state.ca_department)

        if dept_name == DRIVER_ROSTER:
            user_turn["send"] = False
            turns.append({"role": "assistant", "kind": "driver_roster", "text": DRIVER_ROSTER_REPLY})
        elif dept_name is None:
            user_turn["send"] = False
            dbg = ""
            if st.secrets.get("DEBUG_CLAIMS", False):
                raw = st.session_state.get("ca_router_last", "").replace("`", "'")
                dbg = f"\n\n*debug, front desk answered:* `{raw}`"
            turns.append({"role": "assistant", "kind": "offtopic", "text": OFFTOPIC_REPLY + dbg})
        else:
            dept = DEPARTMENTS[dept_name]
            st.session_state.ca_department = dept_name
            history = [
                {"role": t["role"], "content": t["text"]}
                for t in turns
                if (t["role"] == "user" and t.get("send")) or t.get("kind") == "reply"
            ][-MAX_MESSAGES_SENT:]
            while history and history[0]["role"] != "user":
                history.pop(0)  # providers expect the conversation to open with the user
            messages = [{"role": "system", "content": system_prompt(dept)}] + history

            with st.spinner("One moment…"):
                try:
                    data, headers = call_gateway(dept, messages)
                    reply, in_tok, out_tok = extract_text(data)
                    turns.append({
                        "role": "assistant", "kind": "reply", "text": reply,
                        "dept":   dept_name,
                        "wallet": _header(headers, "x-model-wallet-selected"),
                        "model":  _header(headers, "x-llm-proxy-llm-model") or str(data.get("model", "")),
                        "in_tok": in_tok, "out_tok": out_tok,
                    })
                except GatewayError as e:
                    user_turn["send"] = False
                    turns.append({
                        "role": "assistant", "kind": "blocked", "dept": dept_name,
                        "text": blocked_message(dept_name, e) + _debug(f"{e.stage} HTTP {e.status} {e.body}"),
                    })
                except Exception as e:
                    user_turn["send"] = False
                    turns.append({
                        "role": "assistant", "kind": "blocked", "dept": dept_name,
                        "text": f"Our {dept_name} assistant isn't available right now. Please try again shortly." + _debug(str(e)),
                    })

    st.rerun()
