import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import liff from "@line/liff";
import "bootstrap/dist/css/bootstrap.min.css";

function todayStr() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export default function App() {
  const LIFF_ID = import.meta.env.VITE_LIFF_ID;
  const API_BASE = import.meta.env.VITE_API_BASE;

  const [liffReady, setLiffReady] = useState(false);
  const [profile, setProfile] = useState(null);
  const stickyRef = useRef(null);

  const [date, setDate] = useState(todayStr());
  const [meal, setMeal] = useState("LUNCH");
  const [vendor, setVendor] = useState("");
  const [deadlineTime, setDeadlineTime] = useState("10:00");
  const [note, setNote] = useState("");

  const [status, setStatus] = useState({ type: "secondary", text: "等待操作" });

  // 常規菜單狀態
  const [regularItems, setRegularItems] = useState([]);
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState(new Set());

  // 老闆的原始訊息
  const [rawMessage, setRawMessage] = useState("");

  // ==============================
  // 核心：解析老闆訊息的邏輯
  // ==============================
  const parsedItems = useMemo(() => {
    if (!rawMessage.trim()) return [];
    const items = [];

    // 移除常見冗言贅字
    let text = rawMessage.replace(/今天有/g, "").replace(/今天/g, "").replace(/謝謝/g, "").trim();
    if (!text) return [];

    if (meal === "LUNCH") {
      // --- 午餐解析邏輯 (固定 110 元) ---
      const parts = text.split(/[,，。\n]/).filter(x => x.trim());
      parts.forEach((part, i) => {
        if (part.includes("魚便當")) {
          // 直接擷取「魚便當」後面的所有字
          const content = part.replace("魚便當", "").trim();
          if (content) {
            items.push({ id: `T_L_F_${i}`, name: `炸魚便當（${content}）`, price: 110, category: "炸魚" });
          }
        } else if (part.includes("清蒸魚")) {
          // 直接擷取「清蒸魚是」或「清蒸魚」後面的所有字
          const content = part.replace("清蒸魚是", "").replace("清蒸魚", "").trim();
          if (content) {
            items.push({ id: `T_L_S_${i}`, name: `清蒸魚便當（${content}）`, price: 110, category: "清蒸魚" });
          }
        } else if (part.includes("合菜")) {
          // 直接擷取「合菜的肉是」或「合菜」後面的所有字
          const content = part.replace("合菜的肉是", "").replace("合菜", "").trim();
          if (content) {
            items.push({ id: `T_L_C_${i}`, name: `合菜便當（${content}）`, price: 110, category: "合菜" });
          }
        }
      });

    } else {
      // --- 晚餐解析邏輯 (品名 + 價格 + 備註) ---
      let remainingText = text;

      // 1. 特殊處理：水餃群組 => 水餃有高麗菜、韭菜（生的110、熟的60）
      const dumplingRegex = /水餃有([^（(]+)[(（]([^)）]+)[)）]/g;
      let dMatch;
      while ((dMatch = dumplingRegex.exec(remainingText)) !== null) {
        // 抓出口味 ["高麗菜", "韭菜"]
        const flavors = dMatch[1].split(/[、，,]/).map(x => x.trim()).filter(Boolean);
        // 抓出狀態 ["生的110", "熟的60"]
        const states = dMatch[2].split(/[、，,]/).map(x => x.trim()).filter(Boolean);

        flavors.forEach((flavor) => {
          states.forEach((state) => {
            const m = state.match(/(.+?)(\d+)/);
            if (m) {
              const stateName = m[1].replace("的", "").trim();
              const price = parseInt(m[2], 10);
              items.push({
                id: `T_D_DUMP_${flavor}_${stateName}`,
                name: `${flavor}水餃-${stateName}`,
                price: price,
                category: "水餃"
              });
            }
          });
        });
        // 處理完畢，把這串複雜的水餃文字從原句中拔除，以免干擾後續解析
        remainingText = remainingText.replace(dMatch[0], "");
      }

      // 2. 特殊處理：多種尺寸斜線價格 => 酸辣湯25/50/100 或 貢丸湯30/50
      const slashRegex = /([^，,。\n]+?)\s*(\d+(?:\/\d+)+)/g;
      let sMatch;
      while ((sMatch = slashRegex.exec(remainingText)) !== null) {
        const baseName = sMatch[1].trim();
        const prices = sMatch[2].split("/").map(Number); // ["25", "50", "100"]

        // 智慧判斷尺寸標籤：2個預設小/大，3個預設小/中/大
        let sizeLabels = ["小", "大"];
        if (prices.length === 3) sizeLabels = ["小", "中", "大"];
        if (prices.length > 3) sizeLabels = prices.map((_, i) => `尺寸${i + 1}`);

        prices.forEach((p, idx) => {
          items.push({
            id: `T_D_SIZE_${baseName}_${idx}`,
            name: `${baseName}-${sizeLabels[idx] || idx}`,
            price: p,
            category: "湯品"
          });
        });
        remainingText = remainingText.replace(sMatch[0], "");
      }

      // 3. 一般常規處理 (處理剩下的：百香柳橙乳酪吐司105，芋頭可頌60...)
      const parts = remainingText.split(/[,，。\n]/).filter(x => x.trim());
      parts.forEach((part, i) => {
        const p = part.trim();
        if (!p) return;

        // 匹配 名稱 + 價格 + (可選備註)
        const m = p.match(/^(.+?)(\d+)([(（].*[)）])?$/);
        if (m) {
          const nameBase = m[1].trim();
          const price = parseInt(m[2], 10);
          const extra = m[3] ? m[3].trim() : "";
          items.push({ id: `T_D_N_${i}`, name: nameBase + extra, price: price, category: "晚餐單點" });
        } else {
          // 如果真的遇到完全看不懂的格式，就標記為需確認，不阻斷執行
          items.push({ id: `T_D_ERR_${i}`, name: p, price: 0, category: "⚠️需確認" });
        }
      });
    }
    return items;
  }, [rawMessage, meal]);

  const filteredItems = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return regularItems;
    return regularItems.filter((it) => {
      const n = String(it.name || "").toLowerCase();
      const c = String(it.category || "").toLowerCase();
      return n.includes(q) || c.includes(q);
    });
  }, [regularItems, search]);

  const selectedCount = selectedIds.size + parsedItems.length;

  const setMsg = (text, type = "secondary") => setStatus({ text, type });

  async function initLiff() {
    try {
      await liff.init({ liffId: LIFF_ID });
      if (!liff.isLoggedIn()) {
        liff.login();
        return;
      }
      const p = await liff.getProfile();
      setProfile(p);
      setLiffReady(true);
    } catch (e) {
      setMsg(`LIFF 初始化失敗：${String(e)}`, "danger");
    }
  }

  async function fetchRegularMenu(nextMeal) {
    setMsg("正在讀取常規菜單...", "warning");
    setRawMessage("");
    try {
      const cleanBase = API_BASE.replace(/\/$/, "");
      const res = await fetch(`${cleanBase}/get_regular_menu`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ meal: nextMeal }),
      });

      const data = await res.json().catch(() => null);
      if (!res.ok || !data?.ok) throw new Error(data?.error || `HTTP ${res.status}`);

      const items = data.items || [];
      setRegularItems(items);
      setSelectedIds(new Set(items.map((x) => x.itemId)));

      // 👇 儲存從 Google Sheets 拿到的廠商名稱
      setVendor(data.vendor || "預設廠商");

      setMsg(`✅ 讀取完成：${items.length} 筆`, "success");
    } catch (e) {
      setRegularItems([]);
      setSelectedIds(new Set());
      setMsg(`讀取失敗：${String(e.message || e)}`, "danger");
    }
  }

  useEffect(() => {
    initLiff();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    fetchRegularMenu(meal);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meal]);

  useLayoutEffect(() => {
    function syncStickyHeight() {
      const el = stickyRef.current;
      if (!el) return;
      document.documentElement.style.setProperty("--sticky-h", `${el.offsetHeight + 20}px`);
    }
    const timer = setTimeout(syncStickyHeight, 100);
    window.addEventListener("resize", syncStickyHeight);
    return () => {
      window.removeEventListener("resize", syncStickyHeight);
      clearTimeout(timer);
    };
  }, [selectedCount]);

  function toggleOne(itemId) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  }

  async function onPublishClick() {
    if (!date || !deadlineTime) {
      setMsg("請填寫日期與截止時間", "danger");
      return;
    }
    if (selectedCount === 0) {
      setMsg("請至少選 1 項 (包含解析項目或常規菜單)", "danger");
      return;
    }

    const selectedRegular = regularItems.filter((x) => selectedIds.has(x.itemId));

    // 將解析結果轉換為寫入格式
    const formattedParsedItems = parsedItems.map((it, idx) => ({
      itemId: `T_${meal}_${date.replaceAll("-", "")}_${idx}`,
      name: it.name,
      price: Number(it.price || 0),
      sort: 9000 + idx,
      category: it.category || "動態解析",
    }));

    const finalItems = [
      ...selectedRegular.map((it) => ({
        itemId: it.itemId,
        name: it.name,
        price: Number(it.price || 0),
        sort: Number(it.sort || 0),
        category: it.category || "",
      })),
      ...formattedParsedItems,
    ];

    const [hours, minutes] = deadlineTime.split(':');
    const deadlineDate = new Date(`${date}T${hours}:${minutes}:00`);

    // 增加 1 分鐘緩衝 (60,000 毫秒)
    const bufferedDeadline = new Date(deadlineDate.getTime() + 60000);

    // 格式化回 YYYY-MM-DD HH:mm 格式給後端
    const finalDeadlineStr = `${date} ${String(bufferedDeadline.getHours()).padStart(2, '0')}:${String(bufferedDeadline.getMinutes()).padStart(2, '0')}`;

    const payload = {
      date,
      meal,
      vendor: vendor, // 從 Sheet 取得的 B1 值
      deadlineAt: finalDeadlineStr,
      note: note.trim(),
      createdByUserId: profile?.userId || "ADMIN",
      createdByName: profile?.displayName || "ADMIN",
      items: finalItems,
    };

    setMsg("正在寫入紀錄並準備發送...", "warning");

    try {
      const cleanBase = API_BASE.replace(/\/$/, "");
      const res = await fetch(`${cleanBase}/save_menu_log`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payload }),
      });

      if (!res.ok) throw new Error("寫入試算表失敗");
      // 👇 --- 發送到群組的訊息格式 --- 👇
      const mealTwName = meal === "LUNCH" ? "午餐" : "晚餐";
      const senderName = profile?.displayName || "管理員";
      const displayTime = deadlineTime;
      let msgText = `📢 【${senderName}】 ${mealTwName} 開團囉！\n📅 日期：${payload.date}\n⏰ 截止：${displayTime}\n`;
      if (payload.note) msgText += `📝 備註：${payload.note}\n`;
      msgText += `\n👇 提供品項 👇\n`;
      payload.items.forEach(it => { msgText += `- ${it.name} ($${it.price})\n`; });
      msgText += `\n請大家盡快點餐喔！`;

      // 使用 Share Target Picker 讓管理員選擇要發到哪些群組
      if (liff.isApiAvailable("shareTargetPicker")) {
        const shareRes = await liff.shareTargetPicker([{ type: "text", text: msgText }], { isMultiple: true });
        if (shareRes) {
          setMsg("✅ 已成功發送到選定的群組！", "success");
          setTimeout(() => liff.closeWindow(), 1500);
        } else {
          setMsg("✅ 紀錄已寫入 (您取消了發送群組)", "warning");
        }
      } else {
        setMsg("✅ 紀錄已寫入 (目前環境不支援選擇群組發送)", "success");
      }
    } catch (e) {
      setMsg(`發布發生錯誤：${e.message}`, "danger");
    }
  }

  return (
    <div className="container py-3">
      {/* 頂部標題與使用者頭像區塊 */}
      <div className="d-flex align-items-center justify-content-between mb-3 px-1">
        <div className="d-flex align-items-center">
          {profile?.pictureUrl ? (
            <img src={profile.pictureUrl} alt="avatar" className="rounded-circle me-2 shadow-sm" style={{ width: "48px", height: "48px", objectFit: "cover" }} />
          ) : (
            <div className="rounded-circle bg-secondary me-2 shadow-sm d-flex align-items-center justify-content-center text-white" style={{ width: "48px", height: "48px" }}>
              👤
            </div>
          )}
          <div>
            <div className="fs-5 fw-bold">{profile?.displayName || "載入中..."}</div>
            <div className="text-muted small" style={{ fontSize: "0.7rem" }}>⚙️ 管理員開單系統</div>
          </div>
        </div>
        <button className="btn btn-light border btn-sm rounded-pill fw-bold" onClick={() => fetchRegularMenu(meal)}>🔄 重整</button>
      </div>

      {/* 基本資料區塊 */}
      <div className="card shadow-sm border-0 mb-3">
        <div className="card-body row g-3">
          <div className="col-6">
            <label className="form-label fw-semibold">日期</label>
            <input
              type="date"
              className="form-control"
              style={{ fontSize: "0.8rem" }}
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </div>
          <div className="col-6">
            <label className="form-label fw-semibold">餐別</label>
            <select className="form-select text-primary fw-bold" value={meal} onChange={(e) => setMeal(e.target.value)}>
              <option value="LUNCH">☀️ 午餐</option>
              <option value="DINNER">🌙 晚餐</option>
            </select>
          </div>
          <div className="col-12">
            <label className="form-label fw-semibold">截止時間 (24H制)</label>
            <div className="position-relative">
              {/* 1. 底層的原生輸入框：將文字設為透明，但保留點擊彈出選擇器的功能 */}
              <input
                type="time"
                className="form-control fw-bold"
                style={{
                  color: 'transparent',
                  caretColor: 'transparent'
                }}
                value={deadlineTime}
                onChange={(e) => setDeadlineTime(e.target.value)}
              />

              {/* 2. 上層的自定義顯示：強制顯示變數中的 24 小時制數值 */}
              <div
                className="position-absolute top-50 start-0 translate-middle-y ps-3 pointer-events-none"
                style={{
                  pointerEvents: 'none', // 確保點擊時能穿透到下層的 input
                  fontSize: '1.1rem',
                  fontWeight: 'bold',
                  color: '#0d6efd' // 氣象署專業藍
                }}
              >
                {deadlineTime}
              </div>
            </div>
            {/* 推薦的提示寫法 */}
            <div className="mt-2 p-2 rounded-3 bg-light border-start border-primary border-4">
              <div className="d-flex align-items-center mb-1">
                <span className="me-2">🕒</span>
                <span className="fw-bold small">正式收單時間：{deadlineTime}</span>
              </div>
              <div className="text-muted" style={{ fontSize: "0.8rem", marginLeft: "1.5rem" }}>
                系統已自動配置 <strong>1 分鐘緩衝期</strong>，以避免網路延遲造成訂餐失敗。
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 🚀 自動解析區塊 */}
      <div className="card shadow-sm border-0 mb-3" style={{ background: "#eef2ff" }}>
        <div className="card-body">
          <div className="fw-bold fs-5 mb-2 text-primary">🤖 貼上訊息自動解析</div>
          <textarea
            className="form-control mb-3"
            rows="3"
            placeholder={meal === "LUNCH" ? "貼上老闆午餐訊息（例：今天魚便當秋刀魚...）" : "貼上老闆晚餐訊息（例：綜合豆70...）"}
            value={rawMessage}
            onChange={(e) => setRawMessage(e.target.value)}
          />

          {/* 解析結果預覽 */}
          {parsedItems.length > 0 && (
            <div className="bg-white rounded p-3 border">
              <div className="small text-muted mb-2 fw-bold">解析預覽 ({parsedItems.length} 筆)：</div>
              <div className="vstack gap-1">
                {parsedItems.map(it => (
                  <div key={it.id} className="d-flex justify-content-between border-bottom pb-1">
                    <span>
                      {it.category === "⚠️需確認" ? "⚠️ " : "✨ "}
                      {it.name}
                    </span>
                    <strong className={it.price === 0 ? "text-danger" : "text-success"}>
                      ${it.price}
                    </strong>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 常規菜單區塊 */}
      <div className="card shadow-sm border-0">
        <div className="card-body p-0">
          <div className="p-3 border-bottom bg-light">
            <div className="fw-bold fs-5">📋 常規菜單 ({regularItems.length} 筆)</div>
          </div>
          <div className="list-group list-group-flush">
            {filteredItems.map((it) => (
              <label className="list-group-item d-flex align-items-center" key={it.itemId} style={{ cursor: "pointer" }}>
                <input className="form-check-input me-3" type="checkbox" checked={selectedIds.has(it.itemId)} onChange={() => toggleOne(it.itemId)} />
                <div className="flex-grow-1 fw-bold">{it.name}</div>
                <span className="badge text-bg-secondary rounded-pill">${Number(it.price || 0)}</span>
              </label>
            ))}
          </div>
          <div className={`alert alert-${status.type} m-3 mb-3 text-center`}>{status.text}</div>
        </div>
      </div>

      {/* 加入一個隱形墊片，高度綁定按鈕的高度 */}
      <div style={{ height: "var(--sticky-h, 150px)", width: "100%", flexShrink: 0 }}></div>

      {/* 底部固定按鈕 */}
      <div ref={stickyRef} className="sticky-bar">
        <button
          className={`btn w-100 py-3 ${selectedCount > 0 && deadlineTime ? "btn-primary" : "btn-secondary"}`}
          disabled={!(selectedCount > 0 && deadlineTime)}
          onClick={onPublishClick}
        >
          🚀 發布菜單到群組 (共 {selectedCount} 項)
        </button>
      </div>
    </div>
  );
}