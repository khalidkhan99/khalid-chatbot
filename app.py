import streamlit as st
import streamlit.components.v1 as components
import uuid
import hashlib
import secrets
import re
import requests
import time
from openai import OpenAI
from supabase import create_client
from streamlit_cookies_manager import EncryptedCookieManager

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# ============================================================
# CONFIG
# ============================================================
FREE_MESSAGE_LIMIT_GUEST = 3     # bina login walon ke liye
FREE_MESSAGE_LIMIT_USER = 10     # login karne walon ke liye bonus
GROQ_MODEL = "openai/gpt-oss-20b"      # Groq ke free tier pe fast + capable model
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MAX_DOC_CHARS = 8000   # simple context-stuffing limit (chhote/medium documents ke liye)

SYSTEM_PROMPT = (
    "You are Khalid Chatbot, a friendly and helpful AI assistant. "
    "Keep answers concise and direct — 2 to 5 sentences by default, in plain prose. "
    "Do NOT use a table or list unless the user explicitly asks for one, or the content "
    "is genuinely a step-by-step process or a comparison of 3+ items with multiple attributes. "
    "A few news headlines or search results should be summarized in a short paragraph, "
    "not a table. "
    "If a message below starts with 'LIVE SEARCH RESULTS:', use that information "
    "to answer the user's question accurately in plain prose, and mention it's based on "
    "a live search — do not say you lack real-time access in that case. "
    "If a message below starts with 'DOCUMENT CONTEXT:', the user has uploaded a document — "
    "use it to answer questions about it when relevant."
)

# ---------- Live web search (Tavily) ----------
LIVE_KEYWORDS = [
    "price", "current", "latest", "today", "now", "live", "news",
    "score", "weather", "kitna", "abhi", "aaj",
]
LIVE_KEYWORD_PATTERN = re.compile(r"\b(" + "|".join(LIVE_KEYWORDS) + r")\b", re.IGNORECASE)

def needs_live_search(text):
    return bool(LIVE_KEYWORD_PATTERN.search(text))

def web_search_tavily(query, max_results=3):
    api_key = st.secrets.get("TAVILY_API_KEY", None)
    if not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": max_results, "search_depth": "basic"},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        lines = []
        for r in results:
            title = r.get("title", "")
            content = (r.get("content", "") or "")[:300]
            url = r.get("url", "")
            lines.append(f"- {title}: {content} (source: {url})")
        return "LIVE SEARCH RESULTS:\n" + "\n".join(lines)
    except Exception:
        return None

# ---------- Document extraction (simple RAG via context-stuffing) ----------
def extract_text_from_file(uploaded_file):
    name = uploaded_file.name.lower()
    if name.endswith(".pdf"):
        if PdfReader is None:
            return None
        reader = PdfReader(uploaded_file)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    else:
        text = uploaded_file.read().decode("utf-8", errors="ignore")
    return text.strip()

st.set_page_config(page_title="Khalid Chatbot", page_icon="🤖", layout="centered")

# ============================================================
# LIGHT STYLING — just polish, no background overrides
# (Full dark theme is handled properly via .streamlit/config.toml)
# ============================================================
st.markdown("""
<style>
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
</style>
""", unsafe_allow_html=True)

# ============================================================
# SUPABASE CLIENT
# ============================================================
@st.cache_resource
def init_supabase():
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase = init_supabase()

# ============================================================
# COOKIE MANAGER
# ============================================================
cookies = EncryptedCookieManager(prefix="khalid_chatbot/", password=st.secrets["COOKIE_PASSWORD"])
if not cookies.ready():
    st.stop()

if "guest_id" not in cookies or not cookies["guest_id"]:
    cookies["guest_id"] = str(uuid.uuid4())
    cookies.save()

GUEST_ID = cookies["guest_id"]

# ============================================================
# PASSWORD HASHING
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
# USERS
# ============================================================
def create_user(username, email, password):
    if supabase.table("users").select("username").eq("username", username).execute().data:
        return False, "Ye username pehle se maujood hai."
    if supabase.table("users").select("email").eq("email", email).execute().data:
        return False, "Is email se pehle hi ek account bana hua hai."
    pwd_hash, salt = hash_password(password)
    supabase.table("users").insert({
        "username": username, "email": email, "password_hash": pwd_hash, "salt": salt
    }).execute()
    return True, "Account ban gaya! Ab login karein."

def authenticate_user(email, password):
    res = supabase.table("users").select("username, password_hash, salt").eq("email", email).execute()
    if not res.data:
        return False, None
    row = res.data[0]
    if verify_password(password, row["salt"], row["password_hash"]):
        return True, row["username"]
    return False, None

# ============================================================
# USAGE TRACKING
# ============================================================
def get_message_count(identifier):
    res = supabase.table("usage").select("message_count").eq("identifier", identifier).execute()
    if res.data:
        return res.data[0]["message_count"]
    supabase.table("usage").insert({"identifier": identifier, "message_count": 0}).execute()
    return 0

def increment_message_count(identifier, current_count):
    supabase.table("usage").update({"message_count": current_count + 1}).eq("identifier", identifier).execute()

# ============================================================
# CONVERSATIONS (multi-thread chat history) — logged-in users only
# ============================================================
def create_conversation(username, title="New chat"):
    res = supabase.table("conversations").insert({"username": username, "title": title}).execute()
    return res.data[0]["id"]

def list_conversations(username):
    res = (
        supabase.table("conversations")
        .select("id, title, created_at")
        .eq("username", username)
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []

def rename_conversation(conversation_id, title):
    supabase.table("conversations").update({"title": title}).eq("id", conversation_id).execute()

def delete_conversation(conversation_id):
    supabase.table("chat_history").delete().eq("conversation_id", conversation_id).execute()
    supabase.table("conversations").delete().eq("id", conversation_id).execute()

def load_conversation_messages(conversation_id):
    res = (
        supabase.table("chat_history")
        .select("role, content")
        .eq("conversation_id", conversation_id)
        .order("id")
        .execute()
    )
    return res.data or []

def save_message(conversation_id, username, role, content):
    supabase.table("chat_history").insert({
        "conversation_id": conversation_id, "username": username, "role": role, "content": content
    }).execute()

# ============================================================
# FEEDBACK (thumbs up/down on assistant replies)
# ============================================================
def save_feedback(identifier, message_content, rating):
    supabase.table("feedback").insert({
        "identifier": identifier, "message_content": message_content[:2000], "rating": rating
    }).execute()

# ============================================================
# SESSION STATE
# ============================================================
if "username" not in st.session_state:
    st.session_state.username = cookies.get("logged_in") or None
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None
if "messages" not in st.session_state:
    if st.session_state.username:
        convs = list_conversations(st.session_state.username)
        if convs:
            st.session_state.conversation_id = convs[0]["id"]
            st.session_state.messages = load_conversation_messages(convs[0]["id"])
        else:
            st.session_state.messages = []
    else:
        st.session_state.messages = []
if "user_api_key" not in st.session_state:
    st.session_state.user_api_key = cookies.get("user_api_key") or None
if "feedback_given" not in st.session_state:
    st.session_state.feedback_given = {}
if "document_context" not in st.session_state:
    st.session_state.document_context = None
if "doc_name" not in st.session_state:
    st.session_state.doc_name = None

IS_LOGGED_IN = st.session_state.username is not None
IDENTIFIER = st.session_state.username if IS_LOGGED_IN else GUEST_ID
FREE_LIMIT = FREE_MESSAGE_LIMIT_USER if IS_LOGGED_IN else FREE_MESSAGE_LIMIT_GUEST

if "message_count" not in st.session_state or st.session_state.get("_count_identifier") != IDENTIFIER:
    st.session_state.message_count = get_message_count(IDENTIFIER)
    st.session_state._count_identifier = IDENTIFIER

message_count = st.session_state.message_count

# ============================================================
# SIDEBAR
# ============================================================
with st.sidebar:
    st.markdown("### 🤖 Khalid Chatbot")
    st.caption("Portfolio demo — powered by Groq (Llama)")
    st.divider()

    if IS_LOGGED_IN:
        st.success(f"👤 Logged in as **{st.session_state.username}**")
        if st.button("Logout"):
            cookies["logged_in"] = ""
            cookies.save()
            st.session_state.username = None
            st.session_state.conversation_id = None
            st.session_state.messages = []
            st.session_state.feedback_given = {}
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
                            convs = list_conversations(found_username)
                            if convs:
                                st.session_state.conversation_id = convs[0]["id"]
                                st.session_state.messages = load_conversation_messages(convs[0]["id"])
                            else:
                                st.session_state.conversation_id = None
                                st.session_state.messages = []
                            st.session_state.feedback_given = {}
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

        st.caption("Bina login ke bhi chat kar sakte hain — login sirf extra messages, history aur multiple chats ke liye hai.")

    st.divider()

    remaining = max(0, FREE_LIMIT - message_count)
    if st.session_state.user_api_key:
        st.success("✅ Using your own API key — unlimited messages")
        if st.button("🗑️ Remove saved key"):
            st.session_state.user_api_key = None
            cookies["user_api_key"] = ""
            cookies.save()
            st.rerun()
    else:
        st.info(f"🎁 Free messages left: **{remaining} / {FREE_LIMIT}**")
        if not IS_LOGGED_IN:
            st.caption("Login karke free messages 10 tak badhayein.")

    if st.button("➕ New chat"):
        st.session_state.conversation_id = None
        st.session_state.messages = []
        st.session_state.feedback_given = {}
        st.rerun()

    # ---------- Multiple chat threads (logged-in users only) ----------
    if IS_LOGGED_IN:
        st.divider()
        st.markdown("**🕘 Your chats**")
        convs = list_conversations(st.session_state.username)
        if not convs:
            st.caption("Abhi koi purani chat nahi hai.")
        for c in convs:
            col1, col2 = st.columns([5, 1])
            label = c["title"] or "New chat"
            is_active = st.session_state.conversation_id == c["id"]
            with col1:
                if st.button(("🟣 " if is_active else "") + label, key=f"conv_{c['id']}"):
                    st.session_state.conversation_id = c["id"]
                    st.session_state.messages = load_conversation_messages(c["id"])
                    st.session_state.feedback_given = {}
                    st.rerun()
            with col2:
                if st.button("🗑️", key=f"del_{c['id']}"):
                    delete_conversation(c["id"])
                    if is_active:
                        st.session_state.conversation_id = None
                        st.session_state.messages = []
                    st.rerun()

    # ---------- Document chat (simple RAG via context-stuffing) ----------
    st.divider()
    st.markdown("**📄 Chat with a document**")
    uploaded_file = st.file_uploader("PDF ya TXT upload karein", type=["pdf", "txt"], key="doc_uploader")
    if uploaded_file is not None and uploaded_file.name != st.session_state.doc_name:
        text = extract_text_from_file(uploaded_file)
        if text:
            st.session_state.document_context = text[:MAX_DOC_CHARS]
            st.session_state.doc_name = uploaded_file.name
            st.rerun()
        else:
            st.error("File se text nahi nikal saka. Ek doosri file try karein.")
    if st.session_state.document_context:
        st.success(f"✅ Active: {st.session_state.doc_name}")
        st.caption(f"Sirf pehle ~{MAX_DOC_CHARS} characters use hote hain (basic context mode).")
        if st.button("Remove document"):
            st.session_state.document_context = None
            st.session_state.doc_name = None
            st.rerun()

    st.divider()
    st.caption("Made by Khalid · [Groq](https://groq.com) API")

# ============================================================
# HEADER
# ============================================================
st.title("🤖 Khalid Chatbot")
st.caption("Ask me anything — I'm here to help.")

# ============================================================
# SHOW CHAT HISTORY (with feedback buttons on assistant replies)
# ============================================================
for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            if i in st.session_state.feedback_given:
                st.caption(f"Feedback diya: {st.session_state.feedback_given[i]}")
            else:
                c1, c2, _ = st.columns([1, 1, 10])
                if c1.button("👍", key=f"up_{i}"):
                    save_feedback(IDENTIFIER, msg["content"], "up")
                    st.session_state.feedback_given[i] = "👍"
                    st.rerun()
                if c2.button("👎", key=f"down_{i}"):
                    save_feedback(IDENTIFIER, msg["content"], "down")
                    st.session_state.feedback_given[i] = "👎"
                    st.rerun()

# Page ko turant sab se naye message tak scroll kar dete hain
if st.session_state.messages:
    components.html(
        """
        <script>
            var mainSection = window.parent.document.querySelector('section.main');
            if (mainSection) { mainSection.scrollTo(0, mainSection.scrollHeight); }
            window.parent.scrollTo(0, window.parent.document.body.scrollHeight);
        </script>
        """,
        height=0,
    )

# ============================================================
# DECIDE WHICH API KEY TO USE
# ============================================================
def get_active_api_key():
    if st.session_state.user_api_key:
        return st.session_state.user_api_key
    if message_count < FREE_LIMIT:
        return st.secrets.get("GROQ_API_KEY", None)
    return None

active_key = get_active_api_key()

if active_key is None:
    st.warning("⚠️ Free message limit khatam ho gayi hai. Chat continue karne ke liye apni **Groq API key** daalein.")
    with st.form("api_key_form"):
        entered_key = st.text_input("Apni Groq API key yahan paste karein", type="password", placeholder="gsk_xxxxxxxxxxxxxxxxxxxxx")
        submitted = st.form_submit_button("Save & Continue")
        if submitted and entered_key.strip():
            st.session_state.user_api_key = entered_key.strip()
            cookies["user_api_key"] = entered_key.strip()
            cookies.save()
            st.rerun()

    if not IS_LOGGED_IN:
        st.info("💡 Tip: Login/Sign up karke 10 free messages tak paayein — bina API key ke.")
    st.info("🔑 API key nahi hai? [console.groq.com](https://console.groq.com) se free bana sakte hain.")
    st.stop()

# ============================================================
# CHAT INPUT
# ============================================================
user_input = st.chat_input("Type a message...")

if user_input:
    # Logged-in user ki agar koi active conversation nahi hai, to naya thread bana dete hain
    if IS_LOGGED_IN and st.session_state.conversation_id is None:
        title = user_input[:40] + ("…" if len(user_input) > 40 else "")
        st.session_state.conversation_id = create_conversation(st.session_state.username, title)

    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)
    if IS_LOGGED_IN:
        save_message(st.session_state.conversation_id, st.session_state.username, "user", user_input)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("Thinking...")
        reply = ""

        try:
            client = OpenAI(api_key=active_key, base_url=GROQ_BASE_URL)

            api_messages = [{"role": "system", "content": SYSTEM_PROMPT}]

            if needs_live_search(user_input):
                search_context = web_search_tavily(user_input)
                if search_context:
                    api_messages.append({"role": "system", "content": search_context})

            if st.session_state.document_context:
                api_messages.append({
                    "role": "system",
                    "content": f"DOCUMENT CONTEXT (from '{st.session_state.doc_name}'):\n\n{st.session_state.document_context}",
                })

            api_messages += [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]

            stream = client.chat.completions.create(model=GROQ_MODEL, messages=api_messages, stream=True)
            full_reply = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    # Groq ke chunks kabhi bade hote hain (poore words/sentences ek sath) —
                    # isliye har chunk ko chhote tukdon mein todte hain taake smooth
                    # typewriter effect nazar aaye, bade jumps mein nahi.
                    for j in range(0, len(delta), 3):
                        full_reply += delta[j:j + 3]
                        placeholder.markdown(full_reply + "▌")
                        time.sleep(0.012)
            placeholder.markdown(full_reply)
            reply = full_reply
        except Exception as e:
            reply = f"⚠️ Kuch ghalat ho gaya: `{e}`\n\nApni API key check kar lein ya thodi dair baad try karein."
            placeholder.markdown(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
    if IS_LOGGED_IN:
        save_message(st.session_state.conversation_id, st.session_state.username, "assistant", reply)

    if not st.session_state.user_api_key and message_count < FREE_LIMIT:
        increment_message_count(IDENTIFIER, message_count)
        st.session_state.message_count += 1

    st.rerun()

st.caption("⚠️ Ye ek AI chatbot hai — galtiyan kar sakta hai. Zaroori maloomat ko khud verify kar lein.")
