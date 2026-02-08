# ===================== IMPORTS =====================
import os
import re
import json
import base64
import logging
from datetime import datetime

import cv2
import numpy as np
import pytesseract
import pytz

from PIL import Image
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import gspread
from google.oauth2.service_account import Credentials
from groq import Groq

# ===================== CONFIG =====================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
MODEL_NAME = "llama-3.1-8b-instant"

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

logging.basicConfig(level=logging.INFO)
print("✅ Visiting Card Bot starting...")

# ===================== GOOGLE CREDENTIALS =====================
if not os.path.exists("credentials.json"):
    encoded = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not encoded:
        raise RuntimeError("GOOGLE_CREDENTIALS_BASE64 missing")

    with open("credentials.json", "wb") as f:
        f.write(base64.b64decode(encoded))

# ===================== GOOGLE SHEETS =====================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
sheet_client = gspread.authorize(creds)
sheet = sheet_client.open_by_key(SHEET_ID).sheet1

if not sheet.get_all_values():
    sheet.append_row([
        "Timestamp (IST)",
        "Telegram_ID",
        "Name",
        "Designation",
        "Company",
        "Phone",
        "Email",
        "Website",
        "Address",
        "Industry",
        "Services"
    ])

# ===================== GROQ CLIENT =====================
groq_client = Groq(api_key=GROQ_API_KEY)

# ===================== HELPERS =====================
def safe(val):
    return val if val and str(val).strip() else "Not Found"

def clean_text(text):
    replacements = {
        "(at)": "@",
        "[at]": "@",
        " O ": " 0 ",
        " o ": " 0 ",
        "|": "1"
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text

def extract_phone(text):
    match = re.search(r'(\+?\d{1,3}[\s\-]?)?\d{9,10}', text)
    return match.group() if match else "Not Found"

def extract_email(text):
    match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    return match.group() if match else "Not Found"

def extract_website(text):
    match = re.search(r'(www\.[\w.-]+\.\w+|https?://\S+)', text)
    return match.group() if match else "Not Found"

def safe_json_load(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return None
        return None

def call_groq(prompt):
    response = groq_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    return response.choices[0].message.content

def save_to_sheet(chat_id, data):
    ist = pytz.timezone("Asia/Kolkata")
    timestamp = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

    sheet.append_row([
        timestamp,
        chat_id,
        data["name"],
        data["designation"],
        data["company"],
        data["phone"],
        data["email"],
        data["website"],
        data["address"],
        data["industry"],
        ", ".join(data["services"])
    ])

# ===================== COMMANDS =====================
async def start(update: Update, context):
    await update.message.reply_text(
        "✅ Visiting Card Bot Active\n\n"
        "📸 Send a visiting card image\n"
        "📊 Data will be saved\n"
        "💬 Ask follow-up questions"
    )

# ===================== IMAGE HANDLER =====================
async def handle_image(update: Update, context):
    await update.message.reply_text("📸 Image received. Processing...")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    await file.download_to_drive("card.jpg")

    img = cv2.imread("card.jpg")
    img = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 11, 17, 17)
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    ocr_text = pytesseract.image_to_string(thresh, config="--oem 3 --psm 6")
    cleaned = clean_text(ocr_text)

    phone = extract_phone(cleaned)
    email = extract_email(cleaned)
    website = extract_website(cleaned)

    prompt = f"""
Extract visiting card details.

Return ONLY valid JSON.

{{
  "name": "",
  "designation": "",
  "company": "",
  "address": "",
  "industry": "",
  "services": []
}}

TEXT:
{ocr_text}
"""

    ai_response = call_groq(prompt)
    parsed = safe_json_load(ai_response)

    if not parsed:
        await update.message.reply_text("⚠️ Could not extract data. Try clearer image.")
        return

    data = {
        "name": safe(parsed.get("name")),
        "designation": safe(parsed.get("designation")),
        "company": safe(parsed.get("company")),
        "phone": safe(phone),
        "email": safe(email),
        "website": safe(website),
        "address": safe(parsed.get("address")),
        "industry": safe(parsed.get("industry")),
        "services": parsed.get("services") or ["Not Found"]
    }

    context.user_data["company"] = data["company"]
    context.user_data["website"] = data["website"]

    save_to_sheet(update.effective_chat.id, data)

    reply = f"""
📇 Visiting Card Details

Name: {data['name']}
Designation: {data['designation']}
Company: {data['company']}
Phone: {data['phone']}
Email: {data['email']}
Website: {data['website']}
Address: {data['address']}
Industry: {data['industry']}
Services:
- {'\n- '.join(data['services'])}
"""
    await update.message.reply_text(reply)

# ===================== FOLLOW-UP =====================
async def handle_text(update: Update, context):
    company = context.user_data.get("company")

    if not company or company == "Not Found":
        await update.message.reply_text("📸 Upload visiting card first.")
        return

    prompt = f"""
Company: {company}
Website: {context.user_data.get("website")}

Explain:
- What the company does
- Potential customers
- Potential vendors
Focus on India.
"""

    response = groq_client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )

    await update.message.reply_text(response.choices[0].message.content)

# ===================== RUN =====================
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.PHOTO, handle_image))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("🚀 Bot is LIVE")
app.run_polling()
