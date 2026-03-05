import urllib.request
import json
import os
from dotenv import load_dotenv

# 初始化設定
load_dotenv()
TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

# 定義 6 格座標 (寬度平分 833/833/834，高度平分 843/843)
rich_menu_data = {
    "size": {"width": 2500, "height": 1686},
    "selected": False,
    "name": "管理者選單-六格2",
    "chatBarText": "管理員功能",
    "areas": [
        # 第一排(使用者)
        {"bounds": {"x": 0, "y": 0, "width": 833, "height": 843}, "action": {"type": "message", "text": "今日午餐"}},
        {"bounds": {"x": 833, "y": 0, "width": 833, "height": 843}, "action": {"type": "message", "text": "今日晚餐"}},
        {"bounds": {"x": 1666, "y": 0, "width": 834, "height": 843}, "action": {"type": "message", "text": "修改訂單"}},
        # 第二排(管理者)
        {"bounds": {"x": 0, "y": 843, "width": 833, "height": 843}, "action": {"type": "message", "text": "數據報表"}}, # 老闆結單改為數據報表
        {"bounds": {"x": 833, "y": 843, "width": 833, "height": 843}, "action": {"type": "uri", "uri": "https://docs.google.com/spreadsheets/d/1Qpv6V4Mb856iy87mTWGSkzevCjpdsrn1NmjYceoi9Ns/edit?usp=sharing"}}, # 全部明細改為 Google Sheet 連結
        {"bounds": {"x": 1666, "y": 843, "width": 834, "height": 843}, "action": {"type": "uri", "uri": "https://liff.line.ee/2009191430-uamSkSal"}}
    ]
}

def create_admin_menu():
    url = "https://api.line.me/v2/bot/richmenu"
    req = urllib.request.Request(url, data=json.dumps(rich_menu_data).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(req) as f:
            res = json.loads(f.read().decode("utf-8"))
            menu_id = res['richMenuId']
            print(f"✅ 選單建立成功！")
            print(f"管理者 Rich Menu ID: {menu_id}")
            return menu_id
    except Exception as e:
        print(f"❌ 建立失敗: {e}")

admin_id = create_admin_menu()