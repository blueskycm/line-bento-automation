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
    user_rows = _read_values(service, sheet_id, "USERS!A:F") # 讀取到 F 欄
    for r in reversed(user_rows):
        if len(r) >= 4 and r[1] == user_id:
            role = str(r[4]).strip() if len(r) >= 5 else ""
            phone = str(r[5]).strip() if len(r) >= 6 else "" # 取得 F 欄電話
            return r[2], r[3], role, phone # 回傳四個值
    return None, None, "", ""

# =========================
# Webhook 主程式
# =========================
@https_fn.on_request(region="asia-east1", memory=512, secrets=["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "SHEET_ID"])
def line_webhook(req: https_fn.Request) -> Response:
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
    channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    sheet_id = os.getenv("SHEET_ID", "")

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

        # --- 自動切換選單邏輯 ---
        if user_id and (event_type == "follow" or (event_type == "message" and event.get("message", {}).get("type") == "text")):
            try:
                service = _get_sheets_service()
                # 這裡直接呼叫您寫好的工具函式取得角色
                _, _, user_role, _ = _get_user_info(service, sheet_id, user_id)
                
                # 管理者選單 ID
                ADMIN_MENU_ID = "richmenu-0661c63130d18fb63b40b6db5a1fddad"
                
                # 統一權限清單
                ADMIN_ROLES = ["老闆", "超級管理員", "ADMIN"]
                
                if user_role in ADMIN_ROLES:
                    _link_rich_menu(user_id, ADMIN_MENU_ID)
                else:
                    _unlink_rich_menu(user_id)
            except Exception as e:
                print(f"Rich Menu Sync Error: {e}")

        if event_type == "follow":
            try:
                service = _get_sheets_service()
                # 1. 取得使用者 LINE 暱稱
                display_name = _get_line_profile(user_id, channel_access_token)
                
                # 2. 檢查是否已在 USERS 名冊中
                existing_name, _, _, _ = _get_user_info(service, sheet_id, user_id)
                
                if not existing_name:
                    # 3. 準備寫入資料
                    now_tw_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                    new_user_row = [now_tw_str, user_id, display_name, "", "USER"]
                    
                    service.spreadsheets().values().append(
                        spreadsheetId=sheet_id, 
                        range="USERS!A:E", 
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS", 
                        body={"values": [new_user_row]}
                    ).execute()
                    
                    # 4. 發送歡迎詞
                    welcome_msg = f"歡迎加入，{display_name}！\n\n初次點餐時，小幫手會引導您綁定群組。"
                    _reply_text(event.get("replyToken"), welcome_msg)
                
            except Exception as e:
                print(f"Follow Registration Error: {e}")

        # --- 原有的 handle_message 邏輯 ---
        if event_type == "message" and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()
            reply_token = event.get("replyToken")

            # ==========================================
            # 優先處理「純數字」：這必須是第一個判斷！
            # ==========================================
            if text.isdigit():
                service = _get_sheets_service()
                # 讀取狀態表
                states_rows = _read_values(service, sheet_id, "user_states!A:B")
                # 確保 user_id 字串比對正確
                user_state_data = next((r for r in states_rows if len(r) > 1 and str(r[0]).strip() == str(user_id).strip()), None)
                
                if user_state_data and user_state_data[1].startswith("SET_QTY|"):
                    _, meal, item_num = user_state_data[1].split("|")
                    qty = int(text)
                    
                    # 1. 立即清除狀態
                    _set_user_state(service, sheet_id, user_id, "")
                    
                    # 2. 取得用戶資訊
                    display_name, user_unit, _, _ = _get_user_info(service, sheet_id, user_id)
                    
                    # 3. 執行點餐並強制結束 Webhook 請求
                    if user_unit:
                        _execute_order(reply_token, user_id, display_name, user_unit, int(item_num), 
                                       channel_access_token, service, sheet_id, 
                                       target_meal_type=meal, quantity=qty)
                    else:
                        _prompt_binding(reply_token, channel_access_token, 
                                        pending_item=int(item_num), 
                                        pending_meal=meal, 
                                        pending_qty=qty)
                    return Response("OK", status=200)

            # 根據關鍵字分流處理
            if "餐 開團囉！" in text:
                meal_type = "LUNCH" if "午餐" in text else "DINNER"
                _send_order_flex_message(reply_token, channel_access_token, meal_type)

            elif text in ["今日午餐", "今日晚餐"]:
                _handle_show_menu(reply_token, text)

            elif text.startswith("手動輸入點餐 "):
                service = _get_sheets_service()
                _handle_manual_input_trigger(reply_token, user_id, text, service, sheet_id)
            elif text.startswith("我要訂 "):
                m = re.match(r"我要訂 (LUNCH|DINNER) (\d+) 數量是 (\d+)", text)
                if m:
                    meal, item_num, qty = m.groups()
                    _handle_order(reply_token, user_id, f"點餐 {meal} {item_num} {qty}", channel_access_token)

            elif text.startswith("點餐 "):
                _handle_order(reply_token, user_id, text, channel_access_token)

            elif text.startswith("綁定並點餐 "):
                _handle_bind_and_order(reply_token, user_id, text, channel_access_token)

            elif text.startswith("綁定群組 "):
                _handle_bind_unit(reply_token, user_id, text, channel_access_token)

            elif text.startswith("取消訂單 "):
                _handle_cancel_order(reply_token, user_id, text, channel_access_token)

            elif text == "修改訂單":
                _handle_modify_order(reply_token, user_id, text, channel_access_token)
            
            elif text.startswith("選擇數量 "):
                _handle_select_quantity(reply_token, text)
            
            elif text == "數據報表":
                _handle_reports_menu(reply_token, user_id, channel_access_token)

            elif text in ["老闆結單", "單位明細", "全部明細"]:
                _handle_reports(reply_token, user_id, text, channel_access_token)

    return Response("OK", status=200)

# =========================
# 報表指令 (排除已取消訂單)
# =========================
def _handle_reports(reply_token: str, user_id: str, text: str, access_token: str):
    sheet_id = os.getenv("SHEET_ID", "")
    service = _get_sheets_service()

    # 1. 取得資訊 (修正：接收 4 個變數)
    display_name, user_unit, user_role, user_phone = _get_user_info(service, sheet_id, user_id)

    # 2. 權限檢查
    if text == "老闆結單" and user_role not in ["老闆", "超級管理員", "ADMIN"]:
        _reply_text(reply_token, "⛔ 您沒有權限查看老闆結單。")
        return
    if text == "全部明細" and user_role not in ["超級管理員", "ADMIN"]:
        _reply_text(reply_token, "⛔ 此為超級管理員專用指令。")
        return
    if text == "單位明細" and not user_unit:
        _reply_text(reply_token, "⛔ 您尚未綁定群組，無法查看明細。")
        return

    tw_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tw_tz).strftime("%Y-%m-%d")

    # 3. 撈取今日所有有效訂單
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    today_orders = [o for o in orders if len(o) >= 10 and o[9] != "已取消" and today_str in o[1]]
    
    if not today_orders:
        _reply_text(reply_token, "📝 今日尚無訂單資料。")
        return

    messages_to_send = []

    # ==========================
    # 📝 報表 A：單位明細 (第一則訊息)
    # ==========================
    if text == "單位明細":
        unit_msg = ""
        for m_type in ["LUNCH", "DINNER"]:
            m_name = "午餐" if m_type == "LUNCH" else "晚餐"
            sid_prefix = f"{today_str}_{m_type}"
            
            unit_orders = [o for o in today_orders if o[1] == sid_prefix and o[2] == user_unit]
            if not unit_orders: continue

            # 修改：加總 o[8] (小計)
            section_total = sum(int(o[8]) for o in unit_orders)
            user_totals = {}
            for o in unit_orders:
                # o[4]: 姓名, o[5]: 品項, o[6]: 數量, o[8]: 小計
                name, item, qty, subtotal = o[4], o[5], int(o[6]), int(o[8])
                if name not in user_totals:
                    user_totals[name] = {"items": [], "total": 0}
                user_totals[name]["items"].append(f"{item} x{qty}")
                user_totals[name]["total"] += subtotal

            unit_msg += f"🏢 【{today_str} {m_name}】\n"
            unit_msg += f"本餐總金額：${section_total}\n" + "-" * 15 + "\n"
            for name, data in user_totals.items():
                unit_msg += f"👤 {name}：{', '.join(data['items'])} (${data['total']})\n"
            unit_msg += "\n"

        if unit_msg:
            messages_to_send.append({"type": "text", "text": unit_msg.strip()})
        text = "老闆結單" # 自動連鎖顯示老闆結單 

    # ==========================
    # 📝 報表 B：老闆結單 (第二則訊息)
    # ==========================
    if text == "老闆結單":
        lunch_counts, dinner_counts = {}, {}
        unit_totals = {}
        grand_total = 0

        for o in today_orders:
            if text == "老闆結單" and "🏢" not in (messages_to_send[0]["text"] if messages_to_send else ""):
                pass
            elif o[2] != user_unit: 
                continue 
            
            # o[1]: session, o[5]: 品項, o[6]: 數量, o[8]: 小計
            sid_inner, item, qty, subtotal = o[1], o[5], int(o[6]), int(o[8])
            unit_totals[o[2]] = unit_totals.get(o[2], 0) + subtotal
            grand_total += subtotal

            if "LUNCH" in sid_inner:
                lunch_counts[item] = lunch_counts.get(item, 0) + qty # 修改：累加數量
            elif "DINNER" in sid_inner:
                dinner_counts[item] = dinner_counts.get(item, 0) + qty # 修改：累加數量

        phone_display = f"(電話：{user_phone})" if user_phone else ""
        boss_msg = f"🍱 【{today_str} 統計】{phone_display}\n"
        boss_msg += "-" * 15 + "\n"

        for item, count in lunch_counts.items(): boss_msg += f"🔸 {item}：{count} 份\n"
        if lunch_counts and dinner_counts: boss_msg += "-" * 15 + "\n"
        for item, count in dinner_counts.items(): boss_msg += f"🔸 {item}：{count} 份\n"

        boss_msg += "-" * 15 + "\n"
        boss_msg += f"💰 應收合計：${grand_total}\n"
        for unit, total in unit_totals.items():
            boss_msg += f"  🏢 {unit}：${total}\n"
        
        messages_to_send.append({"type": "text", "text": boss_msg.strip()})

    # ==========================
    # 📝 報表 C：全部明細 (管理員專用)
    # ==========================
    elif text == "全部明細":
        all_msg = f"👑 【{today_str} 全署明細】\n" + "=" * 15 + "\n"
        grand_total = 0
        grouped = {}

        # 遍歷今日所有有效訂單
        for o in today_orders:
            # o[2]: 群組, o[4]: 姓名, o[5]: 品項, o[6]: 數量, o[8]: 小計
            unit, name, item, qty, subtotal = o[2], o[4], o[5], int(o[6]), int(o[8])
            
            if unit not in grouped: 
                grouped[unit] = {"users": {}, "total": 0}
            if name not in grouped[unit]["users"]: 
                grouped[unit]["users"][name] = {"items": [], "total": 0}
            
            # 整合顯示格式：品項名稱 x 數量
            grouped[unit]["users"][name]["items"].append(f"{item} x{qty}")
            grouped[unit]["users"][name]["total"] += subtotal
            grouped[unit]["total"] += subtotal
            grand_total += subtotal

        # 組合訊息字串
        for unit, u_data in grouped.items():
            all_msg += f"🏢 [{unit}] (小計 ${u_data['total']})\n"
            for name, p_data in u_data["users"].items():
                all_msg += f"  👤 {name}：{', '.join(p_data['items'])} (${p_data['total']})\n"
            all_msg += "-" * 15 + "\n"
        
        all_msg += f"💰 總計金額：${grand_total}"
        messages_to_send.append({"type": "text", "text": all_msg.strip()})

    # 4. 最終發送
    if messages_to_send:
        _send_line_payload({"replyToken": reply_token, "messages": messages_to_send}, access_token)

def _handle_reports_menu(reply_token: str, user_id: str, access_token: str):
    service = _get_sheets_service()
    sheet_id = os.getenv("SHEET_ID", "")
    _, user_unit, user_role, _ = _get_user_info(service, sheet_id, user_id)
    
    role = str(user_role).strip().upper() # 轉大寫去空格
    ADMIN_ROLES = ["老闆", "超級管理員", "ADMIN"]
    
    quick_reply_items = []
    if user_unit:
        quick_reply_items.append({"type": "action", "action": {"type": "message", "label": "🏢 單位明細", "text": "單位明細"}})
    
    if any(r in role for r in ADMIN_ROLES) or role in ADMIN_ROLES:
        quick_reply_items.append({"type": "action", "action": {"type": "message", "label": "🍱 老闆結單", "text": "老闆結單"}})
        # 雖然圖文選單有網址，但快速回覆裡保留文字指令作為備援
        quick_reply_items.append({"type": "action", "action": {"type": "message", "label": "👑 全部明細", "text": "全部明細"}})

    if not quick_reply_items:
        _reply_text(reply_token, "⚠️ 您目前沒有權限查看報表，請先完成群組綁定。")
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
def _prompt_binding(reply_token: str, access_token: str, pending_item: int = 0, pending_meal: str = "", pending_qty: int = 1):
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
                
                # 指令格式變更為 "綁定並點餐 [群組] [餐別] [編號] [數量]"
                if pending_item > 0:
                    meal_part = f" {pending_meal}" if pending_meal else ""
                    action_text = f"綁定並點餐 {display_name}{meal_part} {pending_item} {pending_qty}" 
                else:
                    action_text = f"綁定群組 {display_name}"
                
                quick_reply_items.append({
                    "type": "action", 
                    "action": {"type": "message", "label": display_name[:20], "text": action_text}
                })
    if not quick_reply_items:
        meal_part = f" {pending_meal}" if pending_meal else ""
        fallback_txt = f"綁定並點餐 未分類群組{meal_part} {pending_item}" if pending_item > 0 else "綁定群組 未分類群組"
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
def _execute_order(reply_token, user_id, display_name, user_unit, item_num, access_token, service, sheet_id, is_new_bind=False, target_meal_type=None, quantity=1):
    tw_tz = timezone(timedelta(hours=8))
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")

    # 1. 取得當前餐期資訊 (Active Payload)
    rows = _read_values(service, sheet_id, "logs!A:E")
    active_payload = None
    for r in reversed(rows):
        if len(r) >= 5 and r[3] == "PUBLISH_MENU":
            try:
                payload = json.loads(r[4])
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
    
    # 2. 餐點檢索
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
    price = int(target_item.get("price", 0))

    # 3. 檢查現有訂單是否需要合併
    orders = _read_values(service, sheet_id, "orders_log!A:J")
    existing_row_idx = -1
    old_qty = 0

    for i, o in enumerate(orders):
        if len(o) >= 10 and o[1] == session_id and o[3] == user_id and o[5] == item_name and o[9] != "已取消":
            existing_row_idx = i + 1
            old_qty = int(o[6])
            break

    # 4. 寫入或更新試算表
    if existing_row_idx > -1:
        new_qty = old_qty + quantity
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"orders_log!G{existing_row_idx}:I{existing_row_idx}",
            valueInputOption="RAW",
            body={"values": [[new_qty, price, new_qty * price]]}
        ).execute()
        op_title = "訂單已更新"
    else:
        new_qty = quantity
        row_data = [
            now_tw.strftime("%Y-%m-%d %H:%M:%S"), session_id, user_unit, user_id, 
            display_name, item_name, quantity, price, quantity * price, "未付款"
        ]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="orders_log!A:J", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row_data]}
        ).execute()
        op_title = "點餐成功"
    
    # 5. 格式化成功訊息
    meal_label = "午餐" if active_payload.get("meal") == "LUNCH" else "晚餐"
    bind_msg = f"🎉 成功綁定群組：{user_unit}\n" if is_new_bind else ""
    
    # 計算本次點餐的小計 (若是累加，則顯示本次增加的金額)
    this_time_subtotal = quantity * price
    
    success_msg = (
        f"{bind_msg}✅ 【{meal_label}{op_title}】\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏢 群組：{user_unit}\n"
        f"🍱 品項：{item_name}\n"
        f"🔢 數量：{quantity} 份\n"
        f"💰 本次小計：${this_time_subtotal} 元\n"
        f"━━━━━━━━━━━━━━━\n"
    )
    
    # 如果是更新，多顯示一列總計
    if existing_row_idx > -1:
        success_msg += f"📊 目前總計：{new_qty} 份 (${new_qty * price} 元)\n"
        
    success_msg += "💡 如需修改，請點選選單中的「修改訂單」。"
    
    _reply_text(reply_token, success_msg)

# =========================
# 點餐與綁定邏輯
# =========================
def _handle_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        parts = text.split(" ")
        target_meal = None
        item_num = 0
        quantity = 1

        # 支援格式： 點餐 LUNCH 1 [數量]
        if len(parts) >= 3 and parts[1] in ["LUNCH", "DINNER"] and parts[2].isdigit():
            target_meal = parts[1]
            item_num = int(parts[2])
            if len(parts) >= 4 and parts[3].isdigit():
                quantity = int(parts[3])
        
        # 支援舊格式： 點餐 1 (預設當前餐期)
        elif len(parts) == 2 and parts[1].isdigit():
            item_num = int(parts[1])
        else:
            return

        sheet_id = os.getenv("SHEET_ID", "")
        service = _get_sheets_service()
        display_name, user_unit, _, _ = _get_user_info(service, sheet_id, user_id)

        if not user_unit:
            # 把傳入的 quantity 帶給 _prompt_binding
            _prompt_binding(reply_token, access_token, 
                            pending_item=item_num, 
                            pending_meal=target_meal, 
                            pending_qty=quantity)
            return
        
        # 呼叫執行函數時，多傳入 quantity
        _execute_order(reply_token, user_id, display_name, user_unit, item_num, 
                       access_token, service, sheet_id, 
                       target_meal_type=target_meal, quantity=quantity)
    except Exception as e:
        print(f"Error in _handle_order: {e}")
        _reply_text(reply_token, "點餐格式錯誤或系統忙碌中。")

def _handle_bind_and_order(reply_token: str, user_id: str, text: str, access_token: str):
    try:
        # 定義當前時間 (修正原本未定義 now_tw_str 的錯誤)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        now_tw_str = now_tw.strftime("%Y-%m-%d %H:%M:%S")

        # 支援解析："綁定並點餐 群組名稱 LUNCH 1 10"
        m_qty = re.match(r"^綁定並點餐\s+(.+)\s+(LUNCH|DINNER)\s+(\d+)\s+(\d+)$", text)
        m_meal = re.match(r"^綁定並點餐\s+(.+)\s+(LUNCH|DINNER)\s+(\d+)$", text)
        
        unit_name = ""
        target_meal = None
        item_num = 0
        quantity = 1 # 預設
        
        if m_qty:
            unit_name, target_meal, item_num, quantity = m_qty.groups()
        elif m_meal:
            unit_name, target_meal, item_num = m_meal.groups()
        else:
            return

        item_num = int(item_num)
        quantity = int(quantity)

        display_name = _get_line_profile(user_id, access_token)
        service = _get_sheets_service()
        sheet_id = os.getenv("SHEET_ID", "")

        # 寫入 USERS 表格完成綁定
        row_data = [now_tw_str, user_id, display_name, unit_name, "USER"]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="USERS!A:E", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row_data]}
        ).execute()

        # 呼叫執行點餐，將數量傳進去
        _execute_order(reply_token, user_id, display_name, unit_name, item_num, 
                       access_token, service, sheet_id, 
                       is_new_bind=True, target_meal_type=target_meal, quantity=quantity)
    except Exception as e:
        print(f"Bind and Order Error: {e}")
        _reply_text(reply_token, "綁定並點餐時發生錯誤，請稍後再試。")

def _handle_bind_unit(reply_token: str, user_id: str, text: str, access_token: str):
    unit_name = text.replace("綁定群組", "").strip()
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
        # 改為觸發數量選擇的指令
        btn_action_text = f"選擇數量 {target_meal} {i}"

        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": prefix, "weight": "bold", "color": title_color, "size": "lg", "align": "center", "margin": "md"},
                    {"type": "text", "text": f"[{i}] {item['name']}", "weight": "bold", "size": "md", "wrap": True},
                    {"type": "text", "text": f"${item['price']}", "weight": "bold", "color": title_color, "size": "md", "margin": "md"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [{
                    "type": "button", "style": btn_style, "color": btn_color, "height": "sm", 
                    "action": {"type": "message", "label": "👉 點這份", "text": btn_action_text}
                }]
            }
        }
        bubbles.append(bubble)

    messages = [{"type": "text", "text": f"🍱 【{keyword}開放點餐中】\n🏪 店家：{vendor}\n⏳ 截止時間：{deadline_str}\n\n👇 請左右滑動圖卡，點擊按鈕直接點餐👇"}]
    for i in range(0, len(bubbles), 10):
        messages.append({"type": "flex", "altText": f"🍱 {keyword}菜單", "contents": {"type": "carousel", "contents": bubbles[i:i+10]}})
        if len(messages) >= 5: break

    _send_line_payload({"replyToken": reply_token, "messages": messages}, os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))

def _handle_select_quantity(reply_token: str, text: str):
    # 解析出 餐別 與 編號 (例如: 選擇數量 LUNCH 1)
    parts = text.split(" ")
    meal = parts[1]
    item_num = parts[2]
    
    # 建立快速回覆按鈕
    quick_reply_items = [
        {"type": "action", "action": {"type": "message", "label": "1 份", "text": f"點餐 {meal} {item_num} 1"}},
        {"type": "action", "action": {"type": "message", "label": "5 份", "text": f"點餐 {meal} {item_num} 5"}},
        {"type": "action", "action": {"type": "message", "label": "10 份", "text": f"點餐 {meal} {item_num} 10"}},
        {"type": "action", "action": {"type": "message", "label": "⌨️ 手動輸入數量", "text": f"手動輸入點餐 {meal} {item_num}"}}
    ]

    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": "請選擇或輸入所需數量：",
            "quickReply": {"items": quick_reply_items}
        }]
    }
    _send_line_payload(payload, os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))

def _set_user_state(service, sheet_id, user_id, state):
    """將使用者狀態寫入 user_states 工作表"""
    rows = _read_values(service, sheet_id, "user_states!A:B")
    found_idx = -1
    for i, r in enumerate(rows):
        if len(r) > 0 and r[0] == user_id:
            found_idx = i + 1
            break
    
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    if found_idx > -1:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"user_states!B{found_idx}:C{found_idx}",
            valueInputOption="RAW", body={"values": [[state, now_str]]}
        ).execute()
    else:
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="user_states!A:C",
            valueInputOption="RAW", body={"values": [[user_id, state, now_str]]}
        ).execute()

def _handle_manual_input_trigger(reply_token, user_id, text, service, sheet_id):
    # 使用 regex 精確抓取： "手動輸入點餐 DINNER 11"
    match = re.search(r"手動輸入點餐\s+(LUNCH|DINNER)\s+(\d+)", text)
    if not match:
        return
    
    meal = match.group(1)
    item_num = match.group(2)
    
    # 儲存狀態到 user_states 工作表
    _set_user_state(service, sheet_id, user_id, f"SET_QTY|{meal}|{item_num}")
    
    # 回覆訊息
    _reply_text(reply_token, f"🔢 請輸入欲訂購的數量：\n(例如直接輸入數字：7533967)")

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

    # 2. 撈取使用者的訂單
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
                    "qty": o[6],
                    "total": o[8]
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
                    {"type": "text", "text": f"{order['item']} x{order['qty']}", "weight": "bold", "size": "md", "wrap": True, "margin": "xs"},
                    {"type": "text", "text": f"共 ${order['total']}", "size": "md", "color": "#666666", "margin": "xs"}
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