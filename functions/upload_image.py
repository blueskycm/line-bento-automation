import urllib.request
import os
from dotenv import load_dotenv

# 1. 設定區
load_dotenv()
TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
# 填入你剛才拿到的：richmenu-ID
RICH_MENU_ID = "richmenu-0661c63130d18fb63b40b6db5a1fddad" 
# 圖片檔案路徑 (建議 2500x1686)
IMAGE_PATH = "admin_menu2.jpg" 

def upload_rich_menu_image():
    # 注意：上傳檔案的 URL 是 api-data 開頭
    url = f"https://api-data.line.me/v2/bot/richmenu/{RICH_MENU_ID}/content"
    
    try:
        with open(IMAGE_PATH, "rb") as img_file:
            img_data = img_file.read()
            
            req = urllib.request.Request(url, data=img_data, method="POST")
            req.add_header("Authorization", f"Bearer {TOKEN}")
            # 如果是 png 用 image/png，如果是 jpg 用 image/jpeg
            req.add_header("Content-Type", "image/jpeg") 
            
            with urllib.request.urlopen(req) as f:
                print(f"✅ 圖片上傳成功！")
                print(f"現在你可以將 ID {RICH_MENU_ID} 綁定給使用者了。")
                
    except FileNotFoundError:
        print(f"❌ 找不到檔案：{IMAGE_PATH}，請檢查檔案名稱與路徑。")
    except Exception as e:
        print(f"❌ 上傳發生錯誤: {e}")

if __name__ == "__main__":
    upload_rich_menu_image()