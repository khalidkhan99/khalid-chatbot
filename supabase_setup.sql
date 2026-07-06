-- ============================================================
-- Khalid Chatbot — Supabase Database Setup
-- Ye poora script copy karke Supabase Dashboard -> SQL Editor
-- mein paste karein aur "Run" dabayein. Ek hi baar chalana hai.
-- ============================================================

-- 1) Users table (login/signup ke liye)
create table if not exists users (
    username text primary key,
    password_hash text not null,
    salt text not null,
    created_at timestamptz default now()
);

-- 2) Usage table (free-message counter, guests + logged-in dono ke liye)
create table if not exists usage (
    identifier text primary key,
    message_count integer default 0
);

-- 3) Chat history table (sirf logged-in users ki chats yahan save hoti hain)
create table if not exists chat_history (
    id bigint generated always as identity primary key,
    username text not null,
    role text not null,
    content text not null,
    created_at timestamptz default now()
);

-- Chat history ko username ke hisaab se jaldi load karne ke liye index
create index if not exists idx_chat_history_username on chat_history (username);

-- ============================================================
-- IMPORTANT: Row Level Security (RLS)
-- Hum server-side (Streamlit app) se Supabase "service_role" key
-- use kar rahe hain, is liye RLS disabled rakhna simplest hai.
-- Agar aap chahen to enable karke apni policies bhi likh sakte hain.
-- ============================================================
alter table users disable row level security;
alter table usage disable row level security;
alter table chat_history disable row level security;
