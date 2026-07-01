#!/usr/bin/env python3
"""
deploy_contentores.py
- Fetches data from Google Apps Script
- Detects changes (ETA, estado, novos contentores)
- Sends HTML email alerts to jpolho@fjmpc.pt
- Embeds data in HTML and deploys to Cloudflare Workers

Usage:
  python deploy_contentores.py          # Deploy se houver mudancas
  python deploy_contentores.py --force  # Forca deploy mesmo sem mudancas
"""

import requests, json, re, hashlib, os, sys, smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ══════════════════════════════════════════════════════
# CONFIGURACAO
# ══════════════════════════════════════════════════════

SHEET_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxM4ZnD7xZvBRa1jVmmVN8E4Qk-YlsFTnIv7I0LEoHY1-CRo7ax--FrtZzwEISk2up-4A/exec"
)

CF_ACCOUNT_ID  = os.environ.get("CF_ACCOUNT_ID",  "")
CF_API_TOKEN   = os.environ.get("CF_API_TOKEN",   "")
CF_SCRIPT_NAME = "contentores-fjmpc"

GMAIL_FROM     = os.environ.get("GMAIL_FROM",     "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
ALERT_TO       = "jpolho@fjmpc.pt"

APP_URL = "https://contentores-fjmpc.fjmpc.workers.dev/"

# ══════════════════════════════════════════════════════

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
HTML_FILE   = os.path.join(SCRIPT_DIR, "index.html")
HASH_FILE   = os.path.join(SCRIPT_DIR, ".last_deploy_hash")
STATE_FILE  = os.path.join(SCRIPT_DIR, ".last_state.json")
PLACEHOLDER = "/*__DATA__*/"

ESTADO_PT = {
    "transito":  "Em Transito",
    "alfandega": "Alfandega",
    "entregue":  "Entregue",
    "recebido":  "Recebido",
    "cancelado": "Cancelado",
    "pendente":  "Pendente",
}

ORANGE  = "#f15a29"
ORANGE2 = "#d44d1f"
DARK    = "#2d2d3f"
GRAY    = "#5a5a6a"

CARD_COLORS = {
    "new":    (ORANGE,   "#fff8f5", "Novo Contentor em Transito"),
    "eta":    ("#f59e0b", "#fffbeb", "Alteracao de ETA"),
    "estado": (ORANGE,   "#fff8f5", "Alteracao de Estado"),
}

# ── Helpers ───────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def fmt_date(d):
    if not d: return "-"
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        months = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        return f"{dt.day} {months[dt.month-1]} {dt.year}"
    except:
        return d

# ── Fetch ─────────────────────────────────────────────

def fetch_sheet_data():
    cb = "__deploy_cb__"
    url = f"{SHEET_URL}?action=all&callback={cb}&t={int(datetime.now().timestamp())}"
    log("A buscar dados do Apps Script...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    match = re.search(r'\((.+)\)\s*;?\s*$', resp.text, re.DOTALL)
    if not match:
        raise ValueError("Resposta JSONP invalida - verifica se o Apps Script esta publicado como 'Anyone'")
    data = json.loads(match.group(1))
    if not data.get("ok"):
        raise ValueError(f"Apps Script erro: {data.get('error', 'desconhecido')}")
    return data["data"]

# ── Hash & State ──────────────────────────────────────

def data_hash(data):
    return hashlib.md5(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def load_hash():
    try:
        with open(HASH_FILE) as f: return f.read().strip()
    except: return None

def save_hash(h):
    with open(HASH_FILE, "w") as f: f.write(h)

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f: return json.load(f)
    except: return None

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def build_state(data):
    state = {"containers": {}, "ids": []}
    for c in data.get("containers", []):
        state["containers"][c["id"]] = {
            "eta":        c.get("eta", ""),
            "etd":        c.get("etd", ""),
            "entrega":    c.get("entrega", ""),
            "estado":     c.get("estado", ""),
            "pod":        c.get("pod", ""),
            "destino":    c.get("destino", ""),
            "companhia":  c.get("companhia", ""),
            "fornecedor": c.get("fornecedor", ""),
            "mercadoria": c.get("mercadoria", ""),
            "tipo":       c.get("tipo", ""),
            "po":         c.get("po", ""),
            "obs":        c.get("obs", ""),
        }
        state["ids"].append(c["id"])
    return state

# ── Change detection ──────────────────────────────────

def detect_changes(old_state, new_state, artigos):
    changes = {"new": [], "eta": [], "estado": []}
    old_ids = set(old_state.get("ids", []))
    old_c = old_state.get("containers", {})
    new_c = new_state.get("containers", {})

    for cid in new_state["ids"]:
        items = (artigos.get(cid) or {}).get("items", [])
        c = new_c[cid]

        if cid not in old_ids:
            changes["new"].append({**c, "id": cid, "items": items})
            continue

        old = old_c.get(cid, {})

        eta_changed = (old.get("eta") != c.get("eta")) or (old.get("entrega") != c.get("entrega"))
        if eta_changed:
            changes["eta"].append({
                **c, "id": cid, "items": items,
                "eta_old":     old.get("eta", ""),
                "entrega_old": old.get("entrega", ""),
            })

        if old.get("estado") != c.get("estado"):
            changes["estado"].append({
                **c, "id": cid, "items": items,
                "estado_old": old.get("estado", ""),
            })

    return changes

# ── Email builder ─────────────────────────────────────

def _get_logo_b64():
    try:
        logo_path = os.path.join(SCRIPT_DIR, "fjmpc_2020 orange.png")
        if os.path.exists(logo_path):
            import base64
            with open(logo_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return None

def items_table_html(items, frete=None):
    if not items:
        return "<p style='color:#aaa;font-size:13px;margin:10px 0 0'>Sem artigos registados.</p>"
    visible = items[:60]

    total_merc = sum((i.get("qtd") or 0) * (i.get("custo") or 0) for i in items)
    total_geral = total_merc + (frete or 0)

    def fmt_usd(v):
        return f"${v:,.2f}" if v else "-"

    rows = "".join(
        f"<tr style='background:{'#fff' if idx % 2 == 0 else '#fdf5f2'}'>"
        f"<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:12px;"
        f"color:{ORANGE};font-family:monospace;font-weight:600'>{i.get('ref','')}</td>"
        f"<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#333'>{i.get('nome','')}</td>"
        f"<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#555;text-align:right'>{i.get('qtd','') or '-'}</td>"
        f"<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#555;text-align:right'>{fmt_usd(i.get('custo'))}</td>"
        f"<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#333;text-align:right;font-weight:600'>"
        f"{fmt_usd((i.get('qtd') or 0) * (i.get('custo') or 0))}</td>"
        f"</tr>"
        for idx, i in enumerate(visible)
    )
    extra = (
        f"<tr><td colspan='5' style='padding:8px 12px;font-size:12px;color:#aaa;border-top:1px solid #eee'>"
        f"... e mais {len(items)-60} artigos</td></tr>"
    ) if len(items) > 60 else ""

    total_row = (
        f"<tr style='background:#f8f8f8'>"
        f"<td colspan='4' style='padding:10px 12px;font-size:11px;font-weight:700;color:#888;"
        f"text-transform:uppercase;letter-spacing:0.5px;border-top:2px solid #eee'>Total Mercadoria</td>"
        f"<td style='padding:10px 12px;font-size:13px;font-weight:700;color:#333;"
        f"text-align:right;border-top:2px solid #eee'>{fmt_usd(total_merc)}</td></tr>"
    )
    frete_row = (
        f"<tr style='background:#f8f8f8'>"
        f"<td colspan='4' style='padding:8px 12px;font-size:11px;font-weight:700;color:#888;"
        f"text-transform:uppercase;letter-spacing:0.5px'>Frete (USD)</td>"
        f"<td style='padding:8px 12px;font-size:13px;color:#555;text-align:right'>{fmt_usd(frete)}</td></tr>"
    ) if frete else ""
    geral_row = (
        f"<tr style='background:{ORANGE}'>"
        f"<td colspan='4' style='padding:11px 12px;font-size:12px;font-weight:800;color:#fff;"
        f"text-transform:uppercase;letter-spacing:0.8px'>Total Geral USD</td>"
        f"<td style='padding:11px 12px;font-size:15px;font-weight:800;color:#fff;text-align:right'>{fmt_usd(total_geral)}</td></tr>"
    )

    return (
        f"<table style='width:100%;border-collapse:collapse;margin-top:16px;border-radius:8px;overflow:hidden'>"
        f"<thead><tr style='background:{ORANGE}'>"
        f"<th style='padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#fff;"
        f"text-transform:uppercase;letter-spacing:0.8px'>Referencia</th>"
        f"<th style='padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#fff;"
        f"text-transform:uppercase;letter-spacing:0.8px'>Produto</th>"
        f"<th style='padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#fff;"
        f"text-transform:uppercase;letter-spacing:0.8px'>Qtd.</th>"
        f"<th style='padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#fff;"
        f"text-transform:uppercase;letter-spacing:0.8px'>Custo Unit.</th>"
        f"<th style='padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#fff;"
        f"text-transform:uppercase;letter-spacing:0.8px'>Total USD</th>"
        f"</tr></thead>"
        f"<tbody>{rows}{extra}{total_row}{frete_row}{geral_row}</tbody>"
        f"</table>"
    )

def cont_meta(c):
    parts = []
    if c.get("fornecedor"): parts.append(f"<b>Fornecedor:</b> {c['fornecedor']}")
    if c.get("destino"):    parts.append(f"<b>Destino:</b> {c['destino']}")
    if c.get("pod"):        parts.append(f"<b>POD:</b> {c['pod']}")
    if c.get("mercadoria"): parts.append(f"<b>Mercadoria:</b> {c['mercadoria']}")
    if c.get("po"):         parts.append(f"<b>PO:</b> {c['po']}")
    if c.get("obs"):        parts.append(f"<b>Obs:</b> {c['obs']}")
    return "  &nbsp;|&nbsp;  ".join(parts)

def build_card(ctype, c):
    accent, bg, _ = CARD_COLORS[ctype]

    tipo_str = f" &middot; {c.get('tipo','')}" if c.get("tipo") else ""
    comp_str = (c.get("companhia") or "").upper()

    # Cabecalho: ID + companhia
    header = (
        f"<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:10px'>"
        f"<span style='font-size:20px;font-weight:800;color:{DARK};font-family:monospace;"
        f"letter-spacing:1.5px'>{c['id']}</span>"
        f"<span style='font-size:12px;color:#aaa;font-weight:600'>{comp_str}{tipo_str}</span>"
        f"</div>"
        f"<div style='font-size:13px;color:#666;line-height:1.9'>{cont_meta(c)}</div>"
    )

    # Detalhe por tipo
    detail = ""
    if ctype == "eta":
        lines = []
        if c.get("eta_old") != c.get("eta"):
            lines.append(
                f"<b style='color:#555'>ETA:</b> "
                f"<span style='color:#ccc;text-decoration:line-through'>{fmt_date(c.get('eta_old'))}</span>"
                f" &#8594; <span style='color:{accent};font-weight:700'>{fmt_date(c.get('eta'))}</span>"
            )
        if c.get("entrega_old") != c.get("entrega"):
            lines.append(
                f"<b style='color:#555'>Entrega:</b> "
                f"<span style='color:#ccc;text-decoration:line-through'>{fmt_date(c.get('entrega_old'))}</span>"
                f" &#8594; <span style='color:{accent};font-weight:700'>{fmt_date(c.get('entrega'))}</span>"
            )
        detail = (
            f"<div style='margin-top:14px;padding:12px 16px;background:#fff3e0;"
            f"border-radius:6px;border-left:4px solid {accent};font-size:14px;line-height:2.2'>"
            f"{'<br>'.join(lines)}</div>"
        )

    elif ctype == "estado":
        old_lbl = ESTADO_PT.get(c.get("estado_old",""), c.get("estado_old","?"))
        new_lbl = ESTADO_PT.get(c.get("estado",""), c.get("estado","?"))
        detail = (
            f"<div style='margin-top:14px;padding:12px 16px;background:#fff3e0;"
            f"border-radius:6px;border-left:4px solid {accent};font-size:14px'>"
            f"<b style='color:#555'>Estado:</b> "
            f"<span style='color:#ccc'>{old_lbl}</span>"
            f" &#8594; <span style='color:{accent};font-weight:700'>{new_lbl}</span>"
            f"</div>"
        )

    elif ctype == "new":
        chips = []
        if c.get("etd"):    chips.append(f"<b>ETD:</b> {fmt_date(c.get('etd'))}")
        if c.get("eta"):    chips.append(f"<b>ETA:</b> {fmt_date(c.get('eta'))}")
        if c.get("entrega"):chips.append(f"<b>Entrega:</b> {fmt_date(c.get('entrega'))}")
        est_str = ESTADO_PT.get(c.get("estado",""), c.get("estado","?"))
        chips.append(f"<b>Estado:</b> <span style='color:{accent};font-weight:700'>{est_str}</span>")
        detail = (
            f"<div style='margin-top:12px;font-size:13px;color:#555;line-height:2;"
            f"padding:10px 14px;background:#fff3e0;border-radius:6px;border-left:4px solid {accent}'>"
            f"{'  &nbsp;&middot;&nbsp;  '.join(chips)}"
            f"</div>"
        )

    frete = c.get("frete") or 0
    items_html = items_table_html(c.get("items", []), frete=frete if frete else None)

    return (
        f"<div style='background:#fff;border-radius:10px;margin-bottom:16px;"
        f"box-shadow:0 2px 10px rgba(0,0,0,0.07);overflow:hidden'>"
        f"<div style='padding:18px 20px 4px;border-left:5px solid {accent}'>"
        f"{header}{detail}"
        f"</div>"
        f"<div style='padding:0 20px 20px'>{items_html}</div>"
        f"</div>"
    )

def build_email_html(changes, ts):
    total = sum(len(v) for v in changes.values())
    sections_html = ""

    for ctype in ["new", "eta", "estado"]:
        items_list = changes.get(ctype, [])
        if not items_list:
            continue
        accent, _, label = CARD_COLORS[ctype]
        emoji = {"new": "&#128994;", "eta": "&#128197;", "estado": "&#128260;"}.get(ctype, "")
        cards = "".join(build_card(ctype, c) for c in items_list)
        sections_html += (
            f"<div style='padding:20px 24px;border-bottom:1px solid #eee'>"
            f"<div style='font-size:11px;font-weight:800;text-transform:uppercase;"
            f"letter-spacing:1px;color:{accent};margin-bottom:14px;"
            f"padding-bottom:8px;border-bottom:2px solid {accent}'>"
            f"{emoji}&nbsp; {label} <span style='background:{accent};color:#fff;"
            f"padding:2px 8px;border-radius:20px;font-size:10px;margin-left:6px'>{len(items_list)}</span>"
            f"</div>"
            f"{cards}</div>"
        )

    alt_word = "alteracao" if total == 1 else "alteracoes"
    logo_b64 = _get_logo_b64()
    logo_tag = (
        f'<img src="data:image/png;base64,{logo_b64}" alt="FJMPC" '
        f'style="height:48px;display:block;margin-bottom:10px" />'
        if logo_b64 else
        '<span style="color:#f15a29;font-size:26px;font-weight:900;letter-spacing:-1px">FJMPC</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Contentores FJMPC - Alertas</title>
</head>
<body style="margin:0;padding:24px 16px;background:#1e1e2e;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
  <div style="max-width:660px;margin:0 auto">

    <!-- Header -->
    <div style="background:#2d2d3f;border-radius:12px 12px 0 0;padding:24px 28px;border-bottom:3px solid #f15a29">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align:middle">
          {logo_tag}
          <div style="font-size:13px;color:#888;margin-top:2px;letter-spacing:0.3px">
            Gestão de Contentores 2026 &nbsp;&middot;&nbsp;
            <span style="color:#f15a29;font-weight:600">{total} {alt_word}</span>
          </div>
        </td>
        <td style="vertical-align:middle;text-align:right">
          <div style="font-size:11px;color:#555;line-height:1.6">{ts}</div>
        </td>
      </tr></table>
    </div>

    <!-- Body -->
    <div style="background:#f8f8fa;border-radius:0 0 12px 12px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,0.25)">
      {sections_html}

      <!-- CTA Footer -->
      <div style="padding:22px 28px;background:#2d2d3f;text-align:center;border-top:1px solid #3a3a4f">
        <a href="{APP_URL}"
           style="display:inline-block;background:#f15a29;color:#3a3a4f;text-decoration:none;
                  padding:12px 32px;border-radius:8px;font-size:14px;font-weight:800;
                  letter-spacing:0.5px">
          Abrir App &#8594;
        </a>
        <div style="margin-top:12px;font-size:11px;color:#555">
          Gerado automaticamente em {ts}
        </div>
      </div>
    </div>

  </div>
</body>
</html>"""

# ── Send email ────────────────────────────────────────

def send_email(changes):
    if not GMAIL_FROM or not GMAIL_APP_PASS:
        log("AVISO: Email nao configurado (GMAIL_FROM / GMAIL_APP_PASS em branco)")
        return

    ts = datetime.now().strftime("%d/%m/%Y %H:%M")
    total = sum(len(v) for v in changes.values())
    emoji_map = {"new": "Novo", "eta": "ETA", "estado": "Estado"}
    parts = [f"{len(changes[k])} {emoji_map[k]}" for k in ["new","eta","estado"] if changes.get(k)]
    subject = f"Contentores FJMPC - {' | '.join(parts)} ({ts})"

    html = build_email_html(changes, ts)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Contentores FJMPC <{GMAIL_FROM}>"
    msg["To"] = ALERT_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    log(f"A enviar email para {ALERT_TO}...")
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(GMAIL_FROM, GMAIL_APP_PASS)
        smtp.sendmail(GMAIL_FROM, ALERT_TO, msg.as_string())
    log("Emial enviado com sucesso")

# ── Cloudflare deploy ─────────────────────────────────

def build_html_with_data(data):
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    target = f"var __EMBEDDED__=null; {PLACEHOLDER}"
    if target not in html:
        raise ValueError("Placeholder nao encontrado em index.html")
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return html.replace(target, f"var __EMBEDDED__={data_json}; {PLACEHOLDER}", 1)

def deploy_to_cloudflare(html_content):
    html_js = json.dumps(html_content, ensure_ascii=False)
    worker_js = (
        f"const H={html_js};\n"
        "export default {\n"
        "  async fetch(req){\n"
        "    return new Response(H,{headers:{'Content-Type':'text/html;charset=utf-8'}});\n"
        "  }\n"
        "}"
    )
    size_kb = len(worker_js.encode()) / 1024
    log(f"Worker: {size_kb:.0f} KB")
    if size_kb > 900:
        log("AVISO: Proximo do limite de 1 MB da Cloudflare")

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{CF_ACCOUNT_ID}/workers/scripts/{CF_SCRIPT_NAME}"
    )
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    files = {
        "metadata": (None, json.dumps({"main_module": "worker.js"}), "application/json"),
        "worker.js": ("worker.js", worker_js.encode("utf-8"), "application/javascript+module"),
    }
    log("A fazer deploy para Cloudflare...")
    resp = requests.put(url, headers=headers, files=files, timeout=60)
    if not resp.ok:
        raise ValueError(f"Cloudflare API {resp.status_code}: {resp.text[:300]}")
    result = resp.json()
    if not result.get("success"):
        raise ValueError(f"Deploy falhou: {result.get('errors')}")
    log("Deploy concluido com sucesso")

# ── Main ──────────────────────────────────────────────

def main():
    force = "--force" in sys.argv
    print()
    print("=" * 55)
    print(f"  Contentores 2026 - Deploy  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 55)

    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        log("ERRO: Cloudflare nao configurado")
        sys.exit(1)

    try:
        # 1. Fetch
        data = fetch_sheet_data()
        containers = data.get("containers", [])
        artigos = data.get("artigos", {})
        total_items = sum(len(v.get("items", [])) for v in artigos.values())
        log(f"Dados: {len(containers)} contentores, {total_items} artigos")

        current_hash = data_hash(data)
        last_hash    = load_hash()

        if current_hash == last_hash and not force:
            log("Sem alteracoes. A saltar deploy.")
            print("=" * 55)
            print()
            return

        # 2. Detectar mudancas e enviar email
        old_state = load_state()
        new_state  = build_state(data)

        if old_state is not None:
            changes = detect_changes(old_state, new_state, artigos)
            total_ch = sum(len(v) for v in changes.values())
            if total_ch > 0:
                log(f"Alteracoes: {len(changes['new'])} novos | {len(changes['eta'])} ETA | {len(changes['estado'])} estado")
                send_email(changes)
            else:
                log("Dados alterados mas sem mudancas relevantes — sem email")
        else:
            log("Primeiro deploy — sem historico anterior (sem email)")

        # 3. Deploy Cloudflare
        log("A embutir dados no HTML...")
        html = build_html_with_data(data)
        deploy_to_cloudflare(html)

        # 4. Guardar estado
        save_hash(current_hash)
        save_state(new_state)

        log("Tudo concluido")
        print("=" * 55)
        print()

    except KeyboardInterrupt:
        print("\nInterrompido.")
        sys.exit(1)
    except Exception as e:
        log(f"ERRO: {e}")
        print("=" * 55)
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
