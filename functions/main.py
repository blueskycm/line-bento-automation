from __future__ import annotations

import json
import os
import re
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any

from firebase_functions import https_fn
from flask import Response
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from webhook import line_webhook


# =========================
# Common helpers
# =========================

def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Max-Age": "3600",
    }


def _json_response(payload: dict, status: int = 200) -> Response:
    return Response(
        response=json.dumps(payload, ensure_ascii=False),
        status=status,
        mimetype="application/json",
        headers=_cors_headers(),
    )


def _parse_price(v: Any) -> int:
    s = str(v or "").strip()
    if not s:
        return 0
    m = re.search(r"(\d+)", s.replace(",", ""))
    return int(m.group(1)) if m else 0


def _get_sheets_service():
    base_dir = os.path.dirname(__file__)
    sa_path = os.path.join(base_dir, "serviceAccount.json")
    if not os.path.exists(sa_path):
        raise RuntimeError("Missing functions/serviceAccount.json")

    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _read_values(service, sheet_id: str, a1: str) -> list[list[Any]]:
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=a1)
        .execute()
        .get("values", [])
    )


# =========================
# 1) 讀取「常規菜單」
# Sheet 欄位：ID | 餐別 | 主菜類別 | 名稱 | 價錢 | del
# =========================
@https_fn.on_request(region="asia-east1", secrets=["SHEET_ID"])
def get_regular_menu(req: https_fn.Request) -> Response:
    if req.method == "OPTIONS":
        return Response(status=204, headers=_cors_headers())

    try:
        if req.method != "POST":
            return _json_response({"ok": False, "error": "Only POST is allowed"}, 405)

        body = req.get_json(silent=True) or {}
        meal = (body.get("meal") or "LUNCH").strip().upper()

        sheet_id = os.getenv("SHEET_ID") or ""
        regular_sheet_name = os.getenv("REGULAR_SHEET_NAME") or "常規菜單"
        if not sheet_id:
            return _json_response({"ok": False, "error": "Missing SHEET_ID env"}, 500)

        service = _get_sheets_service()

        # 👇 --- 讀取 LINE_SETTING 的廠商名稱 --- 👇
        vendor_name = "預設廠商"
        try:
            setting_rows = _read_values(service, sheet_id, "LINE_SETTING!A1:B1")
            # 確保有讀到資料，且長度足夠 (A1=標題, B1=值)
            if setting_rows and len(setting_rows) > 0 and len(setting_rows[0]) >= 2:
                vendor_name = str(setting_rows[0][1]).strip()
        except Exception as e:
            print(f"讀取 LINE_SETTING 失敗: {e}")
            pass
        # 👆 ------------------------------------------ 👆

        # 先抓 A:F（含表頭）
        rows = _read_values(service, sheet_id, f"{regular_sheet_name}!A:F")
        if not rows or len(rows) < 2:
            return _json_response({"ok": True, "items": [], "vendor": vendor_name}) # 記得加上 vendor

        data_rows = rows[1:]
        items: list[dict] = []
        for r in data_rows:
            r = (r + [""] * 6)[:6]
            item_id, r_meal, category, name, price, deleted = r

            if str(deleted).strip() not in ("", "0"):
                continue
            if str(r_meal).strip().upper() != meal:
                continue
            if not str(name).strip():
                continue

            items.append({
                "itemId": str(item_id).strip(),
                "meal": str(r_meal).strip().upper(),
                "category": str(category).strip(),
                "name": str(name).strip(),
                "price": _parse_price(price),
                "sort": len(items) + 1,
            })

        # 回傳時把 vendor 一併打包給前端
        return _json_response({"ok": True, "items": items, "vendor": vendor_name})

    except HttpError as e:
        content = ""
        try:
            content = e.content.decode("utf-8", errors="replace") if getattr(e, "content", None) else ""
        except Exception:
            content = repr(getattr(e, "content", ""))

        return _json_response(
            {
                "ok": False,
                "error_type": "HttpError",
                "status": getattr(e, "status_code", None),
                "reason": getattr(e, "reason", None),
                "content": content,
                "trace": traceback.format_exc(),
            },
            500,
        )
    except Exception as e:
        return _json_response(
            {"ok": False, "error_type": "Exception", "error": repr(e), "trace": traceback.format_exc()},
            500,
        )


# =========================
# 2) 儲存發布紀錄到 logs 分頁
# =========================
@https_fn.on_request(region="asia-east1", secrets=["SHEET_ID"])
def save_menu_log(req: https_fn.Request) -> Response:
    # 處理 CORS 預檢請求
    if req.method == "OPTIONS":
        return Response(status=204, headers=_cors_headers())

    try:
        if req.method != "POST":
            return _json_response({"ok": False, "error": "Only POST is allowed"}, 405)

        body = req.get_json(silent=True) or {}
        payload = body.get("payload", {})

        sheet_id = os.getenv("SHEET_ID") or ""
        # 這裡會讀取環境變數，若沒設定預設寫入 "logs" 分頁
        sheet_name = os.getenv("SHEET_NAME") or "logs" 
        
        if not sheet_id:
            return _json_response({"ok": False, "error": "Missing SHEET_ID env"}, 500)

        # 從 payload 提取發布者的資訊
        user_id = payload.get("createdByUserId", "UNKNOWN")
        user_name = payload.get("createdByName", "UNKNOWN")

        # 產生當下時間
        tw_tz = timezone(timedelta(hours=8))
        now_tw_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
        
        # 準備寫入的列資料：時間 | UserID | 暱稱 | 動作 | 詳細 JSON
        row = [now_tw_str, user_id, user_name, "PUBLISH_MENU", json.dumps(payload, ensure_ascii=False)]

        service = _get_sheets_service()
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!A:E",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        return _json_response({"ok": True})

    except Exception as e:
        return _json_response(
            {"ok": False, "error_type": "Exception", "error": repr(e), "trace": traceback.format_exc()},
            500,
        )