"""
InfraLens — AI-Powered Incident Post-Mortem Generator
Pastel / Light theme edition — mobile responsive fix applied.
"""

import streamlit as st
import requests
import json
import os
import re
from datetime import datetime

st.set_page_config(
    page_title="InfraLens",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=DM+Mono:ital,wght@0,300;0,400;0,500;1,400&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body, [class*="css"], .stApp {
    font-family: 'DM Sans', sans-serif;
    background: #f5f3ef;
    color: #1c1917;
}
.stApp { background: #f5f3ef; }
#MainMenu, footer, header { visibility: hidden; }
section[data-testid="stSidebar"] { display: none; }
.block-container {
    padding: 2rem 3.5rem 5rem !important;
    max-width: 1100px !important;
    margin: 0 auto !important;
}
div[data-testid="stVerticalBlock"] { gap: 0 !important; }

::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: #ede9e3; }
::-webkit-scrollbar-thumb { background: #c4b8a8; border-radius: 3px; }

.stApp::before {
    content: '';
    position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background-image:
        radial-gradient(ellipse 700px 500px at 5% 0%, rgba(167,139,250,0.08) 0%, transparent 60%),
        radial-gradient(ellipse 600px 400px at 95% 100%, rgba(251,182,206,0.09) 0%, transparent 60%),
        radial-gradient(ellipse 500px 300px at 60% 40%, rgba(196,228,255,0.06) 0%, transparent 60%);
    pointer-events: none; z-index: 0;
}

.page { position: relative; z-index: 1; }

/* NAV */
.nav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1.6rem 0 1.3rem;
    border-bottom: 1px solid #e0d9d1;
    margin-bottom: 3.5rem;
}
.nav-logo {
    display: flex; align-items: center; gap: 10px;
    font-size: 1.05rem; font-weight: 700;
    letter-spacing: -0.4px; color: #1c1917;
}
.nav-logo-icon {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, #7c6fcd, #b47ab5);
    border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    font-family: 'DM Mono', monospace;
    font-size: 11px; font-weight: 500; color: #fff;
    flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(124,111,205,0.25);
}
.nav-logo span { color: #9c9189; font-weight: 400; }
.nav-badge {
    display: flex; align-items: center; gap: 7px;
    font-family: 'DM Mono', monospace;
    font-size: 0.65rem; color: #7c7269;
    border: 1px solid #ddd6ce;
    background: #faf8f5;
    padding: 5px 13px; border-radius: 20px;
}
.nav-badge-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: #5bb37a;
    box-shadow: 0 0 6px rgba(91,179,122,0.6);
    animation: blink 2s infinite;
    flex-shrink: 0;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.35} }

/* HERO */
.hero { margin-bottom: 2.2rem; }
.hero-tag {
    display: inline-flex; align-items: center; gap: 7px;
    background: rgba(124,111,205,0.08);
    border: 1px solid rgba(124,111,205,0.18);
    border-radius: 20px; padding: 4px 13px;
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem; color: #6b5fad;
    letter-spacing: 0.09em; text-transform: uppercase;
    margin-bottom: 1.2rem;
}
.hero-tag-dot { width: 4px; height: 4px; border-radius: 50%; background: #6b5fad; flex-shrink: 0; }
.hero-h1 {
    font-size: clamp(1.6rem, 5vw, 3rem);
    font-weight: 700; line-height: 1.07;
    letter-spacing: -1.5px; margin-bottom: 1rem;
    color: #12100e;
}
.hero-h1 .accent {
    background: linear-gradient(90deg, #6b5fad 0%, #a06bb0 50%, #c4728a 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
}
.hero-body {
    font-size: 0.95rem; color: #6b6259;
    line-height: 1.75; max-width: 500px; margin-bottom: 1.8rem;
    font-weight: 400;
}
/* STATS — responsive grid from the start */
.hero-stats {
    display: grid;
    grid-template-columns: repeat(4, auto);
    width: fit-content;
    border: 1px solid #e0d9d1;
    border-radius: 10px; overflow: hidden;
    background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.hero-stat {
    padding: 11px 22px;
    border-right: 1px solid #e0d9d1;
    text-align: center;
}
.hero-stat:last-child { border-right: none; }
.hero-stat-num {
    font-size: 1.4rem; font-weight: 700;
    color: #12100e; line-height: 1; letter-spacing: -0.5px;
}
.hero-stat-label {
    font-size: 0.6rem; color: #9c9189;
    margin-top: 4px; font-family: 'DM Mono', monospace;
    text-transform: uppercase; letter-spacing: 0.07em;
}

/* INPUT PANEL */
.input-panel {
    background: #fff;
    border: 1px solid #e0d9d1;
    border-radius: 16px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.05), 0 1px 3px rgba(0,0,0,0.03);
    overflow: hidden;
    margin-bottom: 0;
}
.input-panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 1rem 1.4rem 0.9rem;
    border-bottom: 1px solid #f0ece6;
    background: #fdfcfa;
}
.input-panel-title {
    font-size: 0.78rem; font-weight: 600;
    color: #3a3530; letter-spacing: -0.1px;
}
.input-panel-body { padding: 1.8rem 1.8rem 1.8rem; }

.mode-strip {
    display: flex; align-items: flex-start; gap: 10px;
    background: rgba(124,111,205,0.06);
    border: 1px solid rgba(124,111,205,0.16);
    border-radius: 9px;
    padding: 9px 13px;
    margin-bottom: 1rem;
    font-size: 0.82rem; color: #3a3530; line-height: 1.55;
}
.mode-strip-icon { font-size: 1rem; flex-shrink: 0; margin-top: 2px; }
.mode-strip-text b { color: #5a4fa0; font-weight: 600; }

.field-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.6rem; color: #9c9189;
    text-transform: uppercase; letter-spacing: 0.1em;
    margin-bottom: 6px;
}

/* RADIO */
.stRadio > div { flex-direction: row !important; gap: 6px !important; flex-wrap: wrap !important; }
.stRadio label {
    color: #4a4540 !important;
    font-size: 0.85rem !important; font-weight: 500 !important;
    background: #faf8f5 !important;
    border: 1px solid #e0d9d1 !important;
    border-radius: 8px !important; padding: 0.5rem 1.1rem !important;
    font-family: 'DM Sans', sans-serif !important;
    transition: all 0.12s !important;
    white-space: nowrap !important;
}
.stRadio label:hover {
    color: #1c1917 !important;
    border-color: #b5adaa !important;
    background: #f5f1eb !important;
}

/* TEXT INPUT */
.stTextInput > div > div {
    background: #faf8f5 !important;
    border: 1px solid #ddd6ce !important;
    border-radius: 9px !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.03) !important;
}
.stTextInput input {
    color: #1c1917 !important;
    font-family: 'DM Mono', monospace !important;
    font-size: 0.87rem !important;
    background: transparent !important;
}
.stTextInput input::placeholder { color: #b5adaa !important; }
.stTextInput > div > div:focus-within {
    border-color: rgba(124,111,205,0.55) !important;
    box-shadow: 0 0 0 3px rgba(124,111,205,0.1) !important;
}

[data-testid="stFileUploader"] label {
    font-family: 'DM Sans', sans-serif !important;
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    color: #3a3530 !important;
    margin-bottom: 6px !important;
}
[data-testid="stFileUploader"] {
    background: #faf8f5 !important;
    border: 1.5px dashed #d9d1c9 !important;
    border-radius: 11px !important;
    transition: all 0.2s !important;
}
[data-testid="stFileUploader"]:hover {
    border-color: rgba(124,111,205,0.45) !important;
    background: rgba(124,111,205,0.03) !important;
}
[data-testid="stFileUploaderDropzoneInstructions"] { color: #8c8279 !important; }
[data-testid="stFileUploader"] small { color: #b5adaa !important; }

/* GENERATE BUTTON */
.stButton > button {
    background: linear-gradient(135deg, #7c6fcd 0%, #b07ab8 60%, #c47a90 100%) !important;
    color: #fff !important; border: none !important;
    border-radius: 9px !important; font-weight: 600 !important;
    font-size: 0.9rem !important; letter-spacing: -0.1px !important;
    padding: 0.6rem 1.6rem !important; width: auto !important;
    box-shadow: 0 3px 14px rgba(124,111,205,0.28), inset 0 1px 0 rgba(255,255,255,0.2) !important;
    transition: all 0.15s ease !important;
    font-family: 'DM Sans', sans-serif !important;
}
.stButton > button:hover {
    box-shadow: 0 8px 28px rgba(124,111,205,0.38), inset 0 1px 0 rgba(255,255,255,0.25) !important;
    transform: translateY(-2px) !important;
}
.stButton > button:active { transform: translateY(0) !important; }
.stButton > button:disabled {
    background: #e8e3dc !important;
    color: #b5adaa !important;
    box-shadow: none !important; transform: none !important;
}

.demo-callout {
    border-radius: 10px;
    border: 1px solid rgba(91,179,122,0.28);
    background: rgba(91,179,122,0.06);
    padding: 1rem 1.2rem;
    margin-bottom: 1.2rem;
    font-size: 0.86rem;
    color: #3d5c47;
    line-height: 1.72;
}
.demo-callout strong { color: #2d7a4d; font-weight: 600; }

.divider { height: 1px; background: #e0d9d1; margin: 2.5rem 0 2rem; }

.report-card {
    background: #fff;
    border: 1px solid #e8e3dc;
    border-radius: 12px;
    padding: 0 1.6rem 1.6rem;
    margin-bottom: 1.5rem;
}

.report-header { margin-bottom: 2rem; padding-top: 0.5rem; }
.report-eyebrow {
    font-family: 'DM Mono', monospace;
    font-size: 0.6rem; font-weight: 500;
    color: #b5adaa;
    letter-spacing: 0.12em; text-transform: uppercase;
    margin-bottom: 6px;
    display: flex; align-items: center; gap: 8px;
}
.report-eyebrow::before { content: ''; display: inline-block; width: 20px; height: 1px; background: #d9d1c9; }
.report-title {
    font-size: 2rem; font-weight: 700;
    letter-spacing: -1px; color: #12100e;
    margin-bottom: 12px; line-height: 1.05;
}
.report-meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.meta-chip {
    font-family: 'DM Mono', monospace;
    font-size: 0.65rem; font-weight: 500;
    padding: 3px 11px; border-radius: 20px;
    display: inline-flex; align-items: center; gap: 5px;
}
.meta-chip.events   { background: #f0ece6; color: #6b6259; border: 1px solid #ddd6ce; }
.meta-chip.duration { background: rgba(124,111,205,0.1); color: #5a4fa0; border: 1px solid rgba(124,111,205,0.22); }
.meta-chip.conf-high   { background: rgba(91,179,122,0.1); color: #2d7a4d; border: 1px solid rgba(91,179,122,0.28); }
.meta-chip.conf-medium { background: rgba(222,158,65,0.1); color: #8f5e12; border: 1px solid rgba(222,158,65,0.28); }
.meta-chip.conf-low    { background: rgba(211,70,70,0.08); color: #a02828; border: 1px solid rgba(211,70,70,0.2); }

.rc-section {
    margin-bottom: 2rem;
    background: #fff;
    border: 1px solid #e8e3dc;
    border-radius: 12px;
    padding: 1.4rem 1.6rem;
}
.rc-kicker {
    font-family: 'DM Mono', monospace;
    font-size: 0.58rem; font-weight: 600;
    color: #7c6fcd; letter-spacing: 0.15em;
    text-transform: uppercase; margin-bottom: 10px;
}
.rc-headline {
    font-size: 1.1rem; font-weight: 600;
    line-height: 1.45; letter-spacing: -0.3px;
    color: #1c1917; max-width: 780px;
    border-left: 2.5px solid #7c6fcd;
    padding-left: 14px; margin-bottom: 12px;
    padding-top: 4px; padding-bottom: 4px;
}
.rc-body {
    font-size: 0.9rem; color: #5a5450;
    line-height: 1.8; max-width: 720px;
    padding-left: 16px;
}

.sec-heading {
    font-size: 0.6rem; font-weight: 600;
    color: #9c9189;
    letter-spacing: 0.13em; text-transform: uppercase;
    margin-bottom: 12px; margin-top: 2rem;
    display: flex; align-items: center; gap: 10px;
    font-family: 'DM Mono', monospace;
}
.sec-heading::after { content: ''; flex: 1; height: 1px; background: #e8e3dc; }

.factor-list { display: flex; flex-direction: column; gap: 0; margin-bottom: 1.5rem; }
.factor-item {
    display: flex; align-items: baseline; gap: 14px;
    padding: 11px 0;
    border-bottom: 1px solid #f0ece6;
    font-size: 0.91rem; color: #2e2b28; line-height: 1.65;
}
.factor-item:last-child { border-bottom: none; }
.factor-n {
    font-family: 'DM Mono', monospace;
    font-size: 0.6rem; color: #c4bdb4;
    min-width: 22px; padding-top: 0.05rem; flex-shrink: 0;
    text-align: right;
}

.ptags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 1.5rem; }
.ptag {
    font-family: 'DM Mono', monospace;
    font-size: 0.63rem; font-weight: 500;
    padding: 3px 9px; border-radius: 5px;
    background: rgba(124,111,205,0.08);
    border: 1px solid rgba(124,111,205,0.2);
    color: #5a4fa0;
}
.ptag.n1 { background: rgba(196,114,138,0.09); border-color: rgba(196,114,138,0.25); color: #8c3a55; }
.ptag.warn { background: rgba(222,158,65,0.08); border-color: rgba(222,158,65,0.22); color: #8f5e12; }

/* TIMELINE */
.tl-wrap { font-family: 'DM Mono', monospace; }
.tl-item {
    display: grid;
    grid-template-columns: 135px 50px 70px 1fr;
    gap: 10px; align-items: baseline;
    padding: 7px 10px;
    border-bottom: 1px solid #ede9e3;
    font-size: 0.77rem; line-height: 1.55;
    border-radius: 6px; transition: background 0.1s;
}
.tl-item:hover { background: #f0ece6; }
.tl-ts { color: #b5adaa; font-size: 0.67rem; }
.tl-src {
    font-size: 0.6rem; font-weight: 700; text-align: center;
    padding: 2px 5px; border-radius: 4px;
}
.src-log    { background: rgba(124,111,205,0.1); color: #5a4fa0; }
.src-slack  { background: rgba(180,122,181,0.1); color: #7a3d8c; }
.src-ticket { background: rgba(56,152,220,0.1);  color: #1a5e99; }
.tl-lvl { font-size: 0.62rem; font-weight: 700; }
.lvl-CRITICAL { color: #c0392b; }
.lvl-ERROR    { color: #c05c28; }
.lvl-WARN     { color: #a07014; }
.lvl-INFO     { color: #b5adaa; }
.tl-msg { color: #4a4540; font-size: 0.77rem; word-break: break-word; }
.tl-msg.crit { color: #1c1917; font-weight: 500; }
.tl-msg.err  { color: #2e2b28; }
.tl-pchip {
    display: inline-block; font-size: 0.54rem; font-weight: 600;
    background: rgba(124,111,205,0.1); color: #5a4fa0;
    border-radius: 3px; padding: 1px 4px; margin-left: 5px;
    vertical-align: middle;
}

.ev-wrap { display: flex; flex-direction: column; gap: 7px; margin-bottom: 1.5rem; }
.ev-item {
    background: #faf8f5;
    border: 1px solid #e0d9d1;
    border-left: 2.5px solid rgba(124,111,205,0.55);
    border-radius: 0 9px 9px 0;
    padding: 9px 13px;
    font-family: 'DM Mono', monospace;
    font-size: 0.74rem; color: #3a3530;
    line-height: 1.65; word-break: break-word;
}

.action-list { display: flex; flex-direction: column; gap: 0; margin-bottom: 1.5rem; }
.action-item {
    display: flex; gap: 14px; align-items: flex-start;
    padding: 13px 0;
    border-bottom: 1px solid #ede9e3;
}
.action-item:last-child { border-bottom: none; }
.action-idx {
    font-family: 'DM Mono', monospace;
    font-size: 0.6rem; color: #b5adaa;
    min-width: 20px; padding-top: 3px; flex-shrink: 0;
}
.action-body { flex: 1; min-width: 0; }
.action-what {
    font-size: 0.93rem; font-weight: 500;
    color: #1c1917; line-height: 1.5;
    margin-bottom: 6px; word-break: break-word;
}
.action-foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.action-owner {
    font-family: 'DM Mono', monospace;
    font-size: 0.66rem; color: #8c8279;
}
.prio {
    font-family: 'DM Mono', monospace;
    font-size: 0.62rem; font-weight: 700;
    padding: 2px 8px; border-radius: 5px;
}
.prio.P1 { background: rgba(192,57,43,0.08); color: #a02828; border: 1px solid rgba(192,57,43,0.2); }
.prio.P2 { background: rgba(222,158,65,0.1);  color: #8f5e12; border: 1px solid rgba(222,158,65,0.25); }
.prio.P3 { background: rgba(91,179,122,0.1);  color: #2d7a4d; border: 1px solid rgba(91,179,122,0.22); }

.similar-item {
    font-family: 'DM Mono', monospace;
    font-size: 0.77rem; color: #5a4fa0;
    padding: 9px 0;
    border-bottom: 1px solid #ede9e3;
    line-height: 1.6; word-break: break-word;
}
.similar-item:last-child { border-bottom: none; }

.stDownloadButton > button {
    background: #faf8f5 !important;
    color: #3a3530 !important;
    border: 1px solid #ddd6ce !important;
    border-radius: 9px !important;
    font-family: 'DM Sans', sans-serif !important;
    font-weight: 600 !important; font-size: 0.85rem !important;
    width: 100% !important;
    padding: 0.62rem 1.1rem !important;
    transition: all 0.15s !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04) !important;
}
.stDownloadButton > button:hover {
    background: rgba(124,111,205,0.07) !important;
    border-color: rgba(124,111,205,0.35) !important;
    color: #1c1917 !important;
}

/* TABS */
.stTabs [data-baseweb="tab-list"] {
    background: transparent !important;
    border-bottom: 1px solid #e0d9d1 !important;
    gap: 0 !important;
    padding: 0 !important;
    flex-wrap: wrap !important;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'DM Sans', sans-serif !important;
    font-size: 0.84rem !important; font-weight: 500 !important;
    color: #9c9189 !important;
    padding: 0.65rem 1.1rem !important;
    background: transparent !important;
    border: none !important; border-radius: 0 !important;
    letter-spacing: -0.1px !important;
    transition: color 0.12s !important;
}
.stTabs [data-baseweb="tab"]:hover { color: #3a3530 !important; background: transparent !important; }
.stTabs [aria-selected="true"] { color: #1c1917 !important; font-weight: 600 !important; background: transparent !important; }
.stTabs [data-baseweb="tab-highlight"] { background: #7c6fcd !important; height: 2px !important; }
.stTabs [data-baseweb="tab-border"] { background: #e0d9d1 !important; }
.stTabs [data-baseweb="tab-panel"] { padding: 1.6rem 0 0 !important; }

[data-baseweb="select"] > div {
    background: #faf8f5 !important;
    border-color: #ddd6ce !important;
    border-radius: 8px !important;
}
[data-baseweb="select"] span { color: #3a3530 !important; }
[data-baseweb="tag"] { background: rgba(124,111,205,0.12) !important; color: #5a4fa0 !important; }
[data-baseweb="menu"] {
    background: #fefcfa !important;
    border: 1px solid #e0d9d1 !important;
    border-radius: 10px !important;
    box-shadow: 0 4px 20px rgba(0,0,0,0.08) !important;
}
[data-baseweb="menu"] li { color: #3a3530 !important; }
[data-baseweb="menu"] li:hover { background: rgba(124,111,205,0.07) !important; }

.stSuccess { background: rgba(91,179,122,0.08) !important; border-color: rgba(91,179,122,0.25) !important; color: #2d7a4d !important; border-radius: 8px !important; }
.stInfo    { background: rgba(124,111,205,0.07) !important; border-color: rgba(124,111,205,0.22) !important; color: #5a4fa0 !important; border-radius: 8px !important; }
.stWarning { background: rgba(222,158,65,0.08) !important; border-color: rgba(222,158,65,0.22) !important; color: #8f5e12 !important; border-radius: 8px !important; }
.stError   { background: rgba(192,57,43,0.07) !important; border-color: rgba(192,57,43,0.2) !important; color: #a02828 !important; border-radius: 8px !important; }

.stCodeBlock { background: #f0ece6 !important; border: 1px solid #ddd6ce !important; border-radius: 10px !important; }
.stCodeBlock code { color: #2e2b28 !important; }

.upload-panel {
    background: #faf8f5;
    border: 1px solid #e0d9d1;
    border-radius: 14px;
    padding: 1.4rem 1.5rem;
    margin-top: 1rem;
    margin-bottom: 0.5rem;
}
.upload-success {
    font-family: 'DM Mono', monospace;
    font-size: 0.67rem; color: #2d7a4d;
    text-align: center; padding: 4px 0;
    font-weight: 500;
}

/* ══════════════════════════════════════════
   MOBILE RESPONSIVE — all fixes below
   ══════════════════════════════════════════ */
@media (max-width: 768px) {
    /* Padding */
    .block-container {
        padding: 1rem 0.9rem 4rem !important;
    }

    /* Nav */
    .nav {
        padding: 1rem 0 0.9rem;
        margin-bottom: 1.8rem;
    }
    .nav-badge {
        font-size: 0.58rem !important;
        padding: 4px 9px !important;
        gap: 5px;
    }
    .nav-logo { font-size: 0.95rem !important; }

    /* Hero */
    .hero-h1 {
        font-size: clamp(1.4rem, 6vw, 2rem) !important;
        letter-spacing: -0.6px !important;
    }
    .hero-body { font-size: 0.85rem !important; max-width: 100% !important; }

    /* Stats — 2x2 grid, full width */
    .hero-stats {
        grid-template-columns: 1fr 1fr !important;
        width: 100% !important;
    }
    .hero-stat {
        padding: 10px 10px !important;
        border-right: 1px solid #e0d9d1;
        border-bottom: 1px solid #e0d9d1;
    }
    .hero-stat:nth-child(2) { border-right: none !important; }
    .hero-stat:nth-child(3) { border-bottom: none !important; }
    .hero-stat:nth-child(4) { border-right: none !important; border-bottom: none !important; }
    .hero-stat-num { font-size: 1.1rem !important; }
    .hero-stat-label { font-size: 0.55rem !important; }

    /* Input panel */
    .input-panel-body { padding: 1rem 0.9rem 1rem !important; }
    .input-panel-header { padding: 0.8rem 1rem 0.7rem !important; }

    /* Mode strip */
    .mode-strip { font-size: 0.78rem !important; }

    /* Radio — wrap naturally */
    .stRadio > div { flex-wrap: wrap !important; }
    .stRadio label {
        font-size: 0.78rem !important;
        padding: 0.4rem 0.8rem !important;
        white-space: normal !important;
    }

    /* Report */
    .report-card { padding: 0 0.6rem 1rem !important; }
    .report-title { font-size: 1.3rem !important; letter-spacing: -0.5px !important; }

    /* Root cause */
    .rc-section { padding: 1rem !important; }
    .rc-headline { font-size: 0.93rem !important; }
    .rc-body { font-size: 0.84rem !important; padding-left: 12px !important; }

    /* Timeline — stack into rows instead of 4-col grid */
    .tl-item {
        display: flex !important;
        flex-direction: column !important;
        gap: 2px !important;
        padding: 8px 4px !important;
        align-items: flex-start !important;
    }
    .tl-ts { font-size: 0.58rem !important; }
    .tl-src { text-align: left !important; }
    .tl-msg { font-size: 0.74rem !important; }

    /* Tabs — allow scroll on mobile */
    .stTabs [data-baseweb="tab-list"] {
        flex-wrap: nowrap !important;
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch !important;
    }
    .stTabs [data-baseweb="tab"] {
        font-size: 0.75rem !important;
        padding: 0.5rem 0.8rem !important;
        white-space: nowrap !important;
    }

    /* Factors */
    .factor-item { font-size: 0.84rem !important; gap: 10px !important; }

    /* Actions */
    .action-what { font-size: 0.84rem !important; }

    /* Evidence */
    .ev-item { font-size: 0.68rem !important; }

    /* Pattern tags */
    .ptag { font-size: 0.57rem !important; padding: 2px 6px !important; }

    /* Column overflow prevention */
    [data-testid="column"] {
        min-width: 0 !important;
        overflow: hidden !important;
    }
}

/* Very small screens (< 420px) */
@media (max-width: 420px) {
    .block-container { padding: 0.75rem 0.6rem 3rem !important; }
    .hero-h1 { font-size: 1.35rem !important; }
    .nav-logo-icon { width: 28px !important; height: 28px !important; font-size: 10px !important; }
    .hero-stat-num { font-size: 1rem !important; }
    .report-title { font-size: 1.1rem !important; }
    .rc-headline { font-size: 0.87rem !important; }
}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════
# DATA + LOGIC (unchanged)
# ══════════════════════════════════════════════════════════════════
DEMO_LOGS = """\
2024-03-15 02:01:03 INFO  [db-primary] PostgreSQL 15.2 on x86_64-pc-linux-gnu
2024-03-15 02:01:45 INFO  [api-gateway] Request rate: 342 req/s (normal baseline)
2024-03-15 02:03:11 INFO  [db-primary] Checkpoint complete: wrote 1823 buffers
2024-03-15 02:07:22 WARN  [db-primary] Connection pool at 78% capacity (156/200)
2024-03-15 02:08:01 WARN  [db-primary] Slow query detected: SELECT * FROM orders WHERE user_id=? took 4823ms
2024-03-15 02:08:14 WARN  [db-primary] Slow query detected: SELECT * FROM orders WHERE user_id=? took 6201ms
2024-03-15 02:08:33 ERROR [db-primary] Connection pool exhausted: 200/200 connections active
2024-03-15 02:08:33 ERROR [api-gateway] Upstream DB timeout after 5000ms on /api/v1/orders
2024-03-15 02:08:34 ERROR [api-gateway] Upstream DB timeout after 5000ms on /api/v1/users
2024-03-15 02:08:34 ERROR [api-gateway] Upstream DB timeout after 5000ms on /api/v1/products
2024-03-15 02:08:35 CRITICAL [db-primary] FATAL: max_connections limit reached — refusing new connections
2024-03-15 02:08:35 CRITICAL [api-gateway] Health check FAILED for db-primary:5432
2024-03-15 02:08:36 ERROR [auth-service] Cannot connect to DB: connection refused on 10.0.1.5:5432
2024-03-15 02:08:36 ERROR [order-service] DB write failed for order ORD-928371: connection refused
2024-03-15 02:08:37 ERROR [payment-service] Transaction rollback: DB unavailable — order ORD-928372 NOT charged
2024-03-15 02:08:38 CRITICAL [api-gateway] Circuit breaker OPEN for db-primary after 5 consecutive failures
2024-03-15 02:08:39 ERROR [api-gateway] 503 Service Unavailable — returned to 1,204 requests in last 10s
2024-03-15 02:09:01 WARN  [db-replica-1] Replication lag: 47 seconds behind primary
2024-03-15 02:09:14 WARN  [db-replica-1] Replication lag: 112 seconds behind primary
2024-03-15 02:09:22 ERROR [db-replica-1] Replication stream disconnected from primary
2024-03-15 02:10:05 INFO  [ops-bot] PagerDuty alert fired: DB_POOL_EXHAUSTED — P1 incident created
2024-03-15 02:10:06 INFO  [ops-bot] On-call engineer notified: @rahul.sre via PagerDuty
2024-03-15 02:12:44 INFO  [rahul.sre] SSH session opened to db-primary (10.0.1.5)
2024-03-15 02:13:01 INFO  [db-primary] Admin query: SELECT count(*), state FROM pg_stat_activity GROUP BY state
2024-03-15 02:13:03 INFO  [db-primary] Query result: 197 connections in state=idle_in_transaction
2024-03-15 02:13:15 WARN  [db-primary] Identified cause: connection leak in order-service v2.4.1
2024-03-15 02:14:02 INFO  [rahul.sre] Executing: SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state='idle_in_transaction'
2024-03-15 02:14:03 INFO  [db-primary] Terminated 183 idle_in_transaction connections
2024-03-15 02:14:04 INFO  [db-primary] Connection pool: 14/200 active — recovered
2024-03-15 02:14:10 INFO  [api-gateway] Circuit breaker HALF-OPEN — testing db-primary
2024-03-15 02:14:22 INFO  [api-gateway] Health check passed — db-primary healthy
2024-03-15 02:14:23 INFO  [api-gateway] Circuit breaker CLOSED — traffic restored
2024-03-15 02:15:01 INFO  [all-services] Traffic normalising — all services healthy
"""

DEMO_SLACK = [
    {"ts":"1710468203.000001","user":"priya.oncall","text":"🚨 ALERT: Getting PagerDuty for DB_POOL_EXHAUSTED on prod. Anyone else seeing this?"},
    {"ts":"1710468215.000002","user":"rahul.sre","text":"Yeah I'm getting paged too. Looking now"},
    {"ts":"1710468228.000003","user":"arjun.backend","text":"order-service is throwing 503s, users can't checkout. Seeing it in Datadog"},
    {"ts":"1710468241.000004","user":"priya.oncall","text":"Declaring P1. @channel — production DB is down. War room in #incident-4821"},
    {"ts":"1710468267.000005","user":"rahul.sre","text":"SSHed into db-primary. Connection pool is full — 200/200. All idle_in_transaction"},
    {"ts":"1710468289.000006","user":"arjun.backend","text":"Wait — we deployed order-service v2.4.1 at 01:45. Could that be it?"},
    {"ts":"1710468301.000007","user":"rahul.sre","text":"Checking pg_stat_activity... yes. 197 connections stuck in idle_in_transaction. This is a connection leak"},
    {"ts":"1710468334.000009","user":"arjun.backend","text":"I think I forgot to close the transaction in the exception handler 😬"},
    {"ts":"1710468342.000010","user":"rahul.sre","text":"Killing idle_in_transaction connections now to restore service"},
    {"ts":"1710468363.000011","user":"rahul.sre","text":"Done. Pool is back to 14/200. Circuit breaker should close soon"},
    {"ts":"1710468585.000015","user":"rahul.sre","text":"Restart complete. order-service v2.4.1 with hotfix running. All healthy"},
    {"ts":"1710468603.000016","user":"priya.oncall","text":"Resolving P1. Total downtime: ~11 minutes. Post-mortem due by EOD."},
]

DEMO_TICKETS = [
    {"id":"INC-4821","title":"P1: Production DB connection pool exhausted — full outage",
     "description":"DB primary unreachable. All API endpoints returning 503. Connection leak in order-service v2.4.1.",
     "status":"resolved","priority":"P1","created_at":"2024-03-15T02:10:05Z","assignee":"rahul.sre"},
    {"id":"TASK-5102","title":"Fix: Close DB transaction in order-service exception handler",
     "description":"Retry logic in v2.4.1 failed to call conn.commit() or conn.rollback() in the except block.",
     "status":"in_progress","priority":"P1","created_at":"2024-03-15T02:21:00Z","assignee":"arjun.backend"},
    {"id":"TASK-5103","title":"Add alert: DB connection pool > 70% capacity",
     "description":"Add warning alert at 70% and critical at 90%.",
     "status":"open","priority":"P2","created_at":"2024-03-15T02:22:00Z","assignee":"priya.oncall"},
]

TS_PATTERNS = [
    re.compile(r'(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})'),
    re.compile(r'(?P<ts>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2})'),
    re.compile(r'(?P<ts>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})'),
]
LEVEL_PATTERN = re.compile(r'\b(CRITICAL|FATAL|ERROR|ERR|WARN(?:ING)?|INFO|DEBUG|NOTICE|TRACE)\b', re.IGNORECASE)
LEVEL_NORM = {"fatal":"CRITICAL","critical":"CRITICAL","error":"ERROR","err":"ERROR","warning":"WARN","warn":"WARN","notice":"INFO","info":"INFO","debug":"DEBUG","trace":"DEBUG"}
SEV_PATTERNS = [
    (re.compile(r'connection\s+pool\s+(exhausted|full|at\s+max)',re.I),"DB_POOL_EXHAUSTED","CRITICAL"),
    (re.compile(r'max_connections\s+limit\s+reached',re.I),"DB_MAX_CONN","CRITICAL"),
    (re.compile(r'idle_in_transaction',re.I),"DB_IDLE_LEAK","WARN"),
    (re.compile(r'connection\s+pool\s+at\s+(\d+)%',re.I),"DB_POOL_HIGH","WARN"),
    (re.compile(r'slow\s+query\s+detected',re.I),"DB_SLOW_QUERY","WARN"),
    (re.compile(r'replication\s+lag[:\s]+(\d+)',re.I),"DB_REPLICATION_LAG","WARN"),
    (re.compile(r'replication\s+stream\s+disconnected',re.I),"DB_REPLICATION_BROKEN","ERROR"),
    (re.compile(r'circuit\s+breaker\s+(open|tripped)',re.I),"CIRCUIT_BREAKER_OPEN","ERROR"),
    (re.compile(r'circuit\s+breaker\s+(closed|half.open)',re.I),"CIRCUIT_BREAKER_CLOSE","INFO"),
    (re.compile(r'health\s+check\s+failed',re.I),"HEALTH_CHECK_FAIL","ERROR"),
    (re.compile(r'health\s+check\s+passed',re.I),"HEALTH_CHECK_OK","INFO"),
    (re.compile(r'upstream\s+\w+\s+timeout',re.I),"UPSTREAM_TIMEOUT","ERROR"),
    (re.compile(r'5\d{2}\s+(?:service\s+unavailable|internal)',re.I),"HTTP_5XX","ERROR"),
    (re.compile(r'out\s+of\s+memory|oom.kill',re.I),"OOM_KILL","CRITICAL"),
    (re.compile(r'memory\s+pressure|swap\s+usage',re.I),"MEMORY_PRESSURE","WARN"),
    (re.compile(r'deploy(?:ed|ing|ment)|rolling\s+restart',re.I),"DEPLOY","INFO"),
    (re.compile(r'rollback',re.I),"ROLLBACK","WARN"),
    (re.compile(r'(?:service|db|connection)\s+re.established|recovered|resolved',re.I),"RECOVERY","INFO"),
    (re.compile(r'traffic\s+restored|normalising|all\s+.*healthy',re.I),"RECOVERY","INFO"),
    (re.compile(r'pagerduty|alert\s+fired|on.call',re.I),"ALERT_FIRED","INFO"),
]

def _detect_patterns(c): return [(t,l) for r,t,l in SEV_PATTERNS if r.search(c)]

def _parse_log_line(line):
    line=line.strip()
    if not line or line.startswith("#"): return None
    ts=None
    for pat in TS_PATTERNS:
        m=pat.search(line)
        if m:
            raw=m.group("ts")
            for fmt in ("%Y-%m-%dT%H:%M:%S","%Y-%m-%d %H:%M:%S","%d/%b/%Y:%H:%M:%S","%b %d %H:%M:%S","%b  %d %H:%M:%S"):
                try:
                    ts=datetime.strptime(raw,fmt)
                    if ts.year==1900: ts=ts.replace(year=datetime.now().year)
                    break
                except ValueError: continue
            if ts: break
    if not ts: return None
    lm=LEVEL_PATTERN.search(line)
    level=LEVEL_NORM.get(lm.group(1).lower(),"INFO") if lm else "INFO"
    content=line
    for pat in TS_PATTERNS: content=pat.sub("",content,count=1)
    content=LEVEL_PATTERN.sub("",content,count=1)
    content=re.sub(r'^\s*[\[\]|:]+\s*','',content).strip() or line
    detected=_detect_patterns(line)
    sr={"DEBUG":0,"INFO":1,"WARN":2,"ERROR":3,"CRITICAL":4}
    for _,il in detected:
        if sr.get(il,0)>sr.get(level,0): level=il
    cm=re.search(r'\[([a-zA-Z0-9_\-\.]+)\]',line)
    return {"timestamp":ts,"source":"log","level":level,"content":content,
            "component":cm.group(1) if cm else None,"patterns":[t for t,_ in detected],"raw":line}

def parse_logs_text(text): return [ev for line in text.splitlines() if (ev:=_parse_log_line(line))]

def parse_slack_data(data):
    evs=[]
    for m in data:
        try: ts=datetime.utcfromtimestamp(float(m["ts"]))
        except: continue
        evs.append({"timestamp":ts,"source":"slack","level":"INFO","content":f"[{m.get('user','?')}] {m.get('text','')}","patterns":[],"raw":str(m)})
    return evs

def parse_ticket_data(data):
    evs=[]
    for t in data:
        try: ts=datetime.fromisoformat(t["created_at"].replace("Z",""))
        except: continue
        evs.append({"timestamp":ts,"source":"ticket","level":"INFO",
                    "content":f"[TICKET {t.get('id','')}] {t.get('title','')} — {t.get('description','')} (status: {t.get('status','?')})",
                    "patterns":[],"raw":str(t)})
    return evs

def build_timeline(sources):
    evs=[]
    for s in sources: evs.extend(s)
    evs.sort(key=lambda e:e["timestamp"])
    seen=set(); out=[]
    for ev in evs:
        k=(ev["timestamp"].replace(microsecond=0).isoformat(),(ev.get("content") or "")[:120])
        if k in seen: continue
        seen.add(k); out.append(ev)
    for i,ev in enumerate(out):
        ev["event_id"]=i; ev["timestamp_str"]=ev["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
    return out

FALLBACK_API=os.getenv("INFRALENS_API_URL","http://localhost:8000")

def _call_api(iid,lb,sb,tb):
    files,data={},{"incident_id":iid}
    if lb: files["log_file"]=("logs.txt",lb,"text/plain")
    if sb: files["slack_file"]=("slack.json",sb,"application/json")
    if tb: files["ticket_file"]=("tickets.json",tb,"application/json")
    r=requests.post(f"{FALLBACK_API}/analyze",data=data,files=files,timeout=120)
    r.raise_for_status(); return r.json()["postmortem"]

def _local_postmortem(iid,tl):
    ap=[]
    for ev in tl: ap.extend(ev.get("patterns",[]))
    pc={}
    for p in ap: pc[p]=pc.get(p,0)+1
    critical=[e for e in tl if e["level"] in ("CRITICAL","ERROR")]
    recovery=[e for e in tl if "RECOVERY" in e.get("patterns",[]) or "CIRCUIT_BREAKER_CLOSE" in e.get("patterns",[])]
    deploys=[e for e in tl if "DEPLOY" in e.get("patterns",[])]
    dur=int((tl[-1]["timestamp"]-tl[0]["timestamp"]).total_seconds()/60) if tl else 0
    hp="DB_POOL_EXHAUSTED" in pc; ho="OOM_KILL" in pc
    hcb="CIRCUIT_BREAKER_OPEN" in pc; hl="DB_IDLE_LEAK" in pc; hhf="HEALTH_CHECK_FAIL" in pc
    ev6=[f"[{e['timestamp_str']}] [{e['source'].upper()}] {e['content']}" for e in critical[:6]]
    if recovery: ev6.append(f"[{recovery[0]['timestamp_str']}] [{recovery[0]['source'].upper()}] {recovery[0]['content']}")
    if hp and hl:
        rc="DB connection pool exhausted due to connection leak (idle_in_transaction) caused by improper transaction handling in application code."
        sm=(f"A P1 production outage lasting ~{dur} minutes was triggered by database connection pool exhaustion. "
            f"Connections were held in idle_in_transaction state due to a bug in a recent deployment, "
            f"causing cascading 503 errors across all downstream services.")
        fa=["Application code failed to close DB transactions on exception paths",
            "No early-warning alert at 70–80% connection pool capacity",
            "Replication lag cascaded as primary became overwhelmed",
            "Circuit breaker opened, cutting off all DB-dependent services"]
        if deploys: fa.insert(0,f"Recent deployment at {deploys[0]['timestamp_str']} introduced the connection leak")
        ac=[{"what":"Fix transaction context manager in exception handlers","owner":"Backend Team","priority":"P1"},
            {"what":"Add connection pool alert at 70% and 90% thresholds","owner":"SRE","priority":"P1"},
            {"what":"Add DB pool checks to deployment PR template","owner":"Engineering Lead","priority":"P2"},
            {"what":"Implement connection pool monitoring dashboard","owner":"SRE","priority":"P2"},
            {"what":"Run regression suite for DB connection handling before merge","owner":"QA","priority":"P3"}]
        cf="high"
    elif ho:
        rc="Out-of-memory event caused service termination and cascading failures."
        sm=f"Service outage of ~{dur} minutes caused by an OOM kill terminating critical processes."
        fa=["Insufficient memory limits configured","Memory leak in application","No memory pressure alerting"]
        ac=[{"what":"Tune memory limits in deployment manifests","owner":"SRE","priority":"P1"},
            {"what":"Profile application for memory leaks","owner":"Backend Team","priority":"P1"},
            {"what":"Add memory pressure alerts at 80% and 90%","owner":"SRE","priority":"P2"}]
        cf="high"
    elif hp:
        rc="Database connection pool exhausted, refusing new connections and causing service-wide outage."
        sm=f"Production incident spanning {dur} minutes. DB connection pool reached 100% capacity."
        fa=["Connection pool capacity too low","No pool capacity alerting","Missing idle connection timeout"]
        ac=[{"what":"Increase connection pool size and add idle timeouts","owner":"SRE","priority":"P1"},
            {"what":"Add pool utilisation alerts","owner":"SRE","priority":"P1"}]
        cf="medium"
    elif hcb and hhf:
        rc="Upstream service health checks failed repeatedly, tripping the circuit breaker and halting traffic."
        sm=f"Circuit breaker opened after consecutive health check failures over {dur} minutes."
        fa=["Health check endpoint not resilient to transient errors","Circuit breaker threshold too low","No fallback/degraded mode"]
        ac=[{"what":"Review health check endpoint for false negatives","owner":"Backend Team","priority":"P1"},
            {"what":"Implement graceful degradation responses","owner":"Backend Team","priority":"P2"},
            {"what":"Tune circuit breaker thresholds","owner":"SRE","priority":"P2"}]
        cf="medium"
    else:
        rc="Root cause requires further investigation — patterns detected but causality unclear."
        sm=f"An incident spanning {dur} minutes was detected. Automated analysis found anomalies but a definitive root cause could not be determined."
        fa=["Insufficient log coverage","Multiple overlapping failure signals"]
        ac=[{"what":"Gather additional metrics for the incident window","owner":"SRE","priority":"P1"},
            {"what":"Correlate with APM traces","owner":"Backend Team","priority":"P1"}]
        cf="low"
    nar=""
    if tl:
        nar=(f"Incident began at {tl[0]['timestamp_str']} with early warning signals. "
             f"Critical failures peaked between {critical[0]['timestamp_str'] if critical else '?'} and {critical[-1]['timestamp_str'] if critical else '?'}. ")
        if recovery: nar+=f"Recovery confirmed at {recovery[0]['timestamp_str']}. "
        nar+=f"Timeline spans {len(tl)} events across {dur} minutes."
    sim=[]
    if hp: sim.append("INC-3901 (2024-01-08): DB pool exhausted after batch job left connections open")
    if ho: sim.append("INC-4200 (2024-02-14): OOM kill on payment-service after memory leak in PDF generator")
    if hcb: sim.append("INC-4105 (2024-02-01): Circuit breaker tripped due to Redis cluster failover taking >30s")
    if not sim: sim.append("No similar past incidents found in knowledge base.")
    return {"incident_id":iid,"summary":sm,"timeline_reconstruction":nar,"root_cause":rc,
            "contributing_factors":fa,"evidence_citations":ev6,"action_items":ac,
            "similar_past_incidents":sim,"confidence":cf,"patterns_detected":pc,
            "event_count":len(tl),"duration_minutes":dur}


# ══════════════════════════════════════════════════════════════════
# RENDER REPORT
# ══════════════════════════════════════════════════════════════════
def render_report(pm, tl, iid):
    conf = (pm.get("confidence") or "").lower()
    conf_label = {"high":"HIGH CONFIDENCE","medium":"MEDIUM CONFIDENCE","low":"LOW CONFIDENCE"}.get(conf,"—")
    conf_cls   = f"conf-{conf if conf in ('high','medium','low') else 'medium'}"

    st.markdown(f"""
<div class="report-header">
  <div class="report-eyebrow">Post-Mortem Report</div>
  <div class="report-title">{iid}</div>
  <div class="report-meta">
    <span class="meta-chip events">⬡ {pm.get('event_count', len(tl))} events</span>
    <span class="meta-chip duration">⏱ {pm.get('duration_minutes', 0)} min</span>
    <span class="meta-chip {conf_cls}">◉ {conf_label}</span>
  </div>
</div>
""", unsafe_allow_html=True)

    st.markdown(f"""
<div class="rc-section">
  <div class="rc-kicker">Root Cause</div>
  <div class="rc-headline">{pm.get('root_cause','')}</div>
  <div class="rc-body">{pm.get('summary','')}</div>
</div>
""", unsafe_allow_html=True)

    st.markdown('<div class="report-card">', unsafe_allow_html=True)
    tabs = st.tabs(["Factors & Patterns", "Timeline", "Evidence", "Actions", "Similar"])

    with tabs[0]:
        factors = pm.get("contributing_factors", [])
        if factors:
            st.markdown('<div class="sec-heading">Contributing factors</div>', unsafe_allow_html=True)
            items = "".join(f'<div class="factor-item"><span class="factor-n">0{i+1}</span><span>{f}</span></div>' for i,f in enumerate(factors))
            st.markdown(f'<div class="factor-list">{items}</div>', unsafe_allow_html=True)
        pats = pm.get("patterns_detected", {})
        if pats:
            st.markdown('<div class="sec-heading">Detected signal patterns</div>', unsafe_allow_html=True)
            sorted_pats = sorted(pats.items(), key=lambda x: -x[1])
            chips = ""
            for i,(p,n) in enumerate(sorted_pats):
                cls = "n1" if i==0 else ("warn" if "WARN" in p or "SLOW" in p or "LAG" in p else "")
                chips += f'<span class="ptag {cls}">{p} ×{n}</span>'
            st.markdown(f'<div class="ptags">{chips}</div>', unsafe_allow_html=True)
        rec = pm.get("timeline_reconstruction","")
        if rec:
            st.markdown('<div class="sec-heading">Narrative</div>', unsafe_allow_html=True)
            st.markdown(f'<div style="font-size:0.9rem;color:#4a4540;line-height:1.85;font-family:DM Sans,sans-serif;max-width:680px;">{rec}</div>', unsafe_allow_html=True)

    with tabs[1]:
        if not tl:
            st.info("No timeline events.")
        else:
            fc1, fc2 = st.columns(2)
            with fc1:
                filter_levels = st.multiselect("Level", ["CRITICAL","ERROR","WARN","INFO"], default=["CRITICAL","ERROR","WARN"], key="tl_lvl")
            with fc2:
                filter_sources = st.multiselect("Source", ["log","slack","ticket"], default=["log","slack","ticket"], key="tl_src")
            filtered = [e for e in tl if e["level"] in filter_levels and e["source"] in filter_sources]
            st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:0.63rem;color:#9c9189;margin-bottom:0.8rem;">{len(filtered)} of {len(tl)} events</div>', unsafe_allow_html=True)
            rows=""
            for ev in filtered:
                is_crit = ev["level"]=="CRITICAL"; is_err = ev["level"]=="ERROR"
                msg_cls = "crit" if is_crit else ("err" if is_err else "")
                pchips="".join(f'<span class="tl-pchip">{p}</span>' for p in (ev.get("patterns") or []))
                rows += (f'<div class="tl-item">'
                         f'<span class="tl-ts">{ev["timestamp_str"]}</span>'
                         f'<span class="tl-src src-{ev["source"]}">{ev["source"].upper()}</span>'
                         f'<span class="tl-lvl lvl-{ev["level"]}">{ev["level"]}</span>'
                         f'<span class="tl-msg {msg_cls}">{ev["content"]}{pchips}</span>'
                         f'</div>')
            st.markdown(f'<div class="tl-wrap">{rows}</div>', unsafe_allow_html=True)

    with tabs[2]:
        cits = pm.get("evidence_citations",[])
        if not cits:
            st.info("No evidence citations.")
        else:
            st.markdown(f'<div style="font-family:DM Mono,monospace;font-size:0.63rem;color:#9c9189;margin-bottom:0.8rem;">{len(cits)} citation(s)</div>', unsafe_allow_html=True)
            st.markdown('<div class="ev-wrap">'+"".join(f'<div class="ev-item">{c}</div>' for c in cits)+'</div>', unsafe_allow_html=True)

    with tabs[3]:
        actions = pm.get("action_items",[])
        if not actions:
            st.info("No action items.")
        else:
            items=""
            for i,a in enumerate(actions,1):
                if isinstance(a,dict): what,owner,prio=a.get("what",""),a.get("owner","Unassigned"),a.get("priority","P3")
                else: what,owner,prio=str(a),"Unassigned","P3"
                items+=(f'<div class="action-item">'
                        f'<span class="action-idx">#{i}</span>'
                        f'<div class="action-body">'
                        f'<div class="action-what">{what}</div>'
                        f'<div class="action-foot">'
                        f'<span class="action-owner">{owner}</span>'
                        f'<span class="prio {prio}">{prio}</span>'
                        f'</div></div></div>')
            st.markdown(f'<div class="action-list">{items}</div>', unsafe_allow_html=True)

    with tabs[4]:
        similar = pm.get("similar_past_incidents",[])
        if similar:
            st.markdown('<div class="sec-heading">Past incidents with similar signatures</div>', unsafe_allow_html=True)
            st.markdown("".join(f'<div class="similar-item">↩  {s}</div>' for s in similar), unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div style="margin-top:1.5rem;display:flex;align-items:center;gap:8px;font-family:DM Mono,monospace;font-size:0.6rem;color:#9c9189;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:10px;">Export</div>', unsafe_allow_html=True)
    ec1, ec2 = st.columns(2)
    with ec1:
        st.download_button("⬇ Download JSON",
            data=json.dumps(pm, indent=2, default=str).encode(),
            file_name=f"postmortem_{iid}.json", mime="application/json",
            use_container_width=True, key="dl_json")
    with ec2:
        md=([f"# Post-Mortem — {iid}",
             f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
             f"Confidence: {pm.get('confidence','').upper()}",
             "","## Summary", pm.get("summary",""),
             "","## Root Cause", pm.get("root_cause",""),
             "","## Contributing Factors"]
            +[f"- {f}" for f in (pm.get("contributing_factors") or [])]
            +["","## Evidence"]
            +[f"```\n{c}\n```" for c in (pm.get("evidence_citations") or [])]
            +["","## Action Items"]
            +[(f"- [{a.get('priority','?')}] {a.get('what','')} — {a.get('owner','?')}" if isinstance(a,dict) else f"- {a}") for a in (pm.get("action_items") or [])])
        st.download_button("⬇ Download Markdown",
            data="\n".join(md).encode(),
            file_name=f"postmortem_{iid}.md", mime="text/markdown",
            use_container_width=True, key="dl_md")


# ══════════════════════════════════════════════════════════════════
# PAGE LAYOUT
# ══════════════════════════════════════════════════════════════════
st.markdown('<div class="page">', unsafe_allow_html=True)

# NAV
st.markdown("""
<div class="nav">
  <div class="nav-logo">
    <div class="nav-logo-icon">IL</div>
    Infra<span>Lens</span>
  </div>
  <div class="nav-badge">
    <span class="nav-badge-dot"></span>
    SRE Intelligence · v2.0
  </div>
</div>
""", unsafe_allow_html=True)

# HERO
st.markdown("""
<div class="hero">
  <div class="hero-tag"><span class="hero-tag-dot"></span>Incident Intelligence</div>
  <div class="hero-h1">Signal over noise.<br><span class="accent">Root cause, instantly.</span></div>
  <div class="hero-body">
    Feed it server logs, Slack threads, Jira tickets. Get back a complete post-mortem — root cause, timeline, evidence, action items — powered by a LangGraph agent and RAG over your incident history.
  </div>
  <div class="hero-stats">
    <div class="hero-stat"><div class="hero-stat-num">20</div><div class="hero-stat-label">signatures</div></div>
    <div class="hero-stat"><div class="hero-stat-num">&lt;30s</div><div class="hero-stat-label">to post-mortem</div></div>
    <div class="hero-stat"><div class="hero-stat-num">3</div><div class="hero-stat-label">source types</div></div>
    <div class="hero-stat"><div class="hero-stat-num">P1–P5</div><div class="hero-stat-label">severity</div></div>
  </div>
</div>
""", unsafe_allow_html=True)

st.markdown("<div style='height:2rem'></div>", unsafe_allow_html=True)
st.markdown('<div class="input-panel">', unsafe_allow_html=True)

st.markdown("""
<div class="input-panel-header">
  <span class="input-panel-title">New Analysis</span>
</div>
""", unsafe_allow_html=True)

st.markdown('<div class="input-panel-body">', unsafe_allow_html=True)

# Row 1: mode radio + incident ID
row1_left, row1_right = st.columns([3, 1.2])
with row1_left:
    mode = st.radio("Mode", ["⚡  Demo — pre-loaded P1 outage", "📁  Upload your own files"],
                    horizontal=True, label_visibility="collapsed")
is_demo = mode.startswith("⚡")

# Clear report when mode switches
if st.session_state.get("last_mode") != mode:
    st.session_state.pop("pm", None)
    st.session_state.pop("tl", None)
    st.session_state.pop("iid", None)
    st.session_state["last_mode"] = mode
with row1_right:
    incident_id = st.text_input("Incident ID", value="INC-DEMO-4821" if is_demo else "",
                                placeholder="INC-2024-001", label_visibility="collapsed")

st.markdown("<div style='height:0.6rem'></div>", unsafe_allow_html=True)

if is_demo:
    st.markdown("""
<div class="mode-strip">
  <span class="mode-strip-icon">⚡</span>
  <span class="mode-strip-text"><b>Demo scenario:</b> P1 DB connection pool exhaustion — order-service v2.4.1 · 34 log lines · 12 Slack messages · 3 tickets.</span>
</div>
""", unsafe_allow_html=True)
    with st.expander("Preview source data", expanded=False):
        t1, t2, t3 = st.tabs(["Server logs", "Slack", "Tickets"])
        with t1:
            st.code(DEMO_LOGS[:700]+"…", language="log")
        with t2:
            for m in DEMO_SLACK[:6]:
                st.markdown(f'<div style="font-size:0.8rem;color:#5a4fa0;font-family:DM Mono,monospace;padding:0.2rem 0;line-height:1.6;"><span style="color:#b5adaa;">[{m["user"]}]</span> {m["text"]}</div>', unsafe_allow_html=True)
            st.caption("…and 6 more")
        with t3:
            for t in DEMO_TICKETS:
                st.markdown(f'<div style="font-size:0.8rem;color:#1a5e99;font-family:DM Mono,monospace;padding:0.2rem 0;"><b>[{t["id"]}]</b> {t["title"]}</div>', unsafe_allow_html=True)

log_file = slack_file = ticket_file = None
if not is_demo:
    st.markdown("""
<div class="mode-strip" style="background:rgba(0,0,0,0.02);border-color:#e0d9d1;">
  <span class="mode-strip-icon">📁</span>
  <span class="mode-strip-text" style="color:#6b6259;">Upload server logs (.txt/.log), Slack JSON export, and/or Jira ticket JSON. At least one file required.</span>
</div>
""", unsafe_allow_html=True)
    u1, u2, u3 = st.columns(3)
    with u1:
        log_file = st.file_uploader("📋 Server Logs (.txt / .log)", type=["txt", "log"], key="lu")
        if log_file:
            st.markdown(f'<div class="upload-success">✓ {log_file.name}</div>', unsafe_allow_html=True)
    with u2:
        slack_file = st.file_uploader("💬 Slack Export (.json)", type=["json"], key="su")
        if slack_file:
            st.markdown(f'<div class="upload-success">✓ {slack_file.name}</div>', unsafe_allow_html=True)
    with u3:
        ticket_file = st.file_uploader("🎫 Tickets / Jira (.json)", type=["json"], key="tu")
        if ticket_file:
            st.markdown(f'<div class="upload-success">✓ {ticket_file.name}</div>', unsafe_allow_html=True)

st.markdown("<div style='height:1.6rem'></div>", unsafe_allow_html=True)

can_run = is_demo or bool(log_file or slack_file or ticket_file)
if not incident_id.strip(): can_run = False

btn_spacer, btn_col = st.columns([3, 1])
with btn_col:
    generate = st.button("⚡ Generate", disabled=not can_run, use_container_width=True)

st.markdown("</div></div>", unsafe_allow_html=True)

# RUN ANALYSIS
if generate and can_run and incident_id.strip():
    with st.spinner("Parsing sources…"):
        sources = []
        if is_demo:
            sources += [parse_logs_text(DEMO_LOGS), parse_slack_data(DEMO_SLACK), parse_ticket_data(DEMO_TICKETS)]
        else:
            if log_file: sources.append(parse_logs_text(log_file.read().decode("utf-8", errors="replace")))
            if slack_file:
                try:
                    d = json.loads(slack_file.read().decode())
                    sources.append(parse_slack_data(d if isinstance(d, list) else [d]))
                except Exception as e: st.error(f"Slack JSON error: {e}")
            if ticket_file:
                try:
                    d = json.loads(ticket_file.read().decode())
                    sources.append(parse_ticket_data(d if isinstance(d, list) else [d]))
                except Exception as e: st.error(f"Ticket JSON error: {e}")
        if not sources: st.error("No events could be parsed."); st.stop()
        timeline = build_timeline(sources)

    with st.spinner("Generating post-mortem…"):
        try:
            lb = DEMO_LOGS.encode() if is_demo else (log_file.getvalue() if log_file else None)
            sb = json.dumps(DEMO_SLACK).encode() if is_demo else (slack_file.getvalue() if slack_file else None)
            tb = json.dumps(DEMO_TICKETS).encode() if is_demo else (ticket_file.getvalue() if ticket_file else None)
            pm = _call_api(incident_id.strip(), lb, sb, tb)
        except Exception:
            pm = _local_postmortem(incident_id.strip(), timeline)

    st.session_state["pm"]  = pm
    st.session_state["tl"]  = timeline
    st.session_state["iid"] = incident_id.strip()

# RENDER REPORT
# RENDER REPORT
if "pm" in st.session_state:
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    render_report(st.session_state["pm"], st.session_state["tl"], st.session_state["iid"])
    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
    if st.button("↩ New Analysis", key="reset_btn"):
        st.session_state.pop("pm", None)
        st.session_state.pop("tl", None)
        st.session_state.pop("iid", None)
        st.rerun()

# FOOTER
st.markdown("""
<div style="text-align:center;padding:4rem 0 2rem;
            font-family:'DM Mono',monospace;font-size:0.6rem;
            color:#c4bdb4;letter-spacing:0.1em;">
  INFRALENS · LANGGRAPH · CHROMADB · GPT-4O · FASTAPI · STREAMLIT
</div>
""", unsafe_allow_html=True)

st.markdown('</div>', unsafe_allow_html=True)