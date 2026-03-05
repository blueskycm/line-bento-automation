import os
from dotenv import load_dotenv
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi

# 1. 初始化設定
load_dotenv()
token = os.getenv("CHANNEL_ACCESS_TOKEN")
configuration = Configuration(host="https://api.line.me", access_token=token)

def main():
    if not token:
        print("❌ 錯誤：請確保 .env 中有 CHANNEL_ACCESS_TOKEN")
        return

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        try:
            print("\n" + "="*40)
            print("📜 LINE 圖文選單管理工具")
            print("="*40)

            # --- 步驟 A: 列出 Token 擁有的選單 ---
            rich_menu_list_response = line_bot_api.get_rich_menu_list()
            owned_menus = rich_menu_list_response.richmenus
            
            print(f"\n[1] 你的 Token 擁有的選單數量: {len(owned_menus)}")
            for rm in owned_menus:
                print(f"    - 名稱: {rm.name}")
                print(f"      ID  : {rm.rich_menu_id}")
                print("-" * 30)

            # --- 步驟 B: 查詢全域預設選單 (一般使用者看到什麼) ---
            print("\n[2] 查詢『全域預設』狀態 (一般使用者):")
            try:
                default_res = line_bot_api.get_default_rich_menu_id()
                print(f"    ✅ 目前預設 ID: {default_res.rich_menu_id}")
            except Exception as e:
                if "404" in str(e):
                    print("    ⚠️ 目前沒有設定任何預設選單。")
                elif "403" in str(e):
                    print("    🚫 權限不足：目前的預設選單可能是在 LINE 後台手動建立的。")
                else:
                    print(f"    ❌ 查詢失敗: {e}")

            print("\n" + "="*40)
            print("💡 提示：若要強制變更預設選單，請執行 line_bot_api.set_default_rich_menu()")
            print("="*40 + "\n")

        except Exception as e:
            print(f"💥 程式執行發生嚴重錯誤: {e}")

if __name__ == "__main__":
    main()