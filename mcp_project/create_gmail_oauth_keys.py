# create_gmail_oauth_keys.py
import json
import os
from pathlib import Path
from dotenv import load_dotenv


def create_gmail_oauth_keys():
    """Create Gmail OAuth keys file in both project root and home directory."""

    # Load .env file
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ Loaded .env from: {env_path}")
    else:
        print(f"⚠️ .env file not found at: {env_path}")

    # Get credentials from environment
    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")

    print(f"GMAIL_CLIENT_ID: {'✓ Set' if client_id else '✗ Missing'}")
    print(f"GMAIL_CLIENT_SECRET: {'✓ Set' if client_secret else '✗ Missing'}")

    if not client_id or not client_secret:
        print("\n❌ GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env")
        print("   Please add them to your .env file and try again.")
        return False

    # Create the OAuth keys data
    keys_data = {
        "installed": {
            "client_id": client_id,
            "project_id": os.getenv("GMAIL_PROJECT_ID", "mcp-project"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": client_secret,
            "redirect_uris": [
                "http://localhost:8000/auth/gmail/callback",
                "http://localhost:8000/oauth/callback",
                "http://localhost",
            ],
        }
    }

    # 1. Create in project root (source location)
    project_root = Path.cwd()
    source_path = project_root / "gcp-oauth.keys.json"

    with open(source_path, "w") as f:
        json.dump(keys_data, f, indent=2)

    print(f"\n✅ Created OAuth keys file at: {source_path}")

    # 2. Create in home directory (destination location)
    home_dir = Path.home() / ".gmail-mcp"
    home_dir.mkdir(parents=True, exist_ok=True)
    dest_path = home_dir / "gcp-oauth.keys.json"

    with open(dest_path, "w") as f:
        json.dump(keys_data, f, indent=2)

    print(f"✅ Created OAuth keys file at: {dest_path}")

    # 3. Verify both files exist
    if source_path.exists() and dest_path.exists():
        print("\n🎉 OAuth keys files created successfully!")
        print(f"   Source: {source_path}")
        print(f"   Destination: {dest_path}")
        print("\n📝 Next steps:")
        print("   1. Run: npx -y @gongrzhe/server-gmail-autoauth-mcp")
        print("   2. Visit: http://localhost:8000/auth/gmail/login")
        return True
    else:
        print("\n❌ Failed to create OAuth keys files!")
        return False


if __name__ == "__main__":
    create_gmail_oauth_keys()
