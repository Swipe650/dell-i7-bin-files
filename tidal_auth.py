#!/usr/bin/env python3
import tidalapi
import pickle
import os

# Create config directory in user's home if it doesn't exist
config_dir = os.path.expanduser("~/.config/tidal_scrobbler")
os.makedirs(config_dir, exist_ok=True)

session_file = os.path.join(config_dir, "session.pkl")

print("🔐 TIDAL API Authentication")
print("=" * 40)
print("This script will open a browser window for you to log into TIDAL.")
print("After successful login, the session will be saved to:")
print(f"   {session_file}")
print()

session = tidalapi.Session()
session.login_oauth_simple()   # Opens a URL – follow it in your browser

with open(session_file, 'wb') as f:
    pickle.dump(session, f)

print("\n✅ Authentication successful.")
print(f"📁 Session saved to: {session_file}")
