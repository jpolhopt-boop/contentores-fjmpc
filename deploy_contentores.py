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

APP_URL = "https://contentores-fjmpc.contentores-fjmpc.workers.dev"

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

CARD_COLORS = {
    "new":    ("#22c55e", "#f0fdf4", "Novo Contentor"),
    "eta":    ("#f59e0b", "#fffbeb", "Alteracao de ETA"),
    "estado": ("#3b82f6", "#eff6ff", "Alteracao de Estado"),
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

def items_table_html(items):
    if not items:
        return "<p style='color:#aaa;font-size:13px;margin:10px 0 0'>Sem artigos registados.</p>"
    visible = items[:60]
    rows = "".join(
        f"<tr>"
        f"<td style='padding:7px 10px;border-top:1px solid #eee;font-size:13px;color:#444;font-family:monospace'>{i.get('ref','')}</td>"
        f"<td style='padding:7px 10px;border-top:1px solid #eee;font-size:13px;color:#444'>{i.get('nome','')}</td>"
        f"<td style='padding:7px 10px;border-top:1px solid #eee;font-size:13px;color:#555;text-align:right'>{i.get('qtd','')}</td>"
        f"<td style='padding:7px 10px;border-top:1px solid #eee;font-size:13px;color:#555;text-align:right'>{i.get('custo','') or '-'}</td>"
        f"</tr>"
        for i in visible
    )
    extra = (
        f"<tr><td colspan='4' style='padding:8px 10px;font-size:12px;color:#aaa;border-top:1px solid #eee'>"
        f"... e mais {len(items)-60} artigos</td></tr>"
    ) if len(items) > 60 else ""
    return (
        f"<table style='width:100%;border-collapse:collapse;margin-top:14px'>"
        f"<thead><tr style='background:#f5f5f5'>"
        f"<th style='padding:8px 10px;text-align:left;font-size:11px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.5px'>Ref.</th>"
        f"<th style='padding:8px 10px;text-align:left;font-size:11px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.5px'>Nome</th>"
        f"<th style='padding:8px 10px;text-align:right;font-size:11px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.5px'>Qtd</th>"
        f"<th style='padding:8px 10px;text-align:right;font-size:11px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.5px'>Custo</th>"
        f"</tr></thead>"
        f"<tbody>{rows}{extra}</tbody>"
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

    # Cabecalho contentor
    tipo_str = f" &middot; {c.get('tipo','')}" if c.get("tipo") else ""
    comp_str = (c.get("companhia") or "").upper()
    header = (
        f"<div style='margin-bottom:8px'>"
        f"<span style='font-size:19px;font-weight:700;color:#1a3a5c;font-family:monospace;letter-spacing:1px'>{c['id']}</span>"
        f"<span style='font-size:13px;color:#aaa;margin-left:10px'>{comp_str}{tipo_str}</span>"
        f"</div>"
        f"<div style='font-size:13px;color:#666;line-height:1.8'>{cont_meta(c)}</div>"
    )

    # Conteudo especifico por tipo
    detail = ""
    if ctype == "eta":
        lines = []
        if c.get("eta_old") != c.get("eta"):
            lines.append(
                f"<b>ETA:</b> "
                f"<span style='color:#bbb;text-decoration:line-through'>{fmt_date(c.get('eta_old'))}</span>"
                f" &rarr; <span style='color:{accent};font-weight:700'>{fmt_date(c.get('eta'))}</span>"
            )
        if c.get("entrega_old") != c.get("entrega"):
            lines.append(
                f"<b>Entrega:</b> "
                f"<span style='color:#bbb;text-decoration:line-through'>{fmt_date(c.get('entrega_old'))}</span>"
                f" &rarr; <span style='color:{accent};font-weight:700'>{fmt_date(c.get('entrega'))}</span>"
            )
        detail = f"<div style='margin-top:12px;font-size:14px;line-height:2'>{'<br>'.join(lines)}</div>"

    elif ctype == "estado":
        old_lbl = ESTADO_PT.get(c.get("estado_old",""), c.get("estado_old","?"))
        new_lbl = ESTADO_PT.get(c.get("estado",""), c.get("estado","?"))
        detail = (
            f"<div style='margin-top:12px;font-size:14px'>"
            f"<b>Estado:</b> "
            f"<span style='color:#bbb'>{old_lbl}</span>"
            f" &rarr; <span style='color:{accent};font-weight:700'>{new_lbl}</span>"
            f"</div>"
        )

    elif ctype == "new":
        etd_str = fmt_date(c.get("etd"))
        eta_str = fmt_date(c.get("eta"))
        ent_str = fmt_date(c.get("entrega"))
        est_str = ESTADO_PT.get(c.get("estado",""), c.get("estado","?"))
        detail = (
            f"<div style='margin-top:12px;font-size:13px;color:#555;line-height:2'>"
            f"<b>ETD:</b> {etd_str} &nbsp;|&nbsp; "
            f"<b>ETA:</b> {eta_str} &nbsp;|&nbsp; "
            f"<b>Entrega:</b> {ent_str} &nbsp;|&nbsp; "
            f"<b>Estado:</b> <span style='color:{accent};font-weight:700'>{est_str}</span>"
            f"</div>"
        )

    items_html = items_table_html(c.get("items", []))

    return (
        f"<div style='border-radius:8px;padding:20px 22px;margin-bottom:14px;"
        f"background:{bg};border-left:5px solid {accent}'>"
        f"{header}{detail}{items_html}"
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
            f"<div style='padding:24px 28px;border-bottom:1px solid #eee'>"
            f"<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
            f"letter-spacing:0.8px;color:{accent};margin-bottom:16px'>"
            f"{emoji} {label} ({len(items_list)})</div>"
            f"{cards}</div>"
        )

    alt_word = "alteracao" if total == 1 else "alteracoes"
    return f"""<!DOCTYPE html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Contentores 2026 - Alertas</title>
</head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
  <div style="max-width:680px;margin:0 auto">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1a3a5c 0%,#0d2035 100%);border-radius:12px 12px 0 0;padding:28px 32px">
      <div style="font-size:24px;font-weight:800;color:white;margin-bottom:4px">
        &#128674; Contentores 2026
      </div>
      <div style="font-size:14px;color:rgba(255,255,255,0.55)">
        FJMPC &nbsp;&middot;&nbsp; {total} {alt_word} detectada{'s' if total > 1 else ''}
      </div>
    </div>

    <!-- Body -->
    <div style="background:white;border-radius:0 0 12px 12px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,0.08)">
      {sections_html}

      <!-- CTA Footer -->
      <div style="padding:24px 28px;background:#fafafa;text-align:center">
        <a href="{APP_URL}"
           style="display:inline-block;background:#1a3a5c;color:white;text-decoration:none;
                  padding:12px 28px;border-radius:8px;font-size:14px;font-weight:700;
                  letter-spacing:0.3px">
          Abrir App &rarr;
        </a>
        <div style="margin-top:14px;font-size:11px;color:#ccc">
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
