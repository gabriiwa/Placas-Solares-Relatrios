import os
import requests
import google.generativeai as genai

# 1. Load Environment Variables
APP_ID = os.environ['APP_ID']
APP_SECRET = os.environ['APP_SECRET']
PLANT_ID = os.environ['PLANT_ID']
WEBHOOK_URL = os.environ['WEBHOOK_URL']
genai.configure(api_key=os.environ['GEMINI_API_KEY'])

BASE_URL = "https://api-domain-provided-by-auxsol.com" # Replace with actual URL

# 2. Get AUXSOL Token
auth_payload = {"app_id": APP_ID, "app_secret": APP_SECRET, "lang": "en-US"}
auth_res = requests.post(f"{BASE_URL}/auth/token", json=auth_payload).json()
token = auth_res['data']['access_token']

# 3. Get Plant Data
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json;charset=UTF-8"}
data_res = requests.get(f"{BASE_URL}/analysis/plantReport/queryPlantCurrentData/{PLANT_ID}", headers=headers).json()
plant_data = data_res['data']

# 4. Generate AI Analysis
model = genai.GenerativeModel('gemini-1.5-flash')
prompt = f"""
Analyze this solar plant data and write a short, professional morning summary for the engineering team.
Highlight the daily yield, monthly yield, and note if the status is online or in alarm.
Data: {plant_data}
"""
response = model.generate_content(prompt)

# 5. Send Notification (Example using a generic webhook like Slack/Discord)
requests.post(WEBHOOK_URL, json={"text": response.text})
print("Report sent successfully!")
