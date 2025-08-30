import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")

base_url = "https://api.delta.exchange"

headers = {
    "api-key": API_KEY,
    "api-secret": API_SECRET
}

def get_balance():
    url = base_url + "/v2/wallet/balances"
    res = requests.get(url, headers=headers)
    return res.json()

print("Account Balance:", get_balance())
