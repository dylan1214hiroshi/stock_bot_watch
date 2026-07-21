import os
import requests
from bs4 import BeautifulSoup
from google import genai
from linebot import LineBotApi
from linebot.models import TextSendMessage
from pymongo import MongoClient
import twstock

# ---------------------------------------------------------
# 1. 環境變數與初始化
# ---------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

client = MongoClient(MONGO_URI)
db = client["stock_db"]
users_collection = db["users"]

# 初始化新版 Gemini Client
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    print("警告：未設定 GEMINI_API_KEY")

# 手動對應字典
MANUAL_MAP = {
    "7911": "阿波羅電力",
    "7856": "漢測"
}

# ---------------------------------------------------------
# 2. 爬蟲與 AI 摘要模組
# ---------------------------------------------------------
def fetch_stock_news(stock_id):
    """從白名單網站抓取新聞標題"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    news_titles = []

    try:
        yahoo_url = f"https://tw.stock.yahoo.com/quote/{stock_id}/news"
        res = requests.get(yahoo_url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        links = soup.find_all("a", href=True)
        for link in links:
            title = link.text.strip()
            if len(title) > 10 and (stock_id in title or "台" in title or "營收" in title):
                news_titles.append(f"[Yahoo] {title}")
    except Exception as e:
        print(f"Yahoo 爬蟲錯誤 ({stock_id}): {e}")

    try:
        anue_url = f"https://www.cnyes.com/search/news?keyword={stock_id}"
        res = requests.get(anue_url, headers=headers, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        titles = soup.select("a._1Zdp h3")
        for t in titles:
            title_text = t.text.strip()
            if len(title_text) > 5:
                news_titles.append(f"[鉅亨網] {title_text}")
    except Exception as e:
        print(f"鉅亨網爬蟲錯誤 ({stock_id}): {e}")

    return list(set(news_titles))

def generate_gemini_summary(stock_id, stock_name, news_titles):
    """呼叫 Gemini 進行整理與摘要"""
    if not news_titles or not gemini_client:
        return "近期無重大公開新聞或多空消息。"

    news_text = "\n".join(news_titles)
    
    prompt = f"""
    你是一位專業、客觀的台股分析師。以下是我從正規財經網站抓取關於【{stock_id} {stock_name}】的最新新聞標題：
    
    {news_text}
    
    你的任務：
    1. 嚴格過濾沒有根據的個人發言、討論區閒聊或無關雜訊。
    2. 將報導「相同事件」的新聞合併。
    3. 輸出「50字以內」的精簡多空重點摘要，直接破題，不要使用「根據新聞...」等開場白。
    4. 如果全部都是雜訊或舊聞，請「直接」回覆：「近期無重大公開新聞或多空消息。」
    """

    try:
        response = gemini_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        # 🚨 直接將真實的錯誤訊息回傳到 LINE 聊天室中
        return f"🚨 API 錯誤: {type(e).__name__} - {str(e)}"

# ---------------------------------------------------------
# 3. 主程式：產出報告並推播給使用者
# ---------------------------------------------------------
def send_morning_reports():
    print("開始產出台股晨報...")
    
    users = list(users_collection.find({"stocks": {"$exists": True, "$ne": []}}))
    if not users:
        print("沒有使用者需要發送晨報。")
        return

    stock_summary_cache = {}

    for user in users:
        user_id = user["_id"]
        stocks = user["stocks"]
        
        report_lines = ["☀️【早安！台股監測多空晨報】\n為你整理今日關注股票的最新動態：\n"]
        
        for stock_id in stocks:
            stock_name = ""
            if stock_id in twstock.codes:
                stock_name = twstock.codes[stock_id].name
            elif stock_id in MANUAL_MAP:
                stock_name = MANUAL_MAP[stock_id]
            else:
                stock_name = "其他股票"
            
            if stock_id not in stock_summary_cache:
                print(f"處理中: {stock_id} {stock_name}...")
                news_titles = fetch_stock_news(stock_id)
                summary = generate_gemini_summary(stock_id, stock_name, news_titles)
                stock_summary_cache[stock_id] = summary
            
            summary = stock_summary_cache[stock_id]
            report_lines.append(f"📊 標的：{stock_id} {stock_name}\n• {summary}\n")

        final_report = "\n".join(report_lines).strip()
        
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=final_report)
            )
            print(f"成功發送晨報給 {user_id}")
        except Exception as e:
            print(f"推播失敗 ({user_id}): {e}")

    print("晨報發送完畢！")
