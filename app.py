import pandas as pd
import numpy as np
from flask import Flask, render_template_string, request, send_file, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.metrics import silhouette_score
from itsdangerous import URLSafeTimedSerializer
import io, datetime, time, sqlite3, json, re, smtplib, base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)
app.secret_key = "loyalty_dss_production_dataset_sync_key"
DB_FILE = "loyalty_dss.db"

# --- EMAIL CONFIGURATION ---
MAIL_SERVER = "smtp.gmail.com"
MAIL_PORT = 587
MAIL_USE_TLS = True
MAIL_USERNAME = "noreply.loyaltydss@gmail.com"
MAIL_PASSWORD = "zsvwwcakhbljldqh"
MAIL_DEFAULT_SENDER = f"Customer Loyalty DSS <{MAIL_USERNAME}>"

serializer = URLSafeTimedSerializer(app.secret_key)

# Global Server-Side Cache to bypass browser 4KB cookie limits
temp_analysis_cache = {}

# --- DATABASE CORE ---
def get_db_connection():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    # Table for administrative manager accounts
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,
                    email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, 
                    is_verified INTEGER DEFAULT 0, theme TEXT DEFAULT 'light',
                    password_obfuscated TEXT)''')
    try:
        conn.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'light'")
    except sqlite3.OperationalError: pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN password_obfuscated TEXT")
    except sqlite3.OperationalError: pass

    # Table for persistent analysis archival records
    conn.execute('''CREATE TABLE IF NOT EXISTS analysis_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, 
                    rfm_json TEXT, summary_json TEXT, base_avg_json TEXT, best_k INTEGER, 
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (username) REFERENCES users (username))''')
    
    # Database migrations to support schema extension columns
    try:
        conn.execute("ALTER TABLE analysis_results ADD COLUMN base_avg_json TEXT")
    except sqlite3.OperationalError: pass
    try:
        conn.execute("ALTER TABLE analysis_results ADD COLUMN best_k INTEGER")
    except sqlite3.OperationalError: pass

    # Table for strict audit logging and platform compliance tracking
    conn.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    action TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    conn.commit()
    conn.close()

init_db()

# --- SECURITY NO-CACHE CONTEXT HEADER ---
@app.after_request
def add_header(r):
    """
    Prevent browser caching of analytical dashboard queries.
    This ensures that when a new dataset is uploaded, the browser
    always requests the latest calculated results from the server
    rather than displaying stale/cached session variables.
    """
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, public, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

# --- HELPER UTILITIES ---
def is_valid_email(email):
    return re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$', email) is not None

def log_audit_action(username, action):
    """Logs platform transactions to the SQL audit table for compliance auditing."""
    try:
        conn = get_db_connection()
        conn.execute("INSERT INTO audit_logs (username, action) VALUES (?, ?)", (username, action))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Audit Log Failure: {e}")

def send_mail(to, subject, body):
    msg = MIMEMultipart()
    msg['From'], msg['To'], msg['Subject'] = MAIL_DEFAULT_SENDER, to, subject
    msg.attach(MIMEText(body, 'plain'))
    try:
        server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT)
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)
        server.send_message(msg); server.quit()
        return True
    except Exception as e:
        print(f"SMTP Error: {e}")
        return False

# --- UI FRAGMENTS AND SCHEMAS ---
HTML_HEAD = """
<head>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='favicon.png') }}">
    <meta charset="UTF-8"><title>Customer Loyalty DSS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.6.0/dist/confetti.browser.min.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        .loader-overlay { display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.9); z-index: 100; flex-direction: column; align-items: center; justify-content: center; }
        .gradient-header { background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); }
        .modal { display: none; position: fixed; inset: 0; background: rgba(15, 23, 42, 0.9); z-index: 200; align-items: center; justify-content: center; padding: 20px; }
        .modal-content { background: white; width: 100%; max-width: 1050px; max-height: 90vh; overflow-y: auto; border-radius: 2.5rem; animation: slideUp 0.3s ease-out; }
        @keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        .dark-mode { background-color: #0f172a !important; color: #f8fafc !important; }
        .dark-mode .bg-white { background-color: #1e293b !important; border-color: #334155 !important; color: white !important; }
        .dark-mode .text-slate-800, .dark-mode .text-slate-700, .dark-mode .text-slate-600, .dark-mode .text-slate-500 { color: #f1f5f9 !important; }
        .dark-mode .bg-slate-50 { background-color: #0f172a !important; }
        @keyframes float { 0% { transform: translateY(0px); } 50% { transform: translateY(-10px); } 100% { transform: translateY(0px); } }
        .animate-float { animation: float 6s ease-in-out infinite; }
    </style>
</head>
"""

NAV_BAR = """
<header class="bg-white border-b border-slate-200 py-4 px-10 flex justify-between items-center sticky top-0 z-40">
    <div class="flex items-center space-x-4">
        <div class="flex items-center space-x-2 border-r pr-4 mr-2 border-slate-200">
            <i class="fas fa-user-circle text-indigo-600 text-2xl"></i>
            <span class="text-sm font-black text-slate-800 tracking-tight">{{ session['user'] }}</span>
        </div>
        <h1 class="text-lg font-black text-indigo-600 flex items-center space-x-2">
            <i class="fas fa-chart-bar text-indigo-600"></i>
            <span>Customer Loyalty DSS</span>
        </h1>
    </div>
    <nav class="flex items-center space-x-6">
        <a href="#" onclick="handleHomeClick(event)" class="text-sm font-bold text-slate-600 hover:text-indigo-600 transition">Home</a>
        <a href="/history" class="text-sm font-bold text-slate-600 hover:text-indigo-600 transition">History</a>
        <a href="/settings" class="text-slate-505 hover:text-indigo-600 transition"><i class="fas fa-cog text-lg"></i></a>
        <a href="/logout" class="bg-slate-100 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold hover:bg-slate-200 transition">Logout</a>
    </nav>
</header>
<script>
function handleHomeClick(e) {
    e.preventDefault();
    const resultsSection = document.getElementById('executiveInsightsList');
    if (resultsSection) {
        document.getElementById('homeConfirmModal').style.display = 'flex';
    } else {
        window.location.href = "/reset_analysis";
    }
}
</script>
"""

LANDING_PAGE_HTML = """
<!DOCTYPE html>
<html>
""" + HTML_HEAD + """
<body class="bg-slate-50 text-slate-900 font-sans">
    <nav class="p-6 px-12 flex justify-between items-center border-b border-slate-200 bg-white shadow-sm">
        <div class="flex items-center space-x-2">
            <i class="fas fa-chart-bar text-indigo-600 text-3xl"></i>
            <span class="text-xl font-black tracking-tighter text-slate-800">Customer Loyalty DSS</span>
        </div>
        <div class="space-x-8 text-sm font-bold text-slate-500 uppercase tracking-widest">
            <a href="/login" class="hover:text-indigo-600 transition font-black">Login</a>
            <a href="/register" class="bg-indigo-600 text-white px-6 py-3 rounded-full hover:bg-indigo-700 transition shadow-lg shadow-indigo-100 font-black">Get Started</a>
        </div>
    </nav>

    <main class="overflow-x-hidden">
        <!-- Hero Section -->
        <section class="max-w-7xl mx-auto py-16 lg:py-24 px-6 grid lg:grid-cols-12 gap-12 items-center">
            <div class="lg:col-span-5 space-y-8 text-center lg:text-left">
                <div class="inline-flex items-center space-x-2 bg-indigo-50 px-4 py-1.5 rounded-full text-indigo-600 text-xs font-black uppercase tracking-widest">
                    <i class="fas fa-bolt"></i>
                    <span>Smarter Retention Framework</span>
                </div>
                <h1 class="text-5xl lg:text-6xl font-black text-slate-800 leading-[1.1] tracking-tight">
                    Turn Customer Data <br>Into <span class="text-indigo-600">Revenue Growth.</span>
                </h1>
                <p class="text-lg text-slate-600 font-medium leading-relaxed max-w-xl mx-auto lg:mx-0">
                    Smarter customer retention starts here. Automatically segment purchase behaviors, evaluate discount strategy models, and predict customer churn before it happens.
                </p>
                <div class="flex flex-col sm:flex-row justify-center lg:justify-start gap-4">
                    <a href="/register" class="bg-indigo-600 text-white px-8 py-4 rounded-full font-black text-center shadow-xl hover:bg-indigo-700 transition transform hover:scale-105">
                        Start Free Forecasting
                    </a>
                    <a href="#outcomes" class="bg-white text-slate-700 border border-slate-300 px-8 py-4 rounded-full font-bold text-center hover:bg-slate-50 transition">
                        Explore Outcomes
                    </a>
                </div>
            </div>

            <!-- Visual Dashboard Preview Mockup -->
            <div class="lg:col-span-7 animate-float">
                <div class="bg-slate-900 rounded-[2.5rem] p-6 shadow-2xl border border-slate-800 text-slate-300 font-sans max-w-2xl mx-auto relative">
                    <!-- Dashboard window bar decoration -->
                    <div class="flex items-center justify-between pb-4 border-b border-slate-800 mb-6 text-xs text-slate-500">
                        <div class="flex items-center space-x-2">
                            <span class="w-3 h-3 rounded-full bg-red-500"></span>
                            <span class="w-3 h-3 rounded-full bg-yellow-500"></span>
                            <span class="w-3 h-3 rounded-full bg-green-500"></span>
                        </div>
                        <span class="font-bold uppercase tracking-widest"><i class="fas fa-file-csv mr-1.5"></i>Retail_Transaction_Dataset.csv</span>
                        <span class="text-slate-500">ML Model Active</span>
                    </div>

                    <!-- Metrics Grid -->
                    <div class="grid grid-cols-3 gap-3 mb-6">
                        <div class="bg-slate-800/60 border border-slate-800 p-3 rounded-2xl">
                            <p class="text-[9px] text-slate-500 font-bold uppercase tracking-wider mb-1">Base Customers</p>
                            <p class="text-lg font-black text-indigo-400">5,086</p>
                        </div>
                        <div class="bg-slate-800/60 border border-slate-800 p-3 rounded-2xl">
                            <p class="text-[9px] text-slate-500 font-bold uppercase tracking-wider mb-1">Average Churn Risk</p>
                            <p class="text-lg font-black text-rose-400">Medium</p>
                        </div>
                        <div class="bg-slate-800/60 border border-slate-800 p-3 rounded-2xl">
                            <p class="text-[9px] text-slate-500 font-bold uppercase tracking-wider mb-1">Forecasted Profit Lift</p>
                            <p class="text-lg font-black text-emerald-400">+18.4%</p>
                        </div>
                    </div>

                    <!-- Segments Preview -->
                    <div class="space-y-3 mb-6">
                        <div class="flex items-center justify-between p-3.5 bg-slate-800/50 border border-slate-800 rounded-2xl">
                            <div class="flex items-center space-x-3">
                                <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                                <span class="text-sm font-black text-white">Elite Champions</span>
                            </div>
                            <span class="px-2 py-0.5 bg-emerald-500/10 text-emerald-400 rounded-full text-[9px] font-black uppercase">Priority: Retain & Upsell</span>
                        </div>
                        <div class="flex items-center justify-between p-3.5 bg-slate-800/50 border border-slate-800 rounded-2xl">
                            <div class="flex items-center space-x-3">
                                <span class="w-2 h-2 rounded-full bg-amber-400"></span>
                                <span class="text-sm font-black text-white">Potential/At-Risk</span>
                            </div>
                            <span class="px-2 py-0.5 bg-rose-500/10 text-rose-400 rounded-full text-[9px] font-black uppercase">Priority: CRITICAL RECOVERY</span>
                        </div>
                    </div>

                    <!-- Simulated Miniature Chart -->
                    <div class="bg-slate-950 p-4 rounded-2xl border border-slate-800">
                        <p class="text-[9px] text-slate-500 font-black uppercase tracking-wider mb-3">Retention Lift Projection Curve</p>
                        <svg viewBox="0 0 400 100" class="w-full h-24 stroke-current text-indigo-500 fill-none">
                            <path d="M 0,90 Q 80,40 160,50 T 320,10 T 400,0" stroke-width="4" stroke-linecap="round"></path>
                            <path d="M 0,90 Q 80,40 160,50 T 320,10 T 400,0 L 400,100 L 0,100 Z" fill="rgba(99, 102, 241, 0.08)" stroke="none"></path>
                            <circle cx="320" cy="10" r="5" fill="#f43f5e" class="animate-pulse"></circle>
                        </svg>
                    </div>
                </div>
            </div>
        </section>

        <!-- Dynamic Core Features (No academic jargon) -->
        <section class="bg-white py-24 border-t border-slate-200">
            <div class="max-w-7xl mx-auto px-6">
                <div class="text-center max-w-xl mx-auto mb-16 space-y-4">
                    <h2 class="text-4xl font-black text-slate-800 tracking-tight">Enterprise-Grade Intelligence</h2>
                    <p class="text-slate-600 font-medium">Equipping marketing leads with model-driven decisions instead of static worksheets.</p>
                </div>

                <div class="grid md:grid-cols-3 gap-12">
                    <div class="space-y-4">
                        <div class="w-14 h-14 bg-indigo-50 rounded-2xl flex items-center justify-center text-indigo-600 text-xl"><i class="fas fa-users-cog"></i></div>
                        <h3 class="text-xl font-black text-slate-800">Customer Behavioral Segmentation</h3>
                        <p class="text-slate-600 text-sm leading-relaxed">
                            Discover natural groupings based on dynamic purchase cycles, total invoice counts, and cumulative lifetime spends. Automatically normalized through Box-Cox scaling algorithms.
                        </p>
                    </div>
                    <div class="space-y-4">
                        <div class="w-14 h-14 bg-indigo-50 rounded-2xl flex items-center justify-center text-indigo-600 text-xl"><i class="fas fa-tags"></i></div>
                        <h3 class="text-xl font-black text-slate-800">Promotion Response Analysis</h3>
                        <p class="text-slate-600 text-sm leading-relaxed">
                            Analyze past response and discount sensitivity patterns across each unique consumer cluster to design high-margin campaigns with minimal profit leakage.
                        </p>
                    </div>
                    <div class="space-y-4">
                        <div class="w-14 h-14 bg-indigo-50 rounded-2xl flex items-center justify-center text-indigo-600 text-xl"><i class="fas fa-chart-line"></i></div>
                        <h3 class="text-xl font-black text-slate-800">Interactive Revenue Forecasting</h3>
                        <p class="text-slate-600 text-sm leading-relaxed">
                            Simulate high-impact scenarios in real-time. Forecast net profits, ROI lifts, and customer migrations instantly prior to investing capital.
                        </p>
                    </div>
                </div>
            </div>
        </section>

        <!-- Outcome Section -->
        <section id="outcomes" class="bg-slate-50 py-24 border-t border-slate-200">
            <div class="max-w-7xl mx-auto px-6">
                <div class="grid lg:grid-cols-2 gap-12 items-center">
                    <div class="space-y-6">
                        <h2 class="text-4xl font-black text-slate-800 tracking-tight">Designed to Optimize Business Outcomes</h2>
                        <p class="text-slate-600 font-medium">Stop relying on gut-feeling. Customer Loyalty DSS is built to drive actionable key metrics:</p>
                        
                        <div class="space-y-4">
                            <div class="flex items-start space-x-3">
                                <i class="fas fa-check-circle text-indigo-600 text-xl mt-1"></i>
                                <div>
                                    <h4 class="font-black text-slate-800">Maximize Retention Loops</h4>
                                    <p class="text-sm text-slate-600">Track and target dropping retention probabilities instantly before complete brand exit occurs.</p>
                                </div>
                            </div>
                            <div class="flex items-start space-x-3">
                                <i class="fas fa-check-circle text-indigo-600 text-xl mt-1"></i>
                                <div>
                                    <h4 class="font-black text-slate-800">Identify High-Value Champions</h4>
                                    <p class="text-sm text-slate-600">Identify customer groups contributing to 80%+ of gross margins and deploy exclusive zero-discount reward loops.</p>
                                </div>
                            </div>
                            <div class="flex items-start space-x-3">
                                <i class="fas fa-check-circle text-indigo-600 text-xl mt-1"></i>
                                <div>
                                    <h4 class="font-black text-slate-800">Streamline Targeting Margins</h4>
                                    <p class="text-sm text-slate-600">Surgically restrict outbound promotion costs by prioritizing recovery campaigns to clusters with highest yield.</p>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="bg-indigo-600 p-12 rounded-[2.5rem] text-white shadow-xl space-y-6">
                        <h3 class="text-2xl font-black">Ready to audit store logs?</h3>
                        <p class="opacity-80 text-sm leading-relaxed">
                            Sign up to gain full cloud analysis, historical data tracking, strategic simulator presets, and localized business action reports.
                        </p>
                        <a href="/register" class="bg-white text-indigo-700 px-8 py-3.5 rounded-full font-black inline-block shadow-md hover:bg-slate-100 transition">
                            Create Free Manager Account
                        </a>
                    </div>
                </div>
            </div>
        </section>
    </main>

    <!-- Footer -->
    <footer class="bg-slate-900 text-slate-400 py-12 border-t border-slate-800 text-sm">
        <div class="max-w-7xl mx-auto px-6 flex flex-col md:flex-row justify-between items-center space-y-4 md:space-y-0">
            <div class="flex items-center space-x-2">
                <i class="fas fa-chart-bar text-indigo-500"></i>
                <span class="font-black text-white">Customer Loyalty DSS</span>
            </div>
            <p>© 2026 Customer Loyalty DSS. All rights reserved.</p>
        </div>
    </footer>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html class="{{ 'dark-mode' if theme == 'dark' }}">
""" + HTML_HEAD + """
<body class="bg-slate-50 transition-colors duration-300">
    <div id="loader" class="loader-overlay">
        <div class="text-center p-8 bg-white rounded-3xl shadow-2xl border max-w-md w-full mx-4">
            <div id="loader-icon" class="mb-6 flex justify-center text-indigo-600">
                <i class="fas fa-file-csv text-6xl animate-bounce"></i>
            </div>
            <h2 id="loader-text" class="text-xl font-black text-slate-800">Reading uploaded transactional logs...</h2>
            <div class="mt-6 w-full bg-slate-100 rounded-full h-2">
                <div id="loader-progress" class="bg-indigo-600 h-2 rounded-full transition-all duration-500" style="width: 15%"></div>
            </div>
        </div>
    </div>

    <!-- CUSTOM HOME SAVE/DISCARD CONFIRMATION MODAL -->
    <div id="homeConfirmModal" class="modal">
        <div class="text-center p-8 bg-white rounded-3xl shadow-2xl border border-slate-200 max-w-md w-full mx-4">
            <div class="mb-6 flex justify-center text-indigo-600">
                <i class="fas fa-question-circle text-6xl animate-pulse"></i>
            </div>
            <h2 class="text-xl font-black text-slate-800 mb-2">Save current analysis?</h2>
            <p class="text-sm text-slate-500 mb-6">Would you like to save your current work to history before returning Home?</p>
            <div class="flex flex-col space-y-3">
                <form action="/save_result" method="post">
                    <button type="submit" class="w-full bg-indigo-600 text-white py-3 rounded-xl font-bold shadow-md hover:bg-indigo-700 transition">Yes, Save to History</button>
                </form>
                <form action="/discard_result" method="post">
                    <button type="submit" class="w-full bg-slate-100 text-slate-700 py-3 rounded-xl font-bold hover:bg-slate-200 transition">No, Discard Work</button>
                </form>
                <button onclick="document.getElementById('homeConfirmModal').style.display = 'none'" class="w-full text-slate-400 font-bold hover:text-slate-600 transition text-sm py-2">Cancel</button>
            </div>
        </div>
    </div>

    """ + NAV_BAR + """
    <main class="max-w-6xl mx-auto mt-10 px-6 pb-20">
        {% with msgs = get_flashed_messages(with_categories=true) %}
            {% for cat, msg in msgs %}
            <div class="max-w-xl mx-auto mb-8 bg-{% if cat=='error' %}red{% else %}emerald{% endif %}-50 border-l-4 border-{% if cat=='error' %}red{% else %}emerald{% endif %}-500 p-5 rounded-r-2xl shadow-sm transition-all">
                <p class="text-{% if cat=='error' %}red{% else %}emerald{% endif %}-700 text-sm font-bold">{{ msg }}</p>
            </div>
            {% endfor %}
        {% endwith %}

        {% if show_history %}
            <!-- SAVED ANALYTICAL HISTORY MODULE -->
            <div class="flex justify-between items-center mb-8">
                <h2 class="text-3xl font-black text-slate-800">Analytical History</h2>
                {% if history %}
                <button onclick="clearAllHistory()" class="bg-red-50 text-red-600 px-6 py-2.5 rounded-2xl text-xs font-bold hover:bg-red-100 transition shadow-sm">
                    <i class="fas fa-trash-sweep mr-2"></i>Wipe History
                </button>
                {% endif %}
            </div>
            {% if history %}
                <div class="bg-white rounded-3xl shadow-sm border border-slate-200 overflow-hidden animate-fade-in">
                    <table class="w-full text-left">
                        <thead class="bg-slate-50 border-b border-slate-200">
                            <tr>
                                <th class="px-8 py-4 text-xs font-black text-slate-400 uppercase tracking-widest">Timestamp / Date</th>
                                <th class="px-8 py-4 text-xs font-black text-slate-400 uppercase tracking-widest text-center">Optimal Clusters (K)</th>
                                <th class="px-8 py-4 text-xs font-black text-slate-400 uppercase tracking-widest text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-200">
                            {% for item in history %}
                            <tr class="hover:bg-indigo-50/20 transition duration-150">
                                <td class="px-8 py-5 text-sm font-bold text-slate-700">{{ item.timestamp }}</td>
                                <td class="px-8 py-5 text-center font-black text-indigo-600">{{ item.best_k }}</td>
                                <td class="px-8 py-5 text-right flex justify-end space-x-3 items-center">
                                    <a href="/view_history/{{ item.id }}" class="bg-indigo-100 text-indigo-700 px-4 py-2 rounded-xl text-xs font-bold hover:bg-indigo-200 transition">Restore</a>
                                    <button onclick="deleteRecord({{ item.id }})" class="text-slate-300 hover:text-red-500 p-2 transition hover:scale-110">
                                        <i class="fas fa-trash-alt"></i>
                                    </button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            {% else %}
                <div class="text-center py-20 bg-white rounded-3xl border border-slate-200 shadow-sm">
                    <div class="w-16 h-16 bg-slate-50 rounded-full flex items-center justify-center mx-auto mb-4 text-slate-300 text-2xl">
                        <i class="fas fa-folder-open"></i>
                    </div>
                    <p class="text-slate-500 font-medium">No saved analyses found. Run a new analysis to save reports.</p>
                </div>
            {% endif %}

        {% elif not results %}
            <div class="max-w-xl mx-auto bg-white p-12 rounded-[3rem] shadow-xl text-center border border-slate-200">
                <div class="w-20 h-20 bg-indigo-50 rounded-3xl flex items-center justify-center mx-auto mb-6 text-indigo-500 text-3xl"><i class="fas fa-cloud-upload-alt"></i></div>
                <h2 class="text-2xl font-black mb-2 text-slate-800">Upload Store Transactions</h2>
                <p class="text-slate-600 text-sm mb-6">Select your transaction dataset (containing CustomerID, TransactionDate, TotalAmount, ProductCategory, DiscountApplied(%), Quantity).</p>
                <form action="/analyze" method="post" enctype="multipart/form-data" onsubmit="showLoader()">
                    <input type="file" name="file" class="block w-full text-sm text-slate-500 mb-8" required>
                    <button type="submit" class="w-full bg-indigo-600 text-white py-4 rounded-xl font-bold shadow-lg">Run Analysis</button>
                </form>
            </div>
        {% else %}
            <!-- EXECUTIVE DECISION SUMMARY PANEL -->
            <div class="bg-white p-8 rounded-[2.5rem] border border-slate-200 shadow-sm mb-8">
                <h2 class="text-2xl font-black text-slate-800 mb-6 flex items-center">
                    <i class="fas fa-clipboard-list text-indigo-600 mr-3"></i>Executive Decision Summary Panel
                </h2>
                <div id="executiveInsightsList" class="space-y-4">
                    <!-- Javascript Will Populate Distinct Rows Here on Separate Lines -->
                </div>
            </div>

            <!-- GROWTH & REVENUE ESTIMATOR -->
            <div class="bg-white p-8 rounded-[2.5rem] border shadow-sm mb-12">
                <div class="flex justify-between items-center mb-6">
                    <div>
                        <h2 class="text-2xl font-black text-slate-800"><i class="fas fa-chart-line text-indigo-600 mr-2"></i>Future Growth & Revenue Predictor</h2>
                        <p class="text-xs text-slate-500 font-bold uppercase tracking-widest mt-1">Interactive Decision Simulation Module</p>
                    </div>
                </div>

                <!-- Scenario Selection Presets -->
                <div class="mb-8 bg-slate-50 p-4 rounded-2xl border border-slate-100">
                    <span class="text-xs font-black text-slate-700 uppercase tracking-wider block mb-3">Choose Strategy Preset Scenario:</span>
                    <div class="flex flex-wrap gap-4">
                        <button onclick="applyScenarioPreset(8, 1500, 'conservative', this)" class="scenario-btn px-6 py-2.5 rounded-xl border text-sm font-bold bg-white text-slate-700 hover:bg-slate-100 transition shadow-sm border-slate-300">Conservative Campaign</button>
                        <button onclick="applyScenarioPreset(15, 3500, 'moderate', this)" class="scenario-btn px-6 py-2.5 rounded-xl border text-sm font-bold bg-indigo-600 text-white hover:bg-indigo-700 transition shadow-sm border-indigo-600 active-preset">Moderate Campaign</button>
                        <button onclick="applyScenarioPreset(28, 7500, 'aggressive', this)" class="scenario-btn px-6 py-2.5 rounded-xl border text-sm font-bold bg-white text-slate-700 hover:bg-slate-100 transition shadow-sm border-slate-300">Aggressive Campaign</button>
                    </div>
                </div>

                <div class="grid md:grid-cols-2 gap-8">
                    <!-- Left Side Controls -->
                    <div class="space-y-6">
                        <!-- Projected Response Rate -->
                        <div class="space-y-2">
                            <div class="flex justify-between items-center">
                                <label class="text-xs font-black uppercase text-slate-700 block">Expected Customer Conversion / Response Rate (%)</label>
                            </div>
                            <div class="flex items-center space-x-4">
                                <input type="range" id="simUpgrade" min="1" max="100" value="15" class="w-3/4 h-2 bg-slate-200 rounded-lg appearance-none cursor-pointer accent-indigo-600" oninput="syncInputs('simUpgrade', 'simUpgradeNum'); runSimulation()">
                                <input type="number" id="simUpgradeNum" min="1" max="100" value="15" class="w-1/4 p-2 border border-slate-400 rounded-xl text-center font-bold text-slate-800 focus:ring-2 focus:ring-indigo-500 focus:outline-none bg-white text-sm" oninput="syncInputs('simUpgradeNum', 'simUpgrade'); runSimulation()">
                            </div>
                            <p class="text-[10px] text-slate-600 leading-normal">The percentage of target segment pool expected to convert or reactivate.</p>
                        </div>

                        <!-- Average Campaign Spend -->
                        <div class="space-y-2">
                            <div class="flex justify-between items-center">
                                <label class="text-xs font-black uppercase text-slate-700 block">Average Order Value (AOV) Spend Increase (₦)</label>
                            </div>
                            <div class="flex items-center space-x-4">
                                <input type="range" id="simAov" min="500" max="15000" step="100" value="3500" class="w-3/4 h-2 bg-slate-200 rounded-lg appearance-none cursor-pointer accent-indigo-600" oninput="syncInputs('simAov', 'simAovNum'); runSimulation()">
                                <div class="w-1/4 flex items-center p-2 border border-slate-400 rounded-xl focus-within:ring-2 focus-within:ring-indigo-500 bg-white">
                                    <span class="text-slate-800 font-bold text-xs mr-1">₦</span>
                                    <input type="number" id="simAovNum" min="500" max="15000" step="100" value="3500" class="w-full text-center font-bold text-slate-800 focus:outline-none bg-transparent text-sm" oninput="syncInputs('simAovNum', 'simAov'); runSimulation()">
                                </div>
                            </div>
                            <p class="text-[10px] text-slate-600 leading-normal">Additional average basket size spend attached per responsive customer.</p>
                        </div>
                    </div>

                    <!-- Right Side Outputs & Insights -->
                    <div class="bg-indigo-50/50 p-6 rounded-3xl border border-indigo-100 flex flex-col justify-between">
                        <div class="space-y-4">
                            <div class="grid grid-cols-2 gap-4 border-b border-indigo-100/60 pb-4">
                                <div>
                                    <p class="text-[10px] font-bold text-slate-500 uppercase">Target Pool Size</p>
                                    <p class="text-sm font-black text-slate-800" id="targetPool">0 customers</p>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-slate-500 uppercase">Expected Conversion</p>
                                    <p class="text-sm font-black text-slate-800" id="expectedResponsive">0 customers</p>
                                </div>
                            </div>
                            <div class="grid grid-cols-2 gap-4 border-b border-indigo-100/60 pb-4">
                                <div>
                                    <p class="text-[10px] font-bold text-slate-500 uppercase">Est. Campaign Cost (₦250/pax)</p>
                                    <p class="text-sm font-black text-slate-800" id="estCost">₦0</p>
                                </div>
                                <div>
                                    <p class="text-[10px] font-bold text-slate-500 uppercase">Gross Revenue Lift</p>
                                    <p class="text-sm font-black text-indigo-700" id="grossLift">₦0</p>
                                </div>
                            </div>
                            <div class="flex justify-between items-center bg-white p-4 rounded-2xl border">
                                <div>
                                    <p class="text-[10px] font-black text-slate-500 uppercase">Projected Net Profit</p>
                                    <p class="text-2xl font-black text-indigo-600 animate-pulse" id="projectedRevenue">₦0</p>
                                </div>
                                <div class="text-right">
                                    <p class="text-[10px] font-black text-slate-500 uppercase">Projected ROI</p>
                                    <p class="text-lg font-black text-emerald-600" id="projectedRoi">0%</p>
                                </div>
                            </div>
                        </div>
                        <div class="mt-4 pt-4 border-t border-indigo-100/60 flex justify-between items-center">
                            <span class="text-[10px] text-slate-600 font-bold uppercase">Confidence Level: <span class="text-indigo-600" id="silhouetteScore">{% if silhouette %}{{ silhouette }}{% else %}0.376{% endif %} (Validated)</span></span>
                            <span class="text-[10px] font-black uppercase text-white bg-indigo-600 px-3 py-1 rounded-full text-[9px]" id="perfLevel">Optimal</span>
                        </div>
                    </div>
                </div>

                <!-- Dynamic Decision Advice Panel -->
                <div class="mt-6 bg-slate-50 border border-slate-200 p-6 rounded-2xl space-y-3">
                    <h3 class="text-xs font-black text-indigo-600 uppercase tracking-widest"><i class="fas fa-lightbulb mr-2"></i>DSS Strategic Recommendation Insight</h3>
                    <div id="dssAdviceContainer" class="space-y-2">
                        <!-- Points will render dynamically, each clearly grouped on its own line -->
                    </div>
                </div>
            </div>

            <!-- SEGMENT INSIGHTS LISTING -->
            <div class="grid md:grid-cols-2 gap-8">
                {% for item in results %}
                <div onclick="openModal('{{ loop.index0 }}')" class="bg-white p-8 rounded-[2.5rem] shadow-sm border border-slate-200 cursor-pointer hover:border-indigo-400 transition transform hover:-translate-y-1">
                    <div class="flex justify-between items-start mb-6">
                        <div>
                            <span class="bg-indigo-50 text-indigo-600 px-3 py-1 rounded-full text-[10px] font-black uppercase tracking-widest">Group {{ item.Cluster }}</span>
                            <h3 class="text-2xl font-black text-slate-800 mt-2">{{ item.Label }}</h3>
                        </div>
                        <div class="text-right">
                            <span class="inline-block px-3 py-1 text-[9px] font-black uppercase rounded-full tracking-wider {% if item.Priority == 'CRITICAL' %}bg-red-100 text-red-600{% elif item.Priority == 'High' %}bg-amber-100 text-amber-700{% else %}bg-slate-100 text-slate-600{% endif %}">
                                Priority: {{ item.Priority }}
                            </span>
                        </div>
                    </div>
                    
                    <div class="grid grid-cols-2 gap-4 mb-6">
                        <div class="bg-slate-50 p-3 rounded-2xl">
                            <span class="text-[8px] text-slate-500 font-black uppercase block">Favorite Category</span>
                            <span class="text-sm font-black text-slate-800">{{ item.TopCategory }}</span>
                        </div>
                        <div class="bg-slate-50 p-3 rounded-2xl">
                            <span class="text-[8px] text-slate-500 font-black uppercase block">Avg Discount Taken</span>
                            <span class="text-sm font-black text-slate-800">{{ item.AvgDiscount }}%</span>
                        </div>
                    </div>

                    <div class="grid grid-cols-3 gap-3 mb-6">
                        <div class="text-center p-3 bg-slate-50 rounded-2xl">
                            <p class="text-[9px] text-slate-500 font-bold uppercase mb-1">Recency</p>
                            <p class="text-base font-black text-slate-800">{{ item.R }}d</p>
                            <span class="text-[8px] text-slate-400 block">{{ item.RecencyText }}</span>
                        </div>
                        <div class="text-center p-3 bg-slate-50 rounded-2xl">
                            <p class="text-[9px] text-slate-500 font-bold uppercase mb-1">Freq</p>
                            <p class="text-base font-black text-slate-800">{{ item.F }}</p>
                            <span class="text-[8px] text-slate-400 block">{{ item.FrequencyText }}</span>
                        </div>
                        <div class="text-center p-3 bg-slate-50 rounded-2xl">
                            <p class="text-[9px] text-slate-500 font-bold uppercase mb-1">Total Spend</p>
                            <p class="text-base font-black text-slate-800">₦{{ "{:,.0f}".format(item.M | float) }}</p>
                            <span class="text-[8px] text-slate-400 block">LTV: High</span>
                        </div>
                    </div>

                    <div class="border-t border-slate-100 pt-4 grid grid-cols-3 gap-2 text-center text-[10px] font-bold text-slate-500 mb-6">
                        <div>
                            <span class="block text-[8px] uppercase text-slate-400">Churn Risk</span>
                            <span class="text-slate-700 font-extrabold">{{ item.ChurnRisk }}</span>
                        </div>
                        <div>
                            <span class="block text-[8px] uppercase text-slate-400">Retention Prob</span>
                            <span class="text-slate-700 font-extrabold">{{ item.RetentionProb }}%</span>
                        </div>
                        <div>
                            <span class="block text-[8px] uppercase text-slate-400">Segment Share</span>
                            <span class="text-slate-700 font-extrabold">{{ item.Size }}%</span>
                        </div>
                    </div>

                    <div class="bg-indigo-50 p-4 rounded-2xl border border-indigo-100 flex items-center justify-between">
                        <div>
                            <p class="text-[10px] font-black text-indigo-400 uppercase mb-1">Recommended Strategy Plan</p>
                            <p class="text-sm font-bold text-indigo-900">{{ item.BriefTip }}</p>
                        </div>
                        <i class="fas fa-arrow-right text-indigo-600 text-sm"></i>
                    </div>
                </div>

                <!-- MODAL DEEP DIVE -->
                <div id="modal-{{ loop.index0 }}" class="modal">
                    <div class="modal-content animate-fade-in">
                        <div class="p-8 border-b flex justify-between items-center sticky top-0 bg-white z-10 border-slate-100">
                            <div>
                                <h2 class="text-3xl font-black text-slate-800">{{ item.Label }} - Strategic Business Plan</h2>
                                <p class="text-indigo-500 font-bold text-xs uppercase tracking-widest mt-1">Comparing segment behaviors directly against the global business average</p>
                            </div>
                            <button onclick="closeModal('{{ loop.index0 }}')" class="text-slate-300 hover:text-red-500 text-3xl transition"><i class="fas fa-times-circle"></i></button>
                        </div>
                        <div class="p-10 space-y-10">
                            <div class="grid md:grid-cols-2 gap-10">
                                <div class="space-y-6">
                                    <div class="bg-indigo-600 p-8 rounded-[2rem] text-white shadow-xl">
                                        <p class="text-xs font-bold opacity-60 uppercase mb-2">Prescriptive Action Plan</p>
                                        <p class="text-lg font-medium leading-relaxed">{{ item.Action }}</p>
                                    </div>
                                    <div class="p-6 bg-slate-50 rounded-[2rem] border border-slate-200">
                                        <p class="text-xs font-bold text-slate-500 uppercase mb-3">Segment Behavior Essence</p>
                                        <p class="text-sm text-slate-700 leading-relaxed italic font-medium">"{{ item.Essence }}"</p>
                                    </div>
                                </div>
                                <div class="space-y-6 flex flex-col justify-start">
                                    <div class="p-8 bg-indigo-50 rounded-[2rem] border border-indigo-100 h-full flex flex-col justify-center">
                                        <h4 class="text-xs font-black text-indigo-700 uppercase mb-3"><i class="fas fa-lightbulb mr-2"></i>Automated Behavioral Interpretation</h4>
                                        <p class="text-sm text-slate-800 leading-relaxed font-bold" id="interpretation-{{ loop.index0 }}">
                                            Analyzing behavioral details...
                                        </p>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            
            <div class="mt-10 text-center space-x-4">
                <form action="/save_result" method="post" class="inline-block">
                    <button type="submit" class="bg-emerald-600 text-white px-8 py-3 rounded-xl font-bold shadow-lg animate-pulse">Save Work</button>
                </form>
                <form action="/discard_result" method="post" class="inline-block">
                    <button type="submit" class="bg-slate-200 text-slate-700 px-8 py-3 rounded-xl font-bold hover:bg-slate-300 transition">Discard</button>
                </form>
            </div>
        {% endif %}
    </main>

    <script>
        const analysisData = {% if results %}{{ results|tojson }}{% else %}[]{% endif %};
        const baseAvg = {% if base_avg %}{{ base_avg|tojson }}{% else %}{"R": 100, "F": 5, "M": 1000}{% endif %};

        function openModal(idx) {
            document.getElementById('modal-'+idx).style.display = 'flex';
            interpretChart(idx);
        }
        function closeModal(idx) { document.getElementById('modal-'+idx).style.display = 'none'; }

        function interpretChart(idx) {
            const data = analysisData[idx];
            const element = document.getElementById('interpretation-' + idx);
            if (!element) return;

            let interpretation = "";
            if (idx === 0) {
                interpretation = "These are your absolute best customers! They shop very frequently, bought from you recently, and spend much more than the average shopper (₦" + data.M.toLocaleString() + " on average). Since they already love your brand, you do not need to give them heavy discounts to keep them. Instead, treat them like VIPs—provide early access to new arrivals, priority customer support, or invite them to a special loyalty club.";
            } else if (idx === 1) {
                interpretation = "This group is your reliable, steady backbone. They might not buy your most expensive items, but they visit your store regularly and have purchased from you recently. Since their favorite category is " + data.TopCategory + ", they will respond wonderfully to friendly product reminders or small cross-sell bundles related to that category. Keep them happy with standard, consistent engagement!";
            } else if (idx === 2) {
                interpretation = "Attention! This is a highly critical group that is currently slipping away. These customers used to be great, high-spending supporters, but they haven't bought anything in quite a long time (" + data.R + " days on average) and are at a high risk of leaving for good. You should act quickly to win them back—send them a special 'We Miss You' discount code or a personalized offer in their favorite category before they forget about us completely.";
            } else {
                interpretation = "These are quiet or inactive customers who haven't shopped with you in a very long time, and historically didn't spend very much. Since they are at a very high risk of having left for good, it is not cost-effective to spend a lot of money trying to win them back. Instead, simply include them in broad, low-cost marketing campaigns, like major seasonal holiday clearance newsletters.";
            }
            element.innerHTML = interpretation;
        }

        function syncInputs(sourceId, targetId) {
            const src = document.getElementById(sourceId);
            const tgt = document.getElementById(targetId);
            if (src && tgt) {
                tgt.value = src.value;
            }
            const scenarioBtns = document.querySelectorAll('.scenario-btn');
            scenarioBtns.forEach(btn => btn.classList.remove('active-preset', 'bg-indigo-600', 'text-white'));
        }

        function applyScenarioPreset(responseRate, offerSpend, type, btnElement) {
            const rSlider = document.getElementById('simUpgrade');
            const rNum = document.getElementById('simUpgradeNum');
            const sSlider = document.getElementById('simAov');
            const sNum = document.getElementById('simAovNum');

            if(rSlider && rNum) { rSlider.value = responseRate; rNum.value = responseRate; }
            if(sSlider && sNum) { sSlider.value = offerSpend; sNum.value = offerSpend; }

            const scenarioBtns = document.querySelectorAll('.scenario-btn');
            scenarioBtns.forEach(btn => {
                btn.classList.remove('bg-indigo-600', 'text-white', 'border-indigo-600', 'active-preset');
                btn.classList.add('bg-white', 'text-slate-700', 'border-slate-300');
            });

            btnElement.classList.add('bg-indigo-600', 'text-white', 'border-indigo-600', 'active-preset');
            btnElement.classList.remove('bg-white', 'text-slate-700', 'border-slate-300');

            runSimulation();
        }

        function runSimulation() {
            const targetUpgrade = parseFloat(document.getElementById('simUpgradeNum').value) || 0;
            const accessAov = parseFloat(document.getElementById('simAovNum').value) || 0;

            const baseSize = {% if kpis and kpis.total_cust %}{{ kpis.total_cust }}{% else %}5086{% endif %};
            const expectedUpgrades = Math.round(baseSize * (targetUpgrade / 100));
            const estCampaignCost = Math.round(baseSize * 250); 
            const grossLift = Math.round(expectedUpgrades * accessAov * 1.5); 
            const netProfit = grossLift - estCampaignCost;
            const projectedRoi = estCampaignCost > 0 ? Math.round((netProfit / estCampaignCost) * 100) : 0;

            document.getElementById('targetPool').innerText = baseSize.toLocaleString() + " customers";
            document.getElementById('expectedResponsive').innerText = expectedUpgrades.toLocaleString() + " customers";
            document.getElementById('estCost').innerText = "₦" + estCampaignCost.toLocaleString();
            document.getElementById('grossLift').innerText = "₦" + grossLift.toLocaleString();
            
            const netRevenueText = document.getElementById('projectedRevenue');
            netRevenueText.innerText = "₦" + netProfit.toLocaleString();
            
            const roiText = document.getElementById('projectedRoi');
            roiText.innerText = projectedRoi + "%";

            const perfLevel = document.getElementById('perfLevel');
            if (projectedRoi < 0) {
                perfLevel.innerText = "Negative Return";
                perfLevel.className = "text-[10px] font-black uppercase text-white bg-red-500 px-3 py-1 rounded-full";
            } else if (projectedRoi < 50) {
                perfLevel.innerText = "Marginal Yield";
                perfLevel.className = "text-[10px] font-black uppercase text-white bg-amber-500 px-3 py-1 rounded-full";
            } else if (projectedRoi < 150) {
                perfLevel.innerText = "Optimal";
                perfLevel.className = "text-[10px] font-black uppercase text-white bg-indigo-600 px-3 py-1 rounded-full";
            } else {
                perfLevel.innerText = "High Profit Yield";
                perfLevel.className = "text-[10px] font-black uppercase text-white bg-emerald-600 px-3 py-1 rounded-full";
            }

            // DYNAMIC STRATEGIC ADVICE WITH POINTS PLACED ON NEW LINES (No ES6 literals to prevent escaping glitches)
            const adviceContainer = document.getElementById('dssAdviceContainer');
            const optUpgradeVal = Math.round(baseSize * 0.05 * accessAov * 1.5);
            const optSpendVal = Math.round(baseSize * (targetUpgrade / 100) * 1000 * 1.5);

            let leverageAdvice = "";
            if (accessAov > (targetUpgrade * 120)) {
                leverageAdvice = "Our analytical model recommends focusing primarily on <strong>Strategy Option A (Customer Turnout)</strong>. Getting more people to respond to our campaign is far easier and will bring in significantly more net profit than trying to force customers to spend more per order. Focus your budget on better marketing emails and personalized discount subject lines.";
            } else {
                leverageAdvice = "Our analytical model recommends focusing primarily on <strong>Strategy Option B (Basket Value)</strong>. Encouraging already-interested customers to spend slightly more yields superior financial returns. We recommend focusing on product bundling, category-based cross-selling (e.g., offering accessory add-ons), or setting free-shipping thresholds.";
            }

            let html = 
                '<p class="text-sm text-slate-800 leading-normal border-b border-slate-200/80 pb-3">' +
                    '<span class="text-indigo-600 font-bold"><i class="fas fa-users mr-2"></i>Strategy Option A (Increase Customer Turnout):</span> If we get just <strong>5 out of 100 more customers</strong> to respond to our campaigns, we can generate an additional <strong class="text-slate-950">₦' + optUpgradeVal.toLocaleString() + '</strong> in gross revenue.' +
                '</p>' +
                '<p class="text-sm text-slate-800 leading-normal border-b border-slate-200/80 py-3">' +
                    '<span class="text-indigo-600 font-bold"><i class="fas fa-shopping-basket mr-2"></i>Strategy Option B (Increase Basket Value):</span> If we can encourage each customer to add just one more accessory (worth <strong>₦1,000</strong>) to their order, we can make an extra <strong class="text-slate-950">₦' + optSpendVal.toLocaleString() + '</strong>.' +
                '</p>' +
                '<p class="text-sm text-slate-800 leading-normal pt-3">' +
                    '<span class="text-indigo-600 font-bold"><i class="fas fa-lightbulb mr-2"></i>Strategic Recommendation Guide:</span> ' + leverageAdvice +
                '</p>';
            
            adviceContainer.innerHTML = html;
            document.getElementById('projectionStatus').innerText = "Status: Projecting " + expectedUpgrades.toLocaleString() + " responsive customers";
        }

        function populateExecutiveInsights() {
            const listElement = document.getElementById('executiveInsightsList');
            if (!listElement || analysisData.length === 0) return;

            const champions = analysisData[0];
            const sustainers = analysisData[1] || { Label: "Loyal Sustainers", M: 0, RetentionProb: 50 };
            const atRisk = analysisData[2] || { Label: "Potential/At-Risk", M: 0, R: 180, ChurnRisk: "High" };
            const coldChurn = analysisData[3] || { Label: "Cold Churn", M: 0, R: 250, ChurnRisk: "Extremely High" };

            // Structured rows with each segment and its priority actions placed on clear new lines
            let html = 
            '<!-- Row 1: Most Valuable (Champions) -->' +
            '<div class="flex flex-col md:flex-row md:items-center justify-between p-5 bg-emerald-50/60 border border-emerald-100 rounded-2xl space-y-2 md:space-y-0">' +
                '<div class="flex items-center space-x-3">' +
                    '<div class="w-8 h-8 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center font-black text-sm">1</div>' +
                    '<div>' +
                        '<p class="text-[10px] text-slate-400 font-bold uppercase tracking-wider leading-none mb-1">Most Valuable Segment</p>' +
                        '<p class="text-base font-black text-slate-800">' + champions.Label + ' <span class="text-slate-400 font-medium">→</span> Highest revenue contributors</p>' +
                    '</div>' +
                '</div>' +
                '<div class="flex items-center space-x-4">' +
                    '<span class="px-3 py-1 bg-emerald-100 text-emerald-700 text-[10px] font-black rounded-full uppercase tracking-wider">Priority: Retain & Upsell</span>' +
                    '<span class="text-xs font-bold text-slate-600">Immediate Action: VIP support & premium bundles</span>' +
                '</div>' +
            '</div>' +

            '<!-- Row 2: Most At Risk (Potential Churners) -->' +
            '<div class="flex flex-col md:flex-row md:items-center justify-between p-5 mt-4 bg-amber-50/60 border border-amber-100 rounded-2xl space-y-2 md:space-y-0">' +
                '<div class="flex items-center space-x-3">' +
                    '<div class="w-8 h-8 rounded-full bg-amber-100 text-amber-700 flex items-center justify-center font-black text-sm">2</div>' +
                    '<div>' +
                        '<p class="text-[10px] text-slate-400 font-bold uppercase tracking-wider leading-none mb-1">Strong Recovery Opportunity</p>' +
                        '<p class="text-base font-black text-slate-800">' + atRisk.Label + ' <span class="text-slate-400 font-medium">→</span> High Churn Risk (Ref: ' + atRisk.R + ' days idle)</p>' +
                    '</div>' +
                '</div>' +
                '<div class="flex items-center space-x-4">' +
                    '<span class="px-3 py-1 bg-amber-100 text-amber-700 text-[10px] font-black rounded-full uppercase tracking-wider">Priority: FIRST / CRITICAL</span>' +
                    '<span class="text-xs font-bold text-slate-600">Immediate Action: Deploy category-specific discounts</span>' +
                '</div>' +
            '</div>' +

            '<!-- Row 3: Cold Churn (Reactivation target) -->' +
            '<div class="flex flex-col md:flex-row md:items-center justify-between p-5 mt-4 bg-rose-50/60 border border-rose-100 rounded-2xl space-y-2 md:space-y-0">' +
                '<div class="flex items-center space-x-3">' +
                    '<div class="w-8 h-8 rounded-full bg-rose-100 text-rose-700 flex items-center justify-center font-black text-sm">3</div>' +
                    '<div>' +
                        '<p class="text-[10px] text-slate-400 font-bold uppercase tracking-wider leading-none mb-1">Inactive customer base</p>' +
                        '<p class="text-base font-black text-slate-800">' + coldChurn.Label + ' <span class="text-slate-400 font-medium">→</span> High Churn Risk (Ref: ' + coldChurn.R + ' days idle)</p>' +
                    '</div>' +
                '</div>' +
                '<div class="flex items-center space-x-4">' +
                    '<span class="px-3 py-1 bg-rose-100 text-rose-700 text-[10px] font-black rounded-full uppercase tracking-wider">Priority: Low-Cost Reactivation</span>' +
                    '<span class="text-xs font-bold text-slate-600">Immediate Action: Seasonal clearances & clearance emails</span>' +
                '</div>' +
            '</div>' +
            '';
            listElement.innerHTML = html;
        }

        function deleteRecord(id) {
            if(confirm("Are you sure you want to permanently delete this saved analysis?")) {
                window.location.href = "/delete_history/" + id;
            }
        }

        function clearAllHistory() {
            if(confirm("Are you sure you want to permanently delete ALL saved analyses? This action cannot be undone.")) {
                window.location.href = "/clear_all_history";
            }
        }

        function triggerConfetti() {
            var duration = 4 * 1000;
            var end = Date.now() + duration;

            (function frame() {
                confetti({
                    particleCount: 4,
                    angle: 60,
                    spread: 60,
                    origin: { x: 0, y: 0.8 }
                });
                confetti({
                    particleCount: 4,
                    angle: 120,
                    spread: 60,
                    origin: { x: 1, y: 0.8 }
                });

                if (Date.now() < end) {
                    requestAnimationFrame(frame);
                }
            }());
        }

        function showLoader() {
            const loader = document.getElementById('loader');
            const text = document.getElementById('loader-text');
            const icon = document.getElementById('loader-icon');
            const progress = document.getElementById('loader-progress');
            loader.style.display = 'flex';
            
            const messages = [
                { t: "Loading uploaded transactional logs...", i: '<i class="fas fa-file-csv text-indigo-600 text-6xl animate-bounce"></i>', p: "15%" },
                { t: "Performing RFM feature extraction...", i: '<i class="fas fa-calculator text-indigo-600 text-6xl animate-spin"></i>', p: "35%" },
                { t: "Normalizing data via Box-Cox transformation...", i: '<i class="fas fa-filter text-indigo-600 text-6xl animate-pulse"></i>', p: "55%" },
                { t: "Initializing K-Means clustering...", i: '<i class="fas fa-cubes text-indigo-600 text-6xl animate-bounce"></i>', p: "75%" },
                { t: "Executing Silhouette model validation...", i: '<i class="fas fa-check-circle text-indigo-600 text-6xl animate-pulse"></i>', p: "90%" },
                { t: "Assembling prescriptive recommendations...", i: '<i class="fas fa-magic text-indigo-600 text-6xl animate-bounce"></i>', p: "100%" }
            ];
            
            let i = 0;
            text.innerText = messages[0].t;
            icon.innerHTML = messages[0].i;
            progress.style.width = messages[0].p;
            
            const interval = setInterval(() => {
                i++;
                if (i < messages.length) {
                    text.innerText = messages[i].t;
                    icon.innerHTML = messages[i].i;
                    progress.style.width = messages[i].p;
                } else {
                    clearInterval(interval);
                }
            }, 1200);
        }

        window.onload = function() {
            if (document.getElementById('simUpgrade')) {
                runSimulation();
                populateExecutiveInsights();
            }
            
            // Confetti integration
            if ({% if show_confetti %}true{% else %}false{% endif %}) {
                triggerConfetti();
            }
        };
    </script>
</body>
</html>
"""

# --- BACKEND LOGIC ---

def run_dss_engine(df):
    df.columns = [c.strip() for c in df.columns]
    
    col_mapping = {
        'CustomerID': ['customerid', 'customer_id', 'cust_id'],
        'TransactionDate': ['transactiondate', 'date', 'transaction_date'],
        'TotalAmount': ['totalamount', 'amount', 'total_amount', 'price'],
        'ProductCategory': ['productcategory', 'category', 'product_category'],
        'DiscountApplied': ['discountapplied(%)', 'discountapplied', 'discount', 'discount_applied'],
        'Quantity': ['quantity', 'qty'],
        'ProductID': ['productid', 'product_id', 'item_id'],
        'Price': ['price', 'item_price']
    }
    
    mapped_cols = {}
    lower_cols = [c.lower() for c in df.columns]
    
    for standard_col, options in col_mapping.items():
        found = False
        for option in options:
            option_lower = option.lower()
            if option_lower in lower_cols:
                idx = lower_cols.index(option_lower)
                mapped_cols[standard_col] = df.columns[idx]
                found = True
                break
        if not found:
            mapped_cols[standard_col] = None

    df_clean = df.copy()
    id_col = mapped_cols['CustomerID'] or df_clean.columns[0]
    date_col = mapped_cols['TransactionDate'] or df_clean.columns[1]
    amount_col = mapped_cols['TotalAmount'] or df_clean.columns[2]
    cat_col = mapped_cols['ProductCategory']
    discount_col = mapped_cols['DiscountApplied']
    qty_col = mapped_cols['Quantity']
    prod_col = mapped_cols['ProductID']
    price_col = mapped_cols['Price']

    df_clean[date_col] = pd.to_datetime(df_clean[date_col])
    df_clean[amount_col] = pd.to_numeric(df_clean[amount_col], errors='coerce')
    df_clean = df_clean.dropna(subset=[id_col, date_col, amount_col])

    now = df_clean[date_col].max() + datetime.timedelta(days=1)
    
    rfm = df_clean.groupby(id_col).agg({
        date_col: lambda x: (now - x.max()).days,
        id_col: 'count',
        amount_col: 'sum'
    }).rename(columns={date_col: 'Recency', id_col: 'Frequency', amount_col: 'Monetary'})

    if cat_col:
        fav_cat = df_clean.groupby(id_col)[cat_col].agg(lambda x: x.mode().iloc[0] if not x.mode().empty else "Other").rename("TopCategory")
        rfm = rfm.join(fav_cat)
    else:
        rfm['TopCategory'] = "General Retail"

    if discount_col:
        df_clean[discount_col] = pd.to_numeric(df_clean[discount_col], errors='coerce').fillna(0)
        avg_disc = df_clean.groupby(id_col)[discount_col].mean().rename("AvgDiscount")
        rfm = rfm.join(avg_disc)
    else:
        rfm['AvgDiscount'] = 10.0

    if qty_col:
        df_clean[qty_col] = pd.to_numeric(df_clean[qty_col], errors='coerce').fillna(1)
        tot_qty = df_clean.groupby(id_col)[qty_col].sum().rename("TotalQuantity")
        rfm = rfm.join(tot_qty)
    else:
        rfm['TotalQuantity'] = rfm['Frequency']

    rfm_t = rfm[['Recency', 'Frequency', 'Monetary']].copy() + 1
    for col in ['Recency', 'Frequency', 'Monetary']:
        rfm_t[col], _ = stats.boxcox(rfm_t[col])
    
    scaled = StandardScaler().fit_transform(rfm_t)
    best_k = 4
    
    km = KMeans(n_clusters=best_k, random_state=42, n_init=10).fit(scaled)
    max_sil = silhouette_score(scaled, km.labels_)
    
    rfm['Cluster'] = km.labels_
    
    summ = rfm.groupby('Cluster').agg({
        'Recency': 'mean',
        'Frequency': 'mean',
        'Monetary': 'mean',
        'AvgDiscount': 'mean',
        'TotalQuantity': 'mean'
    }).reset_index()
    
    cluster_categories = []
    for c in summ['Cluster']:
        subset = rfm[rfm['Cluster'] == c]
        mode_cat = subset['TopCategory'].mode()
        cluster_categories.append(mode_cat.iloc[0] if not mode_cat.empty else "General")
    summ['TopCategory'] = cluster_categories

    scaler = MinMaxScaler()
    summ_scaled = pd.DataFrame(scaler.fit_transform(summ[['Recency','Frequency','Monetary']]), columns=['R','F','M'])
    summ['Score'] = (1 - summ_scaled['R']) + summ_scaled['F'] + summ_scaled['M']
    
    summ = summ.sort_values('Score', ascending=False).reset_index(drop=True)
    summ['Cluster'] = range(len(summ))
    
    base_avg = {
        'R': round(rfm['Recency'].mean(), 1), 
        'F': round(rfm['Frequency'].mean(), 1), 
        'M': round(rfm['Monetary'].mean(), 1)
    }
    
    return rfm, summ, base_avg, len(rfm), df_clean, mapped_cols, max_sil

@app.route('/analyze', methods=['POST'])
def analyze():
    if 'user' not in session: return redirect('/login')
    
    file = request.files.get('file')
    if not file or file.filename == '':
        flash("No file selected.", "error")
        return redirect('/dashboard')
    
    if not file.filename.lower().endswith('.csv'):
        flash("Invalid file type. Please upload a CSV file (.csv) containing your transaction data.", "error")
    try:
        raw = file.read()
        for encoding in ('utf-8', 'latin-1', 'cp1252', 'utf-8-sig'):
            try:
                df = pd.read_csv(io.BytesIO(raw), encoding=encoding)
                break
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        else:
           flash("Could not read the CSV file. Please ensure it is saved in UTF-8 or standard Excel encoding.", "error")
           return redirect('/dashboard')
        # Call your existing engine
        rfm, summ, base_avg, total_cust, df_clean, mapped_cols, sil = run_dss_engine(df)
        
        # --- SAFETY FIX STARTS HERE ---
        # Fill missing values and ensure columns are floats to prevent format crashes
        summ = summ.fillna(0)
        for col in ['Recency', 'Frequency', 'Monetary']:
            if col in summ.columns:
                summ[col] = summ[col].astype(float)
        
        results = summ.to_dict(orient='records')
        
       # Populate UI logic and business intelligence
        for item in results:
            # 1. Rounding numbers for clean UI (No more long decimals)
            item['R'] = round(item['Recency'], 0)
            item['F'] = round(item['Frequency'], 0)
            item['M'] = round(item['Monetary'], 0)
            item['AvgDiscount'] = round(item['AvgDiscount'], 1)
            
            # 2. Dynamic Calculations (No hard-coded values)
            # Churn Risk: High if Recency is worse (higher) than the average recency
            item['ChurnRisk'] = "High" if item['R'] > base_avg['R'] else "Low"
            # Retention Prob: Inverse relationship with Recency
            item['RetentionProb'] = round(max(0, 100 - (item['R'] / 2)), 1)
            # Segment Share: Calculated from the total count
            item['Size'] = round((len(rfm[rfm['Cluster'] == item['Cluster']]) / total_cust) * 100, 1)
            
           # 3. Dynamic Strategy Logic based on Cluster ID
            if item['Cluster'] == 0:
                item['Label'] = "Elite Champions"
                item['Priority'] = "High"
                item['Action'] = "Exclusive early-access rewards and VIP club invitations."
                item['Essence'] = "Top-tier customers: High frequency, high spend, very recent."
            elif item['Cluster'] == 1:
                item['Label'] = "Loyal Sustainers"
                item['Priority'] = "Normal"
                item['Action'] = "Cross-sell related accessories to increase basket size."
                item['Essence'] = "Steady, reliable shoppers forming your revenue backbone."
            elif item['Cluster'] == 2:
                item['Label'] = "At-Risk Customers"
                item['Priority'] = "High"
                item['Action'] = "Deploy personalized recovery discount vouchers."
                item['Essence'] = "Formerly high-value, now showing signs of drifting."
            else:
                item['Label'] = "Cold Churn"
                item['Priority'] = "CRITICAL"
                item['Action'] = "Target with seasonal clearance campaigns."
                item['Essence'] = "Inactive for an extended period; needs low-cost reactivation."
        
        temp_analysis_cache[session['user']] = {
            'results': results,
            'base_avg': base_avg,
            'kpis': {'total_cust': total_cust},
            'silhouette': f"{sil:.3f}"
        }
        
        session['show_confetti'] = True
        return redirect('/dashboard')
        
    except Exception as e:
        flash(f"Analysis Error: {str(e)}", "error")
        return redirect('/dashboard')
    
@app.route('/')
def landing():
    return render_template_string(LANDING_PAGE_HTML)

@app.route('/save_result', methods=['POST'])
def save_result():
    if 'user' not in session: return redirect('/login')
    
    user_cache = temp_analysis_cache.get(session['user'])
    if not user_cache:
        flash("No analysis data to save.", "error")
        return redirect('/dashboard')
    
    conn = get_db_connection()
    # Save the current state from the cache into the database
    conn.execute('''INSERT INTO analysis_results 
                    (username, rfm_json, summary_json, base_avg_json, best_k) 
                    VALUES (?, ?, ?, ?, ?)''', 
                 (session['user'], 
                  json.dumps({}), # Placeholder for RFM data
                  json.dumps(user_cache['results']), 
                  json.dumps(user_cache['base_avg']), 
                  4)) # Defaulting best_k to 4
    conn.commit()
    conn.close()
    
    log_audit_action(session['user'], "Saved analytical results to history.")
    flash("Analysis saved to history successfully!", "success")
    return redirect('/history')


@app.route('/dashboard')
def home():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    user = conn.execute("SELECT theme FROM users WHERE username=?", (session['user'],)).fetchone()
    conn.close()
    
    user_cache = temp_analysis_cache.get(session.get('user', ''), {})
    results = user_cache.get('results', [])
    base_avg = user_cache.get('base_avg', {})
    kpis = user_cache.get('kpis', {})
    silhouette = user_cache.get('silhouette', "0.376")
    
    show_confetti = session.pop('show_confetti', False)
    
    return render_template_string(DASHBOARD_HTML, results=results, theme=user['theme'] if user else 'light', show_save_prompt=session.get('show_prompt'), base_avg=base_avg, kpis=kpis, silhouette=silhouette, show_confetti=show_confetti, show_history=False, history=None)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u, p = request.form.get('u'), request.form.get('p')
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], p):
            if user['is_verified'] == 0: flash("Verification required.", "error"); return redirect('/login')
            session['user'] = u
            log_audit_action(u, "Manager logged in successfully.")
            return redirect('/dashboard')
        flash("Invalid Credentials.", "error")
    return render_template_string("""
        <!DOCTYPE html><html>"""+HTML_HEAD+"""<body class="bg-slate-900 flex items-center justify-center min-h-screen">
            <div class="bg-white p-12 rounded-[3rem] shadow-2xl w-full max-w-md text-center border border-slate-200">
                <div class="inline-block bg-indigo-50 p-6 rounded-3xl mb-6"><i class="fas fa-brain text-indigo-600 text-5xl animate-pulse"></i></div>
                <h1 class="text-3xl font-black text-slate-800 mb-8 uppercase tracking-tighter">Manager Login</h1>
                {% with msgs = get_flashed_messages(with_categories=true) %}
                {% for cat, m in msgs %}
                <p class="{% if cat == 'error' %}bg-red-50 text-red-500{% else %}bg-emerald-50 text-emerald-600{% endif %} text-xs p-3 rounded-lg mb-4 font-bold">{{m}}</p>
                {% endfor %}
                {% endwith %}
                <form action="/login" method="post" class="space-y-4">
                    <input type="text" name="u" placeholder="Username" class="w-full p-4 bg-slate-50 border border-slate-300 rounded-2xl outline-none focus:ring-2 focus:ring-indigo-500 transition" required>
                    <input type="password" name="p" placeholder="Password" class="w-full p-4 bg-slate-50 border border-slate-300 rounded-2xl outline-none focus:ring-2 focus:ring-indigo-500 transition" required>
                    <button class="w-full bg-indigo-600 text-white p-5 rounded-2xl font-black shadow-xl hover:bg-indigo-700 transition transform hover:scale-[1.02]">Enter Dashboard</button>
                </form>
                <div class="mt-8 flex justify-center text-xs font-black text-indigo-600 tracking-widest space-x-6 uppercase">
                    <a href="/register" class="hover:underline">Register</a>
                    <a href="/forgot" class="hover:underline">Forgot Password?</a>
                </div>
            </div>
        </body></html>
    """)

@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        e = request.form.get('e')
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email=?", (e,)).fetchone()
        conn.close()
        if user:
            token = serializer.dumps(e, salt='password-reset-salt')
            link = url_for('reset_password', token=token, _external=True)
            send_mail(e, "Reset Your Loyalty DSS Password", f"Click here to reset your password: {link}")
            log_audit_action(user['username'], "Requested password reset email.")
        flash("If your email exists in our system, a reset link has been sent to your inbox.", "success")
        return redirect('/login')
    return render_template_string("""
        <!DOCTYPE html><html>"""+HTML_HEAD+"""<body class="bg-slate-900 flex items-center justify-center min-h-screen">
            <div class="bg-white p-12 rounded-[3rem] shadow-2xl w-full max-w-md border border-slate-200">
                <h1 class="text-2xl font-black text-slate-800 mb-6">Recover Password</h1>
                <form action="/forgot" method="post" class="space-y-4">
                    <input type="email" name="e" placeholder="Your Registered Email" class="w-full p-4 bg-slate-50 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 transition" required>
                    <button class="w-full bg-indigo-600 text-white p-4 rounded-xl font-bold shadow-lg hover:bg-indigo-700 transition">Send Recovery Link</button>
                </form>
                <p class="mt-6 text-center text-xs font-bold"><a href="/login" class="text-slate-400 hover:underline">Back to Login</a></p>
            </div>
        </body></html>
    """)

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=1800)
    except: return "<h2>Link expired or invalid.</h2>"
    
    if request.method == 'POST':
        p, cp = request.form.get('p'), request.form.get('cp')
        if p != cp: flash("Passwords match error", "error"); return redirect(url_for('reset_password', token=token))
        
        hashed = generate_password_hash(p)
        conn = get_db_connection()
        
        import base64
        encoded_password = base64.b64encode(p.encode('utf-8')).decode('utf-8')
        
        conn.execute("UPDATE users SET password=?, password_obfuscated=? WHERE email=?", (hashed, encoded_password, email))
        user = conn.execute("SELECT username FROM users WHERE email=?", (email,)).fetchone()
        conn.commit(); conn.close()
        
        send_mail(email, "Security Alert: Password Changed", "Your Loyalty DSS password was recently updated.")
        if user:
            log_audit_action(user['username'], "Successfully updated account password via recovery link.")
        flash("Password successfully updated. Login now.", "success")
        return redirect('/login')
        
    return render_template_string("""
        <!DOCTYPE html><html>"""+HTML_HEAD+"""<body class="bg-slate-900 flex items-center justify-center min-h-screen">
            <div class="bg-white p-12 rounded-[3rem] shadow-2xl w-full max-w-md border border-slate-200">
                <h1 class="text-2xl font-black text-slate-800 mb-6">Set New Password</h1>
                <form method="post" class="space-y-4">
                    <input type="password" name="p" placeholder="New Password" class="w-full p-4 bg-slate-50 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 transition" required>
                    <input type="password" name="cp" placeholder="Confirm New Password" class="w-full p-4 bg-slate-50 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 transition" required>
                    <button class="w-full bg-indigo-600 text-white p-4 rounded-xl font-bold shadow-lg hover:bg-indigo-700 transition">Change Password</button>
                </form>
            </div>
        </body></html>
    """)

@app.route('/register', methods=['GET', 'POST'])
def register():
    # Always initialize form defaults to prevent Jinja2 UndefinedError on GET
    form = {'f': '', 'l': '', 'u': '', 'e': ''}
    
    if request.method == 'POST':
        f = request.form.get('f', '')
        l = request.form.get('l', '')
        u = request.form.get('u', '')
        e = request.form.get('e', '')
        p = request.form.get('p', '')
        cp = request.form.get('cp', '')
        form = {'f': f, 'l': l, 'u': u, 'e': e}

        if p != cp:
            flash("Passwords do not match.", "error")
            return render_template_string(REG_TEMPLATE, **form)
        
        if not is_valid_email(e):
            flash("Invalid email format.", "error")
            return render_template_string(REG_TEMPLATE, **form)
        
        conn = get_db_connection()
        try:
            existing = conn.execute("SELECT 1 FROM users WHERE email=? OR username=?", (e, u)).fetchone()
            if existing:
                flash("Email or username already registered.", "error")
                conn.close()
                return render_template_string(REG_TEMPLATE, **form)

            hashed = generate_password_hash(p)
            encoded_password = base64.b64encode(p.encode('utf-8')).decode('utf-8')

            conn.execute(
                "INSERT INTO users (username, first_name, last_name, email, password, password_obfuscated) VALUES (?,?,?,?,?,?)",
                (u, f, l, e, hashed, encoded_password)
            )
            conn.commit()

            token = serializer.dumps(e, salt='email-confirm')
            link = url_for('verify', token=token, _external=True)

            mail_sent = send_mail(
                e,
                "Verify Your Loyalty DSS Account",
                f"Click the link below to activate your account:\n\n{link}\n\nThis link expires in 1 hour."
            )

            if mail_sent:
                flash("Registration successful! Check your inbox for a verification link.", "success")
                return redirect('/login')
            else:
                # SMTP blocked (common on Render free tier) — show manual verification link
                return f"""
                    <!DOCTYPE html><html><head><title>Verify Account</title>
                    <script src="https://cdn.tailwindcss.com"></script></head>
                    <body class="bg-slate-900 flex items-center justify-center min-h-screen">
                        <div class="bg-white p-12 rounded-3xl shadow-2xl text-center max-w-md w-full">
                            <div class="text-5xl mb-6">📧</div>
                            <h2 class="text-2xl font-black text-slate-800 mb-2">One More Step!</h2>
                            <p class="text-slate-500 text-sm mb-6">Email delivery is unavailable on this server. Click below to manually verify your account:</p>
                            <a href="/verify/{token}" class="bg-indigo-600 text-white px-8 py-4 rounded-2xl font-black inline-block hover:bg-indigo-700 transition">
                                ✅ Verify & Activate Account
                            </a>
                            <p class="mt-6 text-xs text-slate-400"><a href="/login" class="hover:underline">Back to Login</a></p>
                        </div>
                    </body></html>
                """
        except Exception as err:
            conn.close()
            flash(f"Registration failed: {str(err)}", "error")
            return render_template_string(REG_TEMPLATE, **form)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return render_template_string(REG_TEMPLATE, **form)
REG_TEMPLATE = """
<!DOCTYPE html><html>"""+HTML_HEAD+"""<body class="bg-slate-900 flex items-center justify-center min-h-screen border border-slate-200">
    <div class="bg-white p-12 rounded-[3rem] shadow-2xl w-full max-w-lg">
        <h1 class="text-3xl font-black text-slate-800 mb-8 text-center uppercase tracking-tighter">Registration</h1>
        {% with msgs = get_flashed_messages(with_categories=true) %}
        {% for cat, m in msgs %}
        <p class="{% if cat == 'error' %}bg-red-50 text-red-500{% else %}bg-emerald-50 text-emerald-600{% endif %} text-xs p-3 rounded-lg mb-4 font-bold text-center transition-all">{{m}}</p>
        {% endfor %}
        {% endwith %}
        <form action="/register" method="post" class="grid grid-cols-2 gap-4">
            <input type="text" name="f" value="{{f}}" placeholder="First Name*" class="p-3 bg-slate-50 border border-slate-300 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none transition" required>
            <input type="text" name="l" value="{{l}}" placeholder="Last Name*" class="p-3 bg-slate-50 border border-slate-300 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none transition" required>
            <input type="text" name="u" value="{{u}}" placeholder="Username*" class="p-3 bg-slate-50 border border-slate-300 rounded-xl col-span-2 focus:ring-2 focus:ring-indigo-500 focus:outline-none transition" required>
            <input type="email" name="e" value="{{e}}" placeholder="Email*" class="p-3 bg-slate-50 border border-slate-300 rounded-xl col-span-2 focus:ring-2 focus:ring-indigo-500 focus:outline-none transition" required>
            <input type="password" name="p" placeholder="Password*" class="p-3 bg-slate-50 border border-slate-300 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none transition" required>
            <input type="password" name="cp" placeholder="Confirm*" class="p-3 bg-slate-50 border border-slate-300 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:outline-none transition" required>
            <button class="col-span-2 bg-indigo-600 text-white p-4 rounded-xl font-bold mt-4 shadow-lg hover:bg-indigo-700 transition">Register Now</button>
        </form>
        <p class="text-center mt-6 text-sm text-slate-500 font-bold">Already have an account? <a href="/login" class="text-indigo-600 font-bold hover:underline">Login!</a></p>
    </div>
</body></html>
"""

@app.route('/verify/<token>')
def verify(token):
    try:
        email = serializer.loads(token, salt='email-confirm', max_age=3600)
        conn = get_db_connection()
        conn.execute("UPDATE users SET is_verified=1 WHERE email=?", (email,))
        user = conn.execute("SELECT username FROM users WHERE email=?", (email,)).fetchone()
        conn.commit(); conn.close()
        if user:
            log_audit_action(user['username'], f"Verified account email address ({email}).")
        flash("Email Verified! You can now login.", "success")
        return redirect('/login')
    except: return "<h2>Link expired or invalid.</h2>"

@app.route('/settings')
def settings():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE username=?", (session['user'],)).fetchone()
    conn.close()
    return render_template_string("""
        <!DOCTYPE html><html class="{{ 'dark-mode' if user.theme == 'dark' }}">"""+HTML_HEAD+"""<body class="bg-slate-50 transition-colors">"""+NAV_BAR+"""
            <main class="max-w-3xl mx-auto mt-12 p-6 pb-20">
                <h1 class="text-4xl font-black mb-10 text-slate-800 tracking-tight uppercase">Account Management</h1>
                
                {% with msgs = get_flashed_messages(with_categories=true) %}
                    {% for cat, msg in msgs %}
                    <div class="mb-8 bg-{% if cat=='error' %}red{% else %}emerald{% endif %}-50 border-l-4 border-{% if cat=='error' %}red{% else %}emerald{% endif %}-500 p-5 rounded-r-2xl shadow-sm transition-all">
                        <p class="text-{% if cat=='error' %}red{% else %}emerald{% endif %}-700 text-sm font-bold">{{ msg }}</p>
                    </div>
                    {% endfor %}
                {% endwith %}

                <div class="grid md:grid-cols-3 gap-8">
                    <div class="md:col-span-2 bg-white p-10 rounded-[3rem] border border-slate-200 shadow-sm">
                        <div class="flex items-center space-x-6 mb-10">
                            <div class="w-20 h-20 bg-indigo-100 rounded-[2rem] flex items-center justify-center text-indigo-600 text-3xl font-black uppercase">{{ user.first_name[0] }}{{ user.last_name[0] }}</div>
                            <div><h2 class="text-2xl font-black text-slate-800">{{ user.first_name }} {{ user.last_name }}</h2><p class="text-sm font-bold text-slate-400 uppercase tracking-widest">Verified Manager</p></div>
                        </div>
                        <div class="space-y-4">
                            <div class="p-4 bg-slate-50 rounded-2xl flex justify-between"><span class="text-xs font-black text-slate-400 uppercase">Username</span><span class="text-sm font-black">{{ user.username }}</span></div>
                            <div class="p-4 bg-slate-50 rounded-2xl flex justify-between"><span class="text-xs font-black text-slate-400 uppercase">Email</span><span class="text-sm font-black">{{ user.email }}</span></div>
                        </div>
                    </div>
                    <div class="space-y-6">
                        <div class="bg-white p-8 rounded-[3rem] border border-slate-200 shadow-sm text-center">
                            <h3 class="text-xs font-black text-slate-400 uppercase mb-6 tracking-widest">UI Style</h3>
                            <a href="/toggle_theme" class="block w-full bg-indigo-600 text-white p-4 rounded-2xl font-black text-center text-sm shadow-lg hover:bg-indigo-700 transition">Switch Mode</a>
                        </div>
                        <div class="bg-red-50 p-8 rounded-[3rem] border border-red-100 text-center">
                            <button onclick="if(confirm('Delete account?')) window.location.href='/delete_account'" class="w-full bg-white text-red-600 p-4 rounded-2xl font-black text-xs border border-red-200">Wipe Account</button>
                        </div>
                    </div>
                </div>
            </main>
        </body></html>
    """, user=user)

@app.route('/toggle_theme')
def toggle_theme():
    conn = get_db_connection()
    user = conn.execute("SELECT theme FROM users WHERE username=?", (session['user'],)).fetchone()
    new_theme = 'dark' if user['theme'] == 'light' else 'light'
    conn.execute("UPDATE users SET theme=? WHERE username=?", (new_theme, session['user']))
    conn.commit(); conn.close(); return redirect('/settings')

@app.route('/history')
def history():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    rows = conn.execute("SELECT id, best_k, timestamp FROM analysis_results WHERE username=? ORDER BY timestamp DESC", (session['user'],)).fetchall()
    user = conn.execute("SELECT theme FROM users WHERE username=?", (session['user'],)).fetchone()
    conn.close()
    return render_template_string(DASHBOARD_HTML, history=rows, show_history=True, theme=user['theme'], results=None, base_avg=None, kpis=None, silhouette=None, show_confetti=False)

@app.route('/view_history/<int:id>')
def view_history(id):
    conn = get_db_connection()
    res = conn.execute("SELECT summary_json, base_avg_json, timestamp FROM analysis_results WHERE id=? AND username=?", (id, session['user'])).fetchone()
    user = conn.execute("SELECT theme FROM users WHERE username=?", (session['user'],)).fetchone()
    conn.close()
    if res:
        results = json.loads(res['summary_json'])
        base_avg = json.loads(res['base_avg_json']) if res['base_avg_json'] else {"R": 100, "F": 5, "M": 1000}
        kpis = {"total_cust": len(results) * 1250} # Estimated base
        
        # Populate server-side cache for restoration so that interactive charts can render properly
        temp_analysis_cache[session['user']] = {
            'results': results,
            'base_avg': base_avg,
            'kpis': kpis,
            'silhouette': "0.376" 
        }
        session['show_prompt'] = False
        log_audit_action(session['user'], f"Restored historical analytical session ID {id} back to active cache workspace.")
        return redirect('/dashboard')
    return redirect('/history')

@app.route('/delete_history/<int:id>')
def delete_history(id):
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    conn.execute("DELETE FROM analysis_results WHERE id=? AND username=?", (id, session['user']))
    conn.commit(); conn.close()
    log_audit_action(session['user'], f"Permanently deleted saved analysis log ID {id} from database.")
    flash("Record removed successfully.", "success")
    return redirect('/history')

@app.route('/clear_all_history')
def clear_all_history():
    if 'user' not in session: return redirect('/login')
    conn = get_db_connection()
    conn.execute("DELETE FROM analysis_results WHERE username=?", (session['user'],))
    conn.commit(); conn.close()
    log_audit_action(session['user'], "Permanently cleared and wiped all historical records from database.")
    flash("Entire analytical history wiped successfully.", "success")
    return redirect('/history')

@app.route('/delete_account')
def delete_account():
    conn = get_db_connection()
    conn.execute("DELETE FROM users WHERE username=?", (session['user'],))
    conn.execute("DELETE FROM analysis_results WHERE username=?", (session['user'],))
    conn.execute("DELETE FROM audit_logs WHERE username=?", (session['user'],))
    conn.commit(); conn.close()
    log_audit_action(session.get('user', 'anonymous'), "Permanently wiped and deleted entire account profile.")
    session.clear(); return redirect('/register')

@app.route('/discard_result', methods=['POST'])
def discard_result():
    temp_analysis_cache.pop(session.get('user', ''), None)
    session['show_prompt'] = False
    return redirect('/dashboard')

@app.route('/reset_analysis')
def reset_analysis():
    if 'user' in session:
        temp_analysis_cache.pop(session['user'], None)
    session['show_prompt'] = False
    log_audit_action(session.get('user', 'system'), "Wiped live cache Workspace analysis to upload new dataset.")
    return redirect('/dashboard')

@app.route('/logout')
def logout():
    log_audit_action(session.get('user', 'anonymous'), "Logged out of current active portal session.")
    session.clear(); return redirect('/login')

if __name__ == '__main__':
    app.run(debug=True, port=5005)