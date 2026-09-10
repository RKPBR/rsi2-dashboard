"""
app.py — RSI(2) + 200EMA Strategy Dashboard (Streamlit web app)

DEPLOYMENT (Streamlit Community Cloud):
1. Create a free GitHub account (github.com) if you don't have one
2. Create a new repository, upload this file (app.py) and requirements.txt
3. Go to share.streamlit.io, sign in with GitHub, click "New app"
4. Point it at your repository / app.py — it deploys automatically
5. Your app gets a URL like: https://yourname-yourapp.streamlit.app

IMPORTANT — login/token is NOT saved permanently on the server:
- You still log in fresh each day (App ID + Secret + browser redirect step)
- Token is kept only in your browser session (st.session_state) — closing
  the tab means logging in again next time, same as the daily routine you
  already have with fyers_login2.py
- This keeps your credentials OFF the server entirely — nothing sensitive
  is stored in the code or repository
