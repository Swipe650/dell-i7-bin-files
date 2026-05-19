#!/usr/bin/env python3
"""
Secure credential storage for Last.fm using keyring library.
Stores credentials in KWallet (Linux) or system keyring.
"""

import keyring
import getpass
import sys

# Service name for keyring
SERVICE_NAME = "LastFM"

def store_credentials():
    """Store Last.fm credentials in system keyring."""
    print("🔐 Last.fm Credential Storage")
    print("=" * 40)
    print(f"Service name: {SERVICE_NAME}")
    print()
    
    # Get username
    username = input("Enter your Last.fm username: ").strip()
    if not username:
        print("❌ Username cannot be empty")
        return False
    
    # Get API key
    api_key = input("Enter your Last.fm API key: ").strip()
    if not api_key:
        print("❌ API key cannot be empty")
        return False
    
    # Get API secret
    api_secret = getpass.getpass("Enter your Last.fm API secret: ").strip()
    if not api_secret:
        print("❌ API secret cannot be empty")
        return False
    
    # Get password
    password = getpass.getpass("Enter your Last.fm password: ").strip()
    if not password:
        print("❌ Password cannot be empty")
        return False
    
    print("\n💾 Storing credentials...")
    
    try:
        # Store each credential separately in keyring
        keyring.set_password(SERVICE_NAME, "username", username)
        keyring.set_password(SERVICE_NAME, "api_key", api_key)
        keyring.set_password(SERVICE_NAME, "api_secret", api_secret)
        keyring.set_password(SERVICE_NAME, "password", password)
        
        print("✅ All credentials stored successfully!")
        print(f"   Service: {SERVICE_NAME}")
        print(f"   Username: {username}")
        print("   API key, API secret, and password stored")
        return True
        
    except Exception as e:
        print(f"❌ Error storing credentials: {e}")
        return False

def retrieve_credentials():
    """Retrieve Last.fm credentials from system keyring."""
    print("🔐 Last.fm Credential Retrieval")
    print("=" * 40)
    
    try:
        username = keyring.get_password(SERVICE_NAME, "username")
        api_key = keyring.get_password(SERVICE_NAME, "api_key")
        api_secret = keyring.get_password(SERVICE_NAME, "api_secret")
        password = keyring.get_password(SERVICE_NAME, "password")
        
        if all([username, api_key, api_secret, password]):
            print("\n✅ Credentials found:")
            print(f"   Username: {username}")
            print(f"   API key: {api_key[:8]}...{api_key[-4:] if len(api_key) > 12 else ''}")
            print(f"   API secret: {api_secret[:8]}...{api_secret[-4:] if len(api_secret) > 12 else ''}")
            print(f"   Password: {'*' * len(password)}")
            return {
                "username": username,
                "api_key": api_key,
                "api_secret": api_secret,
                "password": password
            }
        else:
            print("\n❌ No credentials found for Last.fm")
            return None
            
    except Exception as e:
        print(f"❌ Error retrieving credentials: {e}")
        return None

def delete_credentials():
    """Delete Last.fm credentials from system keyring."""
    print("🔐 Last.fm Credential Deletion")
    print("=" * 40)
    
    confirm = input("⚠️  Are you sure you want to delete all Last.fm credentials? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("❌ Deletion cancelled")
        return False
    
    try:
        for key in ["username", "api_key", "api_secret", "password"]:
            keyring.delete_password(SERVICE_NAME, key)
        
        print("✅ All Last.fm credentials deleted successfully")
        return True
        
    except Exception as e:
        print(f"❌ Error deleting credentials: {e}")
        return False

def check_keyring_backend():
    """Display which keyring backend is being used."""
    print("🔐 Keyring Backend Information")
    print("=" * 40)
    print(f"Current backend: {keyring.get_keyring()}")
    print(f"Priority: {keyring.get_keyring().priority if hasattr(keyring.get_keyring(), 'priority') else 'N/A'}")
    
    # Check if we're using KWallet
    backend_str = str(keyring.get_keyring())
    if "kwallet" in backend_str.lower() or "KWallet" in backend_str:
        print("✅ Using KWallet backend")
    elif "SecretService" in backend_str or "libsecret" in backend_str:
        print("✅ Using GNOME/SecretService backend (may work with KWallet)")
    else:
        print(f"⚠️  Using: {backend_str}")
        print("   For KWallet, ensure python3-keyring and python3-keyrings.alt are installed")

def main():
    """Main menu for credential management."""
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "store":
            store_credentials()
        elif command == "retrieve":
            retrieve_credentials()
        elif command == "delete":
            delete_credentials()
        elif command == "check":
            check_keyring_backend()
        else:
            print(f"Unknown command: {command}")
            print("Usage: python3 lastfm_creds.py [store|retrieve|delete|check]")
        return
    
    # Interactive menu
    while True:
        print("\n" + "=" * 50)
        print("🔐 Last.fm Credential Manager")
        print("=" * 50)
        print("1. Store credentials")
        print("2. Retrieve credentials")
        print("3. Delete credentials")
        print("4. Check keyring backend")
        print("5. Exit")
        print("=" * 50)
        
        choice = input("Choose an option (1-5): ").strip()
        
        if choice == "1":
            store_credentials()
        elif choice == "2":
            retrieve_credentials()
        elif choice == "3":
            delete_credentials()
        elif choice == "4":
            check_keyring_backend()
        elif choice == "5":
            print("👋 Goodbye!")
            break
        else:
            print("❌ Invalid choice. Please try again.")
        
        input("\nPress Enter to continue...")

if __name__ == "__main__":
    main()