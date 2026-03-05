import os
import json
import hmac
import hashlib
import base64
import urllib.request
import re
from datetime import datetime, timezone, timedelta

from firebase_functions import https_fn, options
from flask import Response
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================
# Google Sheets 讀取工具
# =========================
def _get_sheets_service():
    base_dir = os.path.dirname(__file__)
    sa_path = os.path.join(base_dir, "serviceAccount.json")
    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def _read_values(service, sheet_id: str, a1: str):
    try:
        return service.spreadsheets().values().get(spreadsheetId=sheet_id, range=a1).execute().get("values", [])
    except Exception as e:
        print(f"讀取 Sheet 失敗: {e}")
        return []

def _convert_drive_link(url: str) -> str:
    if not url: return ""
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w640"
    return url.strip()

# =========================
# LINE 回覆與 API 工具
# =========================
def _reply_text(reply_token: str, text: str):
    access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    _send_line_payload(payload, access_token)

def _send_line_payload(payload: dict, access_token: str):
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=json.dumps(payload).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        },
        method="POST"
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"回覆 LINE 失敗: {e}")

def _get_line_profile(user_id: str, access_token: str) -> str:
    if not user_id: return "未知使用者"
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode()).get("displayName", "未知使用者")
    except Exception as e:
        return "未知使用者"

def _get_user_info(service, sheet_id: str, user_id: str):
    user_rows = _read_values(service, sheet_id, "USERS!A:E")
    for r in reversed(user_rows):
        if len(r) >= 4 and r[1] == user_id:
            role = str(r[4]).strip() if len(r) >= 5 else ""
            return r[2], r[3], role
    return None, None, ""

# =========================
# Webhook 主程式
# =========================
@https_fn.on_request(region="asia-east1", memory=512, secrets=["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "SHEET_ID"])
def line_webhook(req: https_fn.Request) -> Response:
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
    channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    sheet_id = os.getenv("SHEET_ID", "")

    # 驗證 Signature
    body_str = req.get_data(as_text=True)
    signature = req.headers.get("X-Line-Signature", "")
    hash_val = hmac.new(channel_secret.encode('utf-8'), body_str.encode('utf-8'), hashlib.sha256).digest()
    if base64.b64encode(hash_val).decode('utf-8') != signature:
        return Response("Invalid signature", status=403)

    body_json = req.get_json(silent=True) or {}
    events = body_json.get("events", [])

    for event in events:
        user_id = event.get("source", {}).get("userId")
        event_type = event.get("type")

        # --- 自動切換選單邏輯 (僅針對 follow 或特定 admin 動作) ---
        # 建議：為了效能，可以只在 follow 時同步，或是管理者傳送訊息時同步
        if user_id and (event_type == "follow" or (event_type == "message" and event.get("message", {}).get("type") == "text")):
            try:
                service = _get_sheets_service()
                # 讀取 USERS 工作表 (B 欄 userId, E 欄角色)
                users_data = _read_values(service, sheet_id, "USERS!A:E")
                
                user_role = "USER"
                if users_data:
                    for row in users_data:
                        if len(row) >= 5 and row[1] == user_id:
                            user_role = row[4]
                            break
                
                # 你建立成功的管理者 ID
                ADMIN_MENU_ID = "richmenu-8622a5eb0c952142aa3750952dfbe272"
                
                if user_role == "ADMIN":
                    # 執行綁定管理者選單
                    _link_rich_menu(user_id, ADMIN_MENU_ID)
                else:
                    # 一般使用者解除個人綁定，回歸 Manager 後台設定的「預設選單」
                    _unlink_rich_menu(user_id)
            except Exception as e:
                print(f"Rich Menu Sync Error: {e}")

        # --- 原有的 handle_message 邏輯 ---
        if event_type == "message" and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()
            reply_token = event.get("replyToken")

            # 根據關鍵字分流處理
            if "餐 開團囉！" in text:
                meal_type = "LUNCH" if "午餐" in text else "DINNER"
                _send_order_flex_message(reply_token, channel_access_token, meal_type)

            elif text in ["今日午餐", "今日晚餐"]:
                _handle_show_menu(reply_token, text)

            elif text.startswith("點餐 "):
                _handle_order(reply_token, user_id, text, channel_access_token)

            elif text.startswith("綁定並點餐 "):
                _handle_bind_and_order(reply_token, user_id, text, channel_access_token)

            elif text.startswith("綁定單位 "):
                _handle_bind_unit(reply_token, user_id, text, channel_access_token)

            elif text.startswith("取消訂單 "):
                _handle_cancel_order(reply_token, user_id, text, channel_access_token)

            elif text == "修改訂單":
                _handle_modify_order(reply_token, user_id, text, channel_access_token)
            
            elif text == "數據報表":
                # 點擊圖文選單時，先彈出選擇按鈕
                _handle_reports_menu(reply_token, user_id, channel_access_token)

            elif text in ["老闆結單", "單位明細", "全部明細"]:
                # 當使用者點擊快速回覆的按鈕後，才執行原本的報表邏輯
                _handle_reports(reply_token, user_id, text, channel_access_token)

    return Response("OK", status=200)

# =========================
# 報表指令 (排除已取消訂單)
# =========================
def _handle_reports(reply_token: str, user_id: str, text: str, access_token: str):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()

    display_name, user_unit, user_role = _get_user_info(service, sheet_id, user_id)

    # 1. 權限防護網
    if text == "老闆結單" and user_role not in ["老闆", "超級管理員", "ADMIN"]:
        _reply_text(reply_token, "⛔ 您沒有老闆權限，無法查看廚房總單喔！")
        return
    if text == "全部明細" and user_role not in ["超級管理員", "ADMIN"]:
        _reply_text(reply_token, "⛔ 此為超級管理員專用指令，權限不足！")
        return
    if text == "單位明細" and not user_unit:
        _reply_text(reply_token, "⛔ 您尚未綁定單位，無法查看明細喔！請先完成綁定。")
        return

    # 2. 抓取今日「所有」有效的餐點 Session ID
    tw_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tw_tz).strftime("%Y-%m-%d")

    logs = _read_values(service, sheet_id, "logs!A:E")
    today_sessions = set()
    
    for r in reversed(logs):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if payload.get("date") == today_str:
                    sid = f"{payload.get('date')}_{payload.get('meal')}"
                    today_sessions.add(sid)
            except: continue

    if not today_sessions:
        _reply_text(reply_token, "📝 今天目前沒有發布任何菜單紀錄喔！")
        return

    # 3. 撈取訂單紀錄，並篩選出屬於今日 Session 的訂單
    # 👇 修改：這裡加入了狀態過濾邏輯
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    today_orders = []
    
    for o in orders:
        # 基本資料欄位不足就跳過
        if len(o) < 2: continue
        
        # 1. 檢查是否為今日場次
        if o[1] not in today_sessions: continue
        
        # 2. 檢查狀態 (J欄 = index 9)，若為 "已取消" 則跳過
        # 若資料長度不足 10，視為未取消 (可能是舊資料或未填寫)
        status = o[9] if len(o) >= 10 else ""
        if status == "已取消":
            continue
            
        today_orders.append(o)

    if not today_orders:
        _reply_text(reply_token, "📝 今天還沒有人點餐喔！(或訂單皆已取消)")
        return

    # 4. 將訂單依照 Session (餐別) 進行分組
    session_orders_map = {sid: [] for sid in today_sessions}
    for o in today_orders:
        sid = o[1]
        if sid in session_orders_map:
            session_orders_map[sid].append(o)

    # 5. 排序 Session：讓午餐 (LUNCH) 排在晚餐 (DINNER) 前面
    sorted_sessions = sorted(list(today_sessions), reverse=True) 

    final_reply = ""

    # 6. 迴圈產生每個餐別的報表
    for sid in sorted_sessions:
        current_orders = session_orders_map[sid]
        if not current_orders: continue 
        
        meal_name = "午餐" if "LUNCH" in sid else "晚餐"
        
        section_msg = ""
        section_total = 0

        # ==========================
        # 📝 報表 A：老闆結單 (合併版 - 午餐在上，晚餐在下)
        # ==========================
        if text == "老闆結單":
            lunch_counts = {}
            dinner_counts = {}
            unit_totals = {}
            grand_total = 0

            # 統計邏輯 (注意：today_orders 已經排除已取消的單了)
            for o in today_orders:
                sid_inner, unit, item, price = o[1], o[2], o[5], int(o[7])
                unit_totals[unit] = unit_totals.get(unit, 0) + price
                grand_total += price

                if "LUNCH" in sid_inner:
                    lunch_counts[item] = lunch_counts.get(item, 0) + 1
                elif "DINNER" in sid_inner:
                    dinner_counts[item] = dinner_counts.get(item, 0) + 1

            msg = f"🍱 【{today_str}】\n"
            msg += "-" * 15 + "\n"

            if lunch_counts:
                # msg += "☀️ [午餐]\n" # 可選：是否顯示標題
                for item, count in lunch_counts.items():
                    msg += f"🔸 {item}：{count} 份\n"
                
            if lunch_counts and dinner_counts:
                msg += "-" * 15 + "\n"

            if dinner_counts:
                # msg += "🌙 [晚餐]\n" # 可選：是否顯示標題
                for item, count in dinner_counts.items():
                    msg += f"🔸 {item}：{count} 份\n"

            msg += "-" * 15 + "\n"
            msg += f"💰 應收 (本日合計 ${grand_total})\n"
            for unit, total in unit_totals.items():
                msg += f"  🏢 {unit}：${total}\n"
            
            _reply_text(reply_token, msg.strip())
            return

        # ==========================
        # 報表 B：單位明細
        # ==========================
        elif text == "單位明細":
            unit_orders = [o for o in current_orders if o[2] == user_unit]
            if not unit_orders: continue

            user_totals = {}
            for o in unit_orders:
                name, item, price = o[4], o[5], int(o[7])
                if name not in user_totals:
                    user_totals[name] = {"items": [], "total": 0}
                user_totals[name]["items"].append(item)
                user_totals[name]["total"] += price
                section_total += price

            section_msg += f"\n🏢 【{today_str} {meal_name}】\n"
            section_msg += f"本餐總金額：${section_total}\n" + "-" * 15 + "\n"
            for name, data in user_totals.items():
                items_str = ", ".join(data["items"])
                section_msg += f"👤 {name}：{items_str} (${data['total']})\n"

        # ==========================
        # 報表 C：全部明細
        # ==========================
        elif text == "全部明細":
            grouped_orders = {}
            for o in current_orders:
                unit, name, item, price = o[2], o[4], o[5], int(o[7])
                if unit not in grouped_orders:
                    grouped_orders[unit] = {"users": {}, "total": 0}
                if name not in grouped_orders[unit]["users"]:
                    grouped_orders[unit]["users"][name] = {"items": [], "total": 0}
                    
                grouped_orders[unit]["users"][name]["items"].append(item)
                grouped_orders[unit]["users"][name]["total"] += price
                grouped_orders[unit]["total"] += price
                section_total += price

            section_msg += f"\n👑 【{today_str} {meal_name} 全署明細】\n"
            section_msg += f"本餐總金額：${section_total}\n" + "=" * 15 + "\n"
            
            for unit, unit_data in grouped_orders.items():
                section_msg += f"🏢 [{unit}] (小計 ${unit_data['total']})\n"
                for name, user_data in unit_data["users"].items():
                    items_str = ", ".join(user_data["items"])
                    section_msg += f"  👤 {name}：{items_str} (${user_data['total']})\n"
                section_msg += "-" * 15 + "\n"

        final_reply += section_msg

    if not final_reply:
        _reply_text(reply_token, f"📝 查詢完畢，但【{text}】目前沒有符合條件的訂單資料喔！")
        return

    _reply_text(reply_token, final_reply.strip())

def _handle_reports_menu(reply_token: str, user_id: str, access_token: str):
    """
    整合報表指令，依權限顯示快速回覆按鈕
    """
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()
    
    # 取得使用者角色資訊
    _, user_unit, user_role = _get_user_info(service, sheet_id, user_id)
    
    quick_reply_items = []

    # 1. 單位明細 (有綁定單位即可看)
    if user_unit:
        quick_reply_items.append({
            "type": "action",
            "action": {"type": "message", "label": "🏢 單位明細", "text": "單位明細"}
        })

    # 2. 老闆結單 (管理者權限)
    if user_role in ["老闆", "超級管理員", "ADMIN"]:
        quick_reply_items.append({
            "type": "action",
            "action": {"type": "message", "label": "🍱 老闆結單", "text": "老闆結單"}
        })

    # 3. 全部明細 (超級管理員)
    if user_role in ["超級管理員", "ADMIN"]:
        quick_reply_items.append({
            "type": "action",
            "action": {"type": "message", "label": "👑 全部明細", "text": "全部明細"}
        })

    if not quick_reply_items:
        _reply_text(reply_token, "⚠️ 您目前沒有權限查看報表，請先完成單位綁定。")
        return

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": "📊 請選擇您要查看的報表：",
                "quickReply": {"items": quick_reply_items[:13]}
            }
        ]
    }
    _send_line_payload(payload, access_token)

# =========================
# 彈出綁定快捷按鈕
# =========================
def _prompt_binding(reply_token: str, access_token: str, pending_item: int = 0, pending_meal: str = ""):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()
    
    rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B20")
    quick_reply_items = []
    for r in rows:
        if len(r) > 0:
            key = str(r[0]).strip()
            val = str(r[1]).strip() if len(r) > 1 else ""
            
            if "氣象署" in key:
                display_name = val if val else key
                
                # 👇 新增：把 LUNCH/DINNER 也塞進按鈕指令裡！
                if pending_item > 0:
                    meal_part = f" {pending_meal}" if pending_meal else ""
                    action_text = f"綁定並點餐 {display_name}{meal_part} {pending_item}" 
                else:
                    action_text = f"綁定單位 {display_name}"
                
                quick_reply_items.append({"type": "action", "action": {"type": "message", "label": display_name[:20], "text": action_text}})
                
    if not quick_reply_items:
        meal_part = f" {pending_meal}" if pending_meal else ""
        fallback_txt = f"綁定並點餐 未分類群組{meal_part} {pending_item}" if pending_item > 0 else "綁定單位 未分類群組"
        quick_reply_items = [{"type": "action", "action": {"type": "message", "label": "未分類群組", "text": fallback_txt}}]

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": "⚠️ 系統尚未建立您的資料！\n\n為了方便負責人發放便當，初次使用請先選擇您所在的群組/單位。\n\n👉 請點擊下方按鈕完成綁定：",
                "quickReply": {"items": quick_reply_items[:13]}
            }
        ]
    }
    _send_line_payload(payload, access_token)

# =========================
# 執行寫入訂單核心邏輯
# =========================
def _execute_order(reply_token, user_id, display_name, user_unit, item_num, access_token, service, sheet_id, is_new_bind=False, target_meal_type=None):
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    rows = _read_values(service, sheet_id, "logs!A:E")
    active_payload = None
    for r in reversed(rows):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                # 👇 新增：如果有指定餐別 (LUNCH/DINNER)，一定要對上才算數！
                if target_meal_type and payload.get("meal") != target_meal_type:
                    continue
                
                if payload.get("date") == today_str:
                    deadline_str = payload.get("deadlineAt", "")
                    if deadline_str:
                        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M").replace(tzinfo=tw_tz)
                        if now_tw <= deadline_dt:
                            active_payload = payload
                            break
            except: continue
    
    if not active_payload:
        meal_name = "午餐" if target_meal_type == "LUNCH" else ("晚餐" if target_meal_type == "DINNER" else "餐點")
        _reply_text(reply_token, f"目前沒有開放【{meal_name}】的菜單，或已超過截止時間囉！⏳")
        return
    
    item_idx = item_num - 1
    items = active_payload.get("items", [])
    if item_idx < 0 or item_idx >= len(items):
        _reply_text(reply_token, "找不到這個餐點編號喔！請重新點擊圖卡。")
        return
    
    target_item = items[item_idx]
    if not display_name:
        display_name = _get_line_profile(user_id, access_token)
    
    session_id = f"{active_payload.get('date')}_{active_payload.get('meal')}"
    item_name = target_item.get("name", "")
    price = target_item.get("price", 0)
    
    row_data = [
        now_tw.strftime("%Y-%m-%d %H:%M:%S"), 
        session_id,                           
        user_unit,                            
        user_id,                              
        display_name,                         
        item_name,                            
        1,                                    
        price,                                
        price,                                
        "未付款"                              
    ]
    
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="orders_log!A:J",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row_data]}
    ).execute()
    
    meal_txt = "午餐" if active_payload.get("meal") == "LUNCH" else "晚餐"
    bind_msg = f"🎉 成功綁定單位：{user_unit}\n" if is_new_bind else ""
    success_msg = f"{bind_msg}✅ {meal_txt}點餐成功！\n\n🏢 單位：{user_unit}\n🍱 餐點：{item_name}\n💰 價格：${price}\n\n感謝您的訂購！"
    _reply_text(reply_token, success_msg)

# =========================
# 點餐與綁定邏輯
# =========================
def _handle_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        parts = text.split(" ")
        target_meal = None
        item_num = 0

        # 支援兩種格式：1. "點餐 4" (舊版)  2. "點餐 LUNCH 4" (新版)
        if len(parts) == 3 and parts[1] in ["LUNCH", "DINNER"] and parts[2].isdigit():
            target_meal = parts[1]
            item_num = int(parts[2])
        elif len(parts) == 2 and parts[1].isdigit():
            item_num = int(parts[1])
        else:
            return

        sheet_id = os.getenv("SHEET_ID", "")
        service = _get_sheets_service()
        display_name, user_unit, _ = _get_user_info(service, sheet_id, user_id)

        if not user_unit:
            # 這裡把抓到的 target_meal 傳給綁定流程
            _prompt_binding(reply_token, access_token, pending_item=item_num, pending_meal=target_meal)
            return
        
        _execute_order(reply_token, user_id, display_name, user_unit, item_num, access_token, service, sheet_id, target_meal_type=target_meal)
    except Exception as e:
        print(f"Error: {e}")
        _reply_text(reply_token, "點餐發生錯誤，請稍後再試。")

def _handle_bind_and_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        # 支援解析："綁定並點餐 群組名稱 LUNCH 4"
        m_meal = re.match(r"^綁定並點餐\s+(.+)\s+(LUNCH|DINNER)\s+(\d+)$", text)
        m_legacy = re.match(r"^綁定並點餐\s+(.+)\s+(\d+)$", text)
        
        unit_name = ""
        target_meal = None
        item_num = 0
        
        if m_meal:
            unit_name = m_meal.group(1).strip()
            target_meal = m_meal.group(2)
            item_num = int(m_meal.group(3))
        elif m_legacy:
            unit_name = m_legacy.group(1).strip()
            item_num = int(m_legacy.group(2))
        else:
            return

        display_name = _get_line_profile(user_id, access_token)
        now_tw_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

        sheet_id = os.getenv("SHEET_ID", "")
        service = _get_sheets_service()

        row_data = [now_tw_str, user_id, display_name, unit_name, ""]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="USERS!A:E", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row_data]}
        ).execute()

        # 傳入 target_meal_type
        _execute_order(reply_token, user_id, display_name, unit_name, item_num, access_token, service, sheet_id, is_new_bind=True, target_meal_type=target_meal)
    except Exception as e:
        print(f"Bind Error: {e}")
        _reply_text(reply_token, "作業失敗，請稍後再試。")

def _handle_bind_unit(reply_token: str, user_id: str, text: str, access_token: str):
    unit_name = text.replace("綁定單位", "").strip()
    if not unit_name: return
    display_name = _get_line_profile(user_id, access_token)
    now_tw_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()
    row_data = [now_tw_str, user_id, display_name, unit_name, ""]
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range="USERS!A:E", valueInputOption="RAW",
        insertDataOption="INSERT_ROWS", body={"values": [row_data]}
    ).execute()
    _reply_text(reply_token, f"✅ 成功綁定為：{unit_name}\n👉 請再次點擊圖卡上的按鈕來點餐吧！")


# =========================
# 顯示菜單與發送圖卡
# =========================
def _handle_show_menu(reply_token: str, keyword: str):
    target_meal = "LUNCH" if "午餐" in keyword else "DINNER"
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()
    rows = _read_values(service, sheet_id, "logs!A:E")
    
    active_payload = None
    for r in reversed(rows):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if payload.get("date") == today_str and payload.get("meal") == target_meal:
                    active_payload = payload
                    break
            except: continue

    if not active_payload:
        _reply_text(reply_token, f"老闆還沒發布【{keyword}】的菜單喔！")
        return

    deadline_str = active_payload.get("deadlineAt", "")
    if deadline_str:
        try:
            deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M").replace(tzinfo=tw_tz)
            if now_tw > deadline_dt:
                _reply_text(reply_token, f"不好意思，今天的【{keyword}】已於 {deadline_str} 截止點餐囉！⏳")
                return
        except Exception as e: pass

    settings = {}
    setting_rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B15")
    for r in setting_rows:
        if len(r) > 0:
            key = str(r[0]).strip()
            val = str(r[1]).strip() if len(r) > 1 else ""
            settings[key] = val

    prefix = "午餐" if target_meal == "LUNCH" else "晚餐"
    title_color = settings.get(f"訂{prefix}標題顏色", "#1DB446")
    btn_color = settings.get(f"訂{prefix}按鈕顏色", "#1DB446")
    btn_style = settings.get(f"訂{prefix}按鈕樣式", "primary").lower()

    vendor = active_payload.get("vendor", "未知店家")
    items = active_payload.get("items", [])
    
    bubbles = []
    for i, item in enumerate(items, 1):
        # 👇 修改：按鈕指令加入 target_meal (LUNCH/DINNER)
        btn_action_text = f"點餐 {target_meal} {i}"

        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": prefix, "weight": "bold", "color": title_color, "size": "lg", "align": "center", "margin": "md"},
                    {"type": "text", "text": f"[{i}] {item['name']}", "weight": "bold", "size": "md", "wrap": True},
                    {"type": "text", "text": f"${item['price']}", "weight": "bold", "color": title_color, "size": "md", "margin": "md"},
                    
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [{"type": "button", "style": btn_style, "color": btn_color, "height": "sm", "action": {"type": "message", "label": "👉 點這份", "text": btn_action_text}}]
            }
        }
        bubbles.append(bubble)

    messages = [{"type": "text", "text": f"🍱 【{keyword}開放點餐中】\n🏪 店家：{vendor}\n⏳ 截止時間：{deadline_str}\n\n👇 請左右滑動圖卡，點擊按鈕直接點餐👇"}]
    for i in range(0, len(bubbles), 10):
        messages.append({"type": "flex", "altText": f"🍱 {keyword}菜單", "contents": {"type": "carousel", "contents": bubbles[i:i+10]}})
        if len(messages) >= 5: break

    _send_line_payload({"replyToken": reply_token, "messages": messages}, os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))

# =========================
# 發送群組開團通知
# =========================
def _send_order_flex_message(reply_token: str, access_token: str, meal_type: str):
    sheet_id = os.getenv("SHEET_ID", "")
    settings = {}
    if sheet_id:
        service = _get_sheets_service()
        rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B15")
        for r in rows:
            if len(r) > 0:
                key = str(r[0]).strip()
                val = str(r[1]).strip() if len(r) > 1 else ""
                settings[key] = val

    prefix = "午餐" if meal_type == "LUNCH" else "晚餐"
    img_url = _convert_drive_link(settings.get(f"訂{prefix}主圖", ""))
    title_color = settings.get(f"訂{prefix}標題顏色", "#1DB446")
    btn_color = settings.get(f"訂{prefix}按鈕顏色", "#1DB446")
    btn_style = settings.get(f"訂{prefix}按鈕樣式", "primary").lower() 
    BOT_CHAT_URL = "https://lin.ee/mHcmIiP"

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": f"🔔 {prefix}點餐通知", "weight": "bold", "color": title_color, "size": "sm"},
                {"type": "text", "text": "最新菜單已發布", "weight": "bold", "size": "xl", "margin": "md"},
                {"type": "text", "text": "為了避免群組洗版，請點擊下方按鈕，前往「私訊」機器人完成點餐喔！", "size": "xs", "color": "#666666", "wrap": True, "margin": "md"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "button", "style": btn_style, "color": btn_color, "action": {"type": "uri", "label": "💬 點我私訊點餐", "uri": BOT_CHAT_URL}}
            ]
        }
    }
    if img_url:
        bubble["hero"] = {"type": "image", "url": img_url, "size": "full", "aspectRatio": "1:1", "aspectMode": "cover"}

    _send_line_payload({"replyToken": reply_token, "messages": [{"type": "flex", "altText": f"🍱 {prefix}點餐時間到囉！請私訊機器人", "contents": bubble}]}, access_token)

# =========================
# 修改訂單 (列出使用者當日有效訂單)
# =========================
def _handle_modify_order(reply_token: str, user_id: str, text: str, access_token: str):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()

    # 1. 抓取今天「所有」尚未截止的 Session
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    logs = _read_values(service, sheet_id, "logs!A:E")
    active_sessions = {} # 用 dict 存 session_id -> meal_name
    
    for r in reversed(logs):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
                if payload.get("date") == today_str:
                    # 檢查是否過期 (修改訂單通常也需要在截止前)
                    deadline_str = payload.get("deadlineAt", "")
                    if deadline_str:
                        deadline_dt = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M").replace(tzinfo=tw_tz)
                        if now_tw > deadline_dt:
                            continue # 過期就不能改了
                    
                    sid = f"{payload.get('date')}_{payload.get('meal')}"
                    m_name = "午餐" if payload.get("meal") == "LUNCH" else "晚餐"
                    active_sessions[sid] = m_name
            except: continue

    if not active_sessions:
        _reply_text(reply_token, "🕒 目前沒有開放修改的訂單 (可能尚未開團或已截止)。")
        return

    # 2. 撈取使用者的訂單 (且狀態不能是已取消)
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    user_orders = []
    
    # 訂單結構: timestamp[0], sessionId[1], ..., lineUserId[3], item[5], ..., paymentStatus[9]
    for i, o in enumerate(orders):
        if len(o) >= 10 and o[3] == user_id:
            sid = o[1]
            status = o[9]
            if sid in active_sessions and status != "已取消":
                # 記錄 row_index (Excel 是從 1 開始，列表是 0，加上 header 1行，所以是 i+1)
                # 為了安全，我們用 timestamp 做為取消的驗證 token
                user_orders.append({
                    "row_idx": i + 1,
                    "timestamp": o[0],
                    "meal": active_sessions[sid],
                    "item": o[5],
                    "price": o[7]
                })

    if not user_orders:
        _reply_text(reply_token, "📝 您今天還沒有任何有效訂單喔！")
        return

    # 3. 製作 Flex Message 列表 (可以取消)
    bubbles = []
    for order in user_orders:
        # 按鈕指令: 取消訂單 <timestamp>
        # (用 timestamp 當 ID 比較安全，不會因為別人新增訂單導致 row 跑掉)
        cancel_cmd = f"取消訂單 {order['timestamp']}"
        
        # 根據餐別決定顏色
        color = "#E6A817" if order['meal'] == "午餐" else "#6F42C1"

        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": order['meal'], "weight": "bold", "color": color, "size": "md"},
                    {"type": "text", "text": order['item'], "weight": "bold", "size": "md", "wrap": True, "margin": "xs"},
                    {"type": "text", "text": f"${order['price']}", "size": "md", "color": "#666666", "margin": "xs"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {
                        "type": "button", "style": "primary", "color": "#FF3333", "height": "sm",
                        "action": {"type": "message", "label": "取消", "text": cancel_cmd}
                    }
                ]
            }
        }
        bubbles.append(bubble)

    msg = {
        "type": "flex",
        "altText": "修改訂單",
        "contents": {
            "type": "carousel",
            "contents": bubbles
        }
    }
    
    _send_line_payload({"replyToken": reply_token, "messages": [msg]}, access_token)

# =========================
# 執行取消訂單
# =========================
def _handle_cancel_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        # 指令格式: "取消訂單 <timestamp>"
        parts = text.split(" ", 1)
        if len(parts) < 2: return
        target_ts = parts[1].strip()

        sheet_id = os.getenv("SHEET_ID", "")
        service = _get_sheets_service()

        # 1. 重新讀取訂單，找到對應的那一行
        # (不能只靠前端傳來的 row_index，因為多人同時點餐時 row 可能會變)
        orders = _read_values(service, sheet_id, "orders_log!A:J")
        target_row_idx = -1
        target_item_name = ""
        
        for i, o in enumerate(orders):
            # 比對 timestamp [0] 和 userId [3] (雙重驗證，防止刪到別人的)
            if len(o) >= 4 and o[0] == target_ts and o[3] == user_id:
                target_row_idx = i + 1
                target_item_name = o[5] if len(o) > 5 else "餐點"
                
                # 如果已經取消過了，就提示一下
                if len(o) >= 10 and o[9] == "已取消":
                    _reply_text(reply_token, "這筆訂單已經取消過囉！")
                    return
                break
        
        if target_row_idx == -1:
            _reply_text(reply_token, "找不到這筆訂單，可能已經過期或系統資料異動。")
            return

        # 2. 更新 Google Sheets (把 J 欄 PaymentStatus 改為 "已取消")
        # Range 寫法: orders_log!J{row}:J{row}
        update_range = f"orders_log!J{target_row_idx}"
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=update_range,
            valueInputOption="RAW",
            body={"values": [["已取消"]]}
        ).execute()

        _reply_text(reply_token, f"🗑️ 已為您取消：{target_item_name}\n\n如需加點其他餐點，請重新點選菜單。")

    except Exception as e:
        print(f"Cancel Error: {e}")
        _reply_text(reply_token, "取消失敗，請稍後再試。")

def check_and_set_rich_menu(user_id):
    """檢查使用者角色並設定對應的圖文選單"""
    # 1. 從 USERS 工作表讀取資料
    # 假設你已定義好讀取 Google Sheets 的工具函式
    df_users = get_sheet_data("USERS") 
    
    # 2. 尋找該 user_id 的角色
    user_row = df_users[df_users['lineUserId'] == user_id]
    
    # 預設選單 ID (可在 LINE_SETTING 工作表定義)
    USER_MENU_ID = "richmenu-xxxxxxxxuser" 
    ADMIN_MENU_ID = "richmenu-yyyyyyyyadmin"
    
    if not user_row.empty:
        role = user_row.iloc[0]['角色']
        if role == 'ADMIN':
            # 綁定管理員選單
            line_bot_api.link_rich_menu_to_user(user_id, ADMIN_MENU_ID)
            return "ADMIN"
    
    # 若非管理員或查無資料，綁定一般選單
    line_bot_api.link_rich_menu_to_user(user_id, USER_MENU_ID)
    return "USER"

def _sync_rich_menu(user_id, reply_token=None):
    """根據 USERS 工作表同步使用者的圖文選單"""
    try:
        sheet_id = os.getenv("SHEET_ID")
        service = _get_sheets_service()
        
        # 1. 讀取 USERS 工作表 (假設 A 欄是時間, B 欄是 lineUserId, E 欄是角色)
        users_data = _read_values(service, sheet_id, "USERS!A:E")
        
        user_role = "USER" # 預設角色
        for row in users_data:
            if len(row) >= 5 and row[1] == user_id:
                user_role = row[4] # 取得「角色」欄位
                break
        
        # 2. 定義你的 Rich Menu ID (請替換為步驟 1 取得的真實 ID)
        # 也可以寫在 LINE_SETTING 工作表由程式讀取
        RICH_MENU_USER = "richmenu-18883912"  # 使用者選單 (18883912)
        RICH_MENU_ADMIN = "richmenu-18904595" # 管理者選單 (18904595)
        
        target_id = RICH_MENU_ADMIN if user_role == "ADMIN" else RICH_MENU_USER
        
        # 3. 呼叫 LINE API 進行綁定
        # 注意：你需要確保 line_bot_api 已在該作用域中定義
        _line_api_call("POST", f"/bot/user/{user_id}/richmenu/{target_id}")
        
    except Exception as e:
        print(f"同步選單失敗: {e}")

def _line_api_call(method, path, body=None):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    url = f"https://api.line.me/v2{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
        with urllib.request.urlopen(req, data=data) as f:
            return f.read()
    else:
        with urllib.request.urlopen(req) as f:
            return f.read()

def _link_rich_menu(user_id, rich_menu_id):
    """呼叫 LINE API 將選單綁定給特定使用者"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    url = f"https://api.line.me/v2/bot/user/{user_id}/richmenu/{rich_menu_id}"
    
    req = urllib.request.Request(url, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req) as f:
            return True
    except Exception as e:
        print(f"Rich Menu Link Error: {e}")
        return False

def _unlink_rich_menu(user_id):
    """解除個人選單綁定，回歸帳號預設選單"""
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    url = f"https://api.line.me/v2/bot/user/{user_id}/richmenu"
    
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req) as f:
            return True
    except Exception as e:
        print(f"Rich Menu Unlink Error: {e}")
        return False