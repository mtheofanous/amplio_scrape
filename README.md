# amplio

Tracking & Ad Platform Scanner — expanded feature set.

Setup

1. Create virtual environment and install deps:

```powershell
python -m venv venv
.\venv\Scripts\pip.exe install --upgrade pip
.\venv\Scripts\pip.exe install -r requirements.txt
```

2. (Optional) Install Playwright browsers for headless checks:

```powershell
.\venv\Scripts\python.exe -m playwright install
```

3. Add API tokens (optional) in `secrets.toml` or env vars:

- `META_ACCESS_TOKEN` for Meta Ads Library API

Run

```powershell
.\venv\Scripts\python.exe -m streamlit run tracking_scanner_app.py
```

Notes

- The app contains optional features that require extra packages (Playwright, dnspython, python-whois). If those packages are missing the app will still run but will mark the features as not configured.
- Do NOT commit API keys. Use `st.secrets` or environment variables.

Playwright headless checks

- Open the app and enable "Headless checks (Playwright)" in the sidebar.
- Use the "Install Playwright browsers" button to install required browser binaries.
- Increasing "Playwright workers" allows more concurrent headless checks; the app uses a semaphore to avoid overloading the machine.
