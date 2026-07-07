import streamlit as st
import uuid
import hashlib
import secrets
import re
from openai import OpenAI
from supabase import create_client
from streamlit_cookies_manager import EncryptedCookieManager

# ============================================================
# CONFIG
# ============================================================
FREE_MESSAGE_LIMIT_GUEST = 3     # bina login walon ke liye
FREE_MESSAGE_LIMIT_USER = 10     # login karne walon ke liye bonus
GROK_MODEL = "grok-3"
GROK_BASE_URL = "https://api.x.ai/v1"

st.set_page_config(page_title="Khalid Chatbot", page_icon="🤖", layout="centered")

# ============================================================
# CUSTOM STYLING — dark theme with purple accents
# ============================================================
st.markdown("""
<style>
    .stApp {
        background: #0a0a10;
    }
    section[data-testid="stSidebar"] {
        background: #111017;
        border-right: 1px solid rgba(255,255,255,0.07);
    }
    h1, h2, h3 {
        font-family: 'Sora', 'Inter', sans-serif !important;
    }
    /* Primary buttons (Login, Send, Create account) */
    .stButton > button {
        background: linear-gradient(135deg, #6d5bff, #8b6bff);
        color: #fff;
        border: none;
        border-radius: 10px;
        font-weight: 600;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 6px 18px rgba(109,91,255,0.35);
        color: #fff;
    }
    /* Chat bubbles */
    div[data-testid="stChatMessage"] {
        border-radius: 16px;
        padding: 4px 8px;
    }
    /* Input box */
    .stChatInput textarea, .stChatInput input {
        border-radius: 999px !important;
    }
    /* Alert/info boxes */
    div[data-testid="stAlert"] {
        border-radius: 12px;
    }
</style>
""", unsafe_allow_html=True)

# ============================================================
# SUPABASE CLIENT (permanent storage — redeploy-proof)
# ============================================================
@st.cache_resource
def init_supabase():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase = init_supabase()

# ============================================================
# COOKIE MANAGER (guest identity ke liye)
# ============================================================
cookies = EncryptedCookieManager(
    prefix="khalid_chatbot/",
    password=st.secrets["COOKIE_PASSWORD"],
)
if not cookies.ready():
    st.stop()

if "guest_id" not in cookies or not cookies["guest_id"]:
    cookies["guest_id"] = str(uuid.uuid4())
    cookies.save()

GUEST_ID = cookies["guest_id"]

# ============================================================
# PASSWORD HASHING (demo-grade, salted PBKDF2)
# ============================================================
def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return pwd_hash, salt

def verify_password(password, salt, stored_hash):
    pwd_hash, _ = hash_password(password, salt)
    return pwd_hash == stored_hash

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_valid_email(email):
    return bool(EMAIL_PATTERN.match(email))

# ============================================================
# USERS (Supabase table: users)
# ============================================================
def create_user(username, email, password):
    existing_username = supabase.table("users").select("username").eq("username", username).execute()
    if existing_username.data:
        return False, "Ye username pehle se maujood hai."
    existing_email = supabase.table("users").select("email").eq("email", email).execute()
    if existing_email.data:
        return False, "Is email se pehle hi ek account bana hua hai."
    pwd_hash, salt = hash_password(password)
    supabase.table("users").insert({
        "username": username, "email": email, "password_hash": pwd_hash, "salt": salt
    }).execute()
    return True, "Account ban gaya! Ab login karein."

def authenticate_user(email, password):
    """Email + password se login. Success par (True, username) return karta hai."""
    res = supabase.table("users").select("username, password_hash, salt").eq("email", email).execute()
    if not res.data:
        return False, None
    row = res.data[0]
    if verify_password(password, row["salt"], row["password_hash"]):
        return True, row["username"]
    return False, None

# ============================================================
# USAGE TRACKING (Supabase table: usage)
# ============================================================
def get_message_count(identifier):
    res = supabase.table("usage").select("message_count").eq("identifier", identifier).execute()
    if res.data:
        return res.data[0]["message_count"]
    supabase.table("usage").insert({"identifier": identifier, "message_count": 0}).execute()
    return 0

def increment_message_count(identifier, current_count):
    supabase.table("usage").update(
        {"message_count": current_count + 1}
    ).eq("identifier", identifier).execute()

# ============================================================
# CHAT HISTORY (Supabase table: chat_history) — logged-in users only
# ============================================================
def load_chat_history(username):
    res = (
        supabase.table("chat_history")
        .select("role, content")
        .eq("username", username)
        .order("id")
        .execute()
    )
    return res.data or []

def save_message(username, role, content):
    supabase.table("chat_history").insert({
        "username": username, "role": role, "content": content
    }).execute()

def clear_chat_history(username):
    supabase.table("chat_history").delete().eq("username", username).execute()

# ============================================================
# SESSION STATE
# ============================================================
if "username" not in st.session_state:
    st.session_state.username = cookies.get("logged_in") or None
if "messages" not in st.session_state:
    st.session_state.messages = (
        load_chat_history(st.session_state.username) if st.session_state.username else []
    )
if "user_api_key" not in st.session_state:
    st.session_state.user_api_key = None

IS_LOGGED_IN = st.session_state.username is not None
IDENTIFIER = st.session_state.username if IS_LOGGED_IN else GUEST_ID
FREE_LIMIT = FREE_MESSAGE_LIMIT_USER if IS_LOGGED_IN else FREE_MESSAGE_LIMIT_GUEST

# Message count ko session mein cache karte hain — sirf identifier badalne
# (login/logout) par hi dobara Supabase se fetch hota hai. Isse har
# message/click par network round-trip nahi hoti aur UI turant respond karta hai.
if "message_count" not in st.session_state or st.session_state.get("_count_identifier") != IDENTIFIER:
    st.session_state.message_count = get_message_count(IDENTIFIER)
    st.session_state._count_identifier = IDENTIFIER

message_count = st.session_state.message_count

# ============================================================
# SIDEBAR — AUTH + STATUS
# ============================================================
with st.sidebar:
    st.markdown("### 🤖 Khalid Chatbot")
    st.caption("Portfolio demo — powered by Grok (x.ai)")
    st.divider()

    if IS_LOGGED_IN:
        st.success(f"👤 Logged in as **{st.session_state.username}**")
        if st.button("Logout"):
            cookies["logged_in"] = ""
            cookies.save()
            st.session_state.username = None
            st.session_state.messages = []
            st.rerun()
    else:
        with st.expander("🔐 Login / Sign up (optional)", expanded=False):
            tab_login, tab_signup = st.tabs(["Login", "Sign up"])

            with tab_login:
                le = st.text_input("Email", key="login_email", placeholder="you@example.com").strip()
                lp = st.text_input("Password", type="password", key="login_pass")
                if st.button("Login", key="login_btn"):
                    if not le or not lp:
                        st.error("Email aur password dono zaroori hain.")
                    elif not is_valid_email(le):
                        st.error("Sahi email format daalein (jaisay you@example.com).")
                    else:
                        ok, found_username = authenticate_user(le, lp)
                        if ok:
                            cookies["logged_in"] = found_username
                            cookies.save()
                            st.session_state.username = found_username
                            st.session_state.messages = load_chat_history(found_username)
                            st.rerun()
                        else:
                            st.error("Galat email ya password.")

            with tab_signup:
                su = st.text_input("Display name", key="signup_user", placeholder="e.g. Khalid").strip()
                se = st.text_input("Email", key="signup_email", placeholder="you@example.com").strip()
                sp = st.text_input("Choose a password", type="password", key="signup_pass")
                if st.button("Create account", key="signup_btn"):
                    if len(su) < 3:
                        st.error("Display name kam az kam 3 characters ka ho.")
                    elif not is_valid_email(se):
                        st.error("Sahi email format daalein (jaisay you@example.com).")
                    elif len(sp) < 4:
                        st.error("Password kam az kam 4 characters ka ho.")
                    else:
                        ok, msg = create_user(su, se, sp)
                        (st.success if ok else st.error)(msg)

        st.caption("Bina login ke bhi chat kar sakte hain — login sirf extra messages aur history save karne ke liye hai.")

    st.divider()

    remaining = max(0, FREE_LIMIT - message_count)
    if st.session_state.user_api_key:
        st.success("✅ Using your own API key — unlimited messages")
    else:
        st.info(f"🎁 Free messages left: **{remaining} / {FREE_LIMIT}**")
        if not IS_LOGGED_IN:
            st.caption("Login karke free messages 10 tak badhayein.")

    if st.button("🔄 Start new chat"):
        if IS_LOGGED_IN:
            clear_chat_history(st.session_state.username)
        st.session_state.messages = []
        st.rerun()

    st.divider()
    st.caption("Made by Khalid · [x.ai](https://x.ai) API")

# ============================================================
# HEADER
# ============================================================
st.title("🤖 Khalid Chatbot")
st.caption("Ask me anything — I'm here to help.")

# ============================================================
# SHOW CHAT HISTORY
# ============================================================
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ============================================================
# DECIDE WHICH API KEY TO USE
# ============================================================
def get_active_api_key():
    if message_count < FREE_LIMIT:
        return st.secrets.get("GROK_API_KEY", None)
    return st.session_state.user_api_key

active_key = get_active_api_key()

# ============================================================
# IF LIMIT REACHED AND NO USER KEY YET -> ASK FOR IT
# ============================================================
if active_key is None:
    st.warning(
        "⚠️ Free message limit khatam ho gayi hai. "
        "Chat continue karne ke liye apni **Grok (x.ai) API key** daalein."
    )
    with st.form("api_key_form"):
        entered_key = st.text_input(
            "Apni x.ai API key yahan paste karein",
            type="password",
            placeholder="xai-xxxxxxxxxxxxxxxxxxxxx",
        )
        submitted = st.form_submit_button("Save & Continue")
        if submitted and entered_key.strip():
            st.session_state.user_api_key = entered_key.strip()
            st.rerun()

    if not IS_LOGGED_IN:
        st.info("💡 Tip: Login/Sign up karke 10 free messages tak paayein — bina API key ke.")
    st.info("🔑 API key nahi hai? [x.ai console](https://console.x.ai) se free bana sakte hain.")
    st.stop()

# ============================================================
# CHAT INPUT
# ============================================================
user_input = st.chat_input("Type a message...")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)
    if IS_LOGGED_IN:
        save_message(st.session_state.username, "user", user_input)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("Thinking...")

        try:
            client = OpenAI(api_key=active_key, base_url=GROK_BASE_URL)
            response = client.chat.completions.create(
                model=GROK_MODEL,
                messages=[{"role": m["role"], "content": m["content"]} for m in st.session_state.messages],
            )
            reply = response.choices[0].message.content
        except Exception as e:
            reply = f"⚠️ Kuch ghalat ho gaya: `{e}`\n\nApni API key check kar lein ya thodi dair baad try karein."

        placeholder.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
    if IS_LOGGED_IN:
        save_message(st.session_state.username, "assistant", reply)

    if message_count < FREE_LIMIT:
        increment_message_count(IDENTIFIER, message_count)
        st.session_state.message_count += 1

    st.rerun()

st.caption("⚠️ Ye ek AI chatbot hai — galtiyan kar sakta hai. Zaroori maloomat ko khud verify kar lein.")
