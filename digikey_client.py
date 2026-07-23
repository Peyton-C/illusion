import os
import time
import threading
import urllib.parse
from pathlib import Path

import requests
import yaml

API_BASE = "https://api.digikey.com"
AUTH_URL = f"{API_BASE}/v1/oauth2/authorize"
TOKEN_URL = f"{API_BASE}/v1/oauth2/token"


class DigiKeyClient:
    def __init__(self, config_path="./config.yaml"):
        self.config_path = Path(config_path)
        self.lock = threading.Lock()

        with self.config_path.open("r") as f:
            config = yaml.safe_load(f)

        dk = config["illusion"]["digikey"]
        self.client_id = dk["client_id"]
        self.client_secret = dk["client_secret"]
        self.tokens = dk.get("tokens") or {}

    def _save(self, response):
        self.tokens = {
            "access_token": response["access_token"],
            "refresh_token": response["refresh_token"],
            "expires_at": time.time() + int(response["expires_in"]) - 60,
        }

        # Re-read so we don't clobber changes made elsewhere
        with self.config_path.open("r") as f:
            config = yaml.safe_load(f)

        config["illusion"]["digikey"]["tokens"] = self.tokens
        
        # Atomic write: temp file then rename, so a crash can't corrupt config
        tmp_path = self.config_path.with_suffix(".yaml.tmp")
        with tmp_path.open("w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, self.config_path)

    def bootstrap(self, auth_code, redirect_uri="https://localhost"):
        r = requests.post(TOKEN_URL, data={
            "code": auth_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        r.raise_for_status()
        self._save(r.json())

    def _get_access_token(self):
        with self.lock:
            if time.time() < self.tokens.get("expires_at", 0):
                return self.tokens["access_token"]

            r = requests.post(TOKEN_URL, data={
                "refresh_token": self.tokens["refresh_token"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            })
            r.raise_for_status()
            self._save(r.json())
            return self.tokens["access_token"]

    def _get(self, path):
        r = requests.get(f"{API_BASE}{path}", headers={
            "X-DIGIKEY-Client-Id": self.client_id,
            "Authorization": f"Bearer {self._get_access_token()}",
            "Accept": "application/json",
            "X-DIGIKEY-Locale-Site": "CA",
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": "CAD",
        })
        r.raise_for_status()
        return r.json()

    def lookup_barcode(self, barcode: str):
        if barcode.isdigit():
            path = f"/Barcoding/v3/ProductBarcodes/{urllib.parse.quote(barcode)}"
        else:
            barcode = barcode.replace("\x1d", "\u241d").replace("\x1e", "\u241e")
            path = f"/Barcoding/v3/Product2DBarcodes/{urllib.parse.quote(barcode, safe='')}"
        return self._get(path)
    
if __name__ == "__main__":
    import webbrowser

    with open("./config.yaml", "r") as file:
        config = yaml.safe_load(file)

    params = {
        "response_type": "code",
        "client_id": config["illusion"]["digikey"]["client_id"],
        "redirect_uri": "https://localhost",
    }

    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


    print("Opening DigiKey authorization page...")
    print(url)
    webbrowser.open(url)

    print()
    print("After logging in, DigiKey will redirect you to your redirect URI.")
    print("Copy the `code` value from the redirected URL.")
    print()
    code = input("Paste authorization code here: ").strip()

    dk = DigiKeyClient()
    dk.bootstrap(code)