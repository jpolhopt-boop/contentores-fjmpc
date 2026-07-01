#!/usr/bin/env python3
"""
deploy_contentores.py
- Fetches data from Google Apps Script
- Detects changes (ETA, estado, novos contentores, sheet externa cols B/D/H)
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

# ======================================================
# CONFIGURACAO
# ======================================================

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

ORANGE = "#f15a29"
DARK   = "#2d2d3f"

CARD_COLORS = {
    "new":      (ORANGE,    "#fff8f5", "Novo Contentor"),
    "removed":  ("#ef4444", "#fff5f5", "Contentor Removido"),
    "eta":      ("#f59e0b", "#fffbeb", "Alteracao de ETA"),
    "companhia":(ORANGE,    "#fff8f5", "Alteracao de Companhia"),
    "outros":   ("#8b5cf6", "#f5f3ff", "Outros Dados Alterados"),
    "ext":      ("#0ea5e9", "#f0f9ff", "Alteracao Sheet Tracking (Col B/D/H)"),
}

# -- Helpers --

def log(msg):
    print("[" + datetime.now().strftime("%H:%M:%S") + "] " + msg)

def fmt_date(d):
    if not d: return "-"
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        months = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        return str(dt.day) + " " + months[dt.month-1] + " " + str(dt.year)
    except:
        return str(d)

# -- Fetch --

def fetch_sheet_data():
    cb  = "__deploy_cb__"
    url = SHEET_URL + "?action=all&callback=" + cb + "&t=" + str(int(datetime.now().timestamp()))
    log("A buscar dados do Apps Script...")
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    match = re.search(r'\((.+)\)\s*;?\s*$', resp.text, re.DOTALL)
    if not match:
        raise ValueError("Resposta JSONP invalida")
    data = json.loads(match.group(1))
    if not data.get("ok"):
        raise ValueError("Apps Script erro: " + data.get("error","desconhecido"))
    return data["data"]

# -- Hash & State --

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
    state = {"containers": {}, "ids": [], "ext_rows": {}}
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
    for r in data.get("ext_rows", []):
        key = str(r.get("row", ""))
        state["ext_rows"][key] = {
            "id":   r.get("id", ""),
            "seal": r.get("seal", ""),
            "eta":  r.get("eta", ""),
        }
    return state

# -- Change detection --

def detect_changes(old_state, new_state, artigos):
    changes = {"new": [], "removed": [], "eta": [], "companhia": [], "outros": [], "ext": []}
    old_ids = set(old_state.get("ids", []))
    new_ids = set(new_state.get("ids", []))
    old_c   = old_state.get("containers", {})
    new_c   = new_state.get("containers", {})

    for cid in old_ids - new_ids:
        changes["removed"].append(dict(list(old_c.get(cid, {}).items()) + [("id", cid), ("items", [])]))

    for cid in new_state["ids"]:
        items = (artigos.get(cid) or {}).get("items", [])
        c     = new_c[cid]

        if cid not in old_ids:
            entry = dict(list(c.items()) + [("id", cid), ("items", items)])
            changes["new"].append(entry)
            continue

        old = old_c.get(cid, {})

        eta_changed = (old.get("eta") != c.get("eta")) or (old.get("entrega") != c.get("entrega"))
        if eta_changed:
            entry = dict(list(c.items()) + [
                ("id", cid), ("items", items),
                ("eta_old", old.get("eta", "")),
                ("entrega_old", old.get("entrega", "")),
            ])
            changes["eta"].append(entry)

        if old.get("companhia") != c.get("companhia"):
            entry = dict(list(c.items()) + [
                ("id", cid), ("items", items),
                ("companhia_old", old.get("companhia", "")),
            ])
            changes["companhia"].append(entry)

        outros_fields = ["fornecedor", "destino", "pod", "po", "obs", "tipo", "mercadoria"]
        diffs = {f: (old.get(f,""), c.get(f,"")) for f in outros_fields if old.get(f,"") != c.get(f,"")}
        if diffs and not eta_changed:
            entry = dict(list(c.items()) + [("id", cid), ("items", items), ("diffs", diffs)])
            changes["outros"].append(entry)

        if old.get("estado") != c.get("estado") and not eta_changed:
            entry = dict(list(c.items()) + [
                ("id", cid), ("items", items),
                ("diffs", {"estado": (old.get("estado",""), c.get("estado",""))}),
            ])
            changes["outros"].append(entry)

    old_ext = old_state.get("ext_rows", {})
    new_ext = new_state.get("ext_rows", {})
    for key in sorted(set(old_ext) | set(new_ext), key=lambda x: int(x) if x.isdigit() else 0):
        o = old_ext.get(key, {})
        n = new_ext.get(key, {})
        if o == n:
            continue
        diffs_ext = {f: (o.get(f,""), n.get(f,"")) for f in ["id","seal","eta"] if o.get(f,"") != n.get(f,"")}
        if diffs_ext:
            changes["ext"].append({
                "row":   key,
                "id":    n.get("id") or o.get("id") or "Linha " + key,
                "diffs": diffs_ext,
            })

    return changes

# -- Email builder --

def _get_logo_b64():
    try:
        import base64
        p = os.path.join(SCRIPT_DIR, "fjmpc_2020 orange.png")
        if os.path.exists(p):
            with open(p, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return None

def fusd(v):
    return "${:,.2f}".format(v) if v else "-"

def items_table_html(items, frete=None):
    if not items:
        return "<p style='color:#aaa;font-size:13px;margin:10px 0 0'>Sem artigos registados.</p>"
    visible     = items[:60]
    total_merc  = sum((i.get("qtd") or 0) * (i.get("custo") or 0) for i in items)
    total_geral = total_merc + (frete or 0)

    row_parts = []
    for idx, i in enumerate(visible):
        bg = "#fff" if idx % 2 == 0 else "#fdf5f2"
        row_parts.append(
            "<tr style='background:" + bg + "'>"
            "<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:12px;color:" + ORANGE + ";font-family:monospace;font-weight:600'>" + str(i.get("ref","")) + "</td>"
            "<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#333'>" + str(i.get("nome","")) + "</td>"
            "<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#555;text-align:right'>" + str(i.get("qtd","") or "-") + "</td>"
            "<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;color:#555;text-align:right'>" + fusd(i.get("custo")) + "</td>"
            "<td style='padding:9px 12px;border-top:1px solid #f0e8e4;font-size:13px;font-weight:600;text-align:right'>" + fusd((i.get("qtd") or 0)*(i.get("custo") or 0)) + "</td>"
            "</tr>"
        )
    rows = "".join(row_parts)
    extra     = ("<tr><td colspan='5' style='padding:8px 12px;font-size:12px;color:#aaa;border-top:1px solid #eee'>... e mais " + str(len(items)-60) + " artigos</td></tr>") if len(items) > 60 else ""
    total_row = "<tr style='background:#f8f8f8'><td colspan='4' style='padding:10px 12px;font-size:11px;font-weight:700;color:#888;text-transform:uppercase;border-top:2px solid #eee'>Total Mercadoria</td><td style='padding:10px 12px;font-size:13px;font-weight:700;text-align:right;border-top:2px solid #eee'>" + fusd(total_merc) + "</td></tr>"
    frete_row = ("<tr style='background:#f8f8f8'><td colspan='4' style='padding:8px 12px;font-size:11px;font-weight:700;color:#888;text-transform:uppercase'>Frete (USD)</td><td style='padding:8px 12px;font-size:13px;color:#555;text-align:right'>" + fusd(frete) + "</td></tr>") if frete else ""
    geral_row = "<tr style='background:" + ORANGE + "'><td colspan='4' style='padding:11px 12px;font-size:12px;font-weight:800;color:#fff;text-transform:uppercase;letter-spacing:0.8px'>Total Geral USD</td><td style='padding:11px 12px;font-size:15px;font-weight:800;color:#fff;text-align:right'>" + fusd(total_geral) + "</td></tr>"

    return (
        "<table style='width:100%;border-collapse:collapse;margin-top:16px;border-radius:8px;overflow:hidden'>"
        "<thead><tr style='background:" + ORANGE + "'>"
        "<th style='padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#fff;text-transform:uppercase;letter-spacing:0.8px'>Referencia</th>"
        "<th style='padding:10px 12px;text-align:left;font-size:11px;font-weight:700;color:#fff;text-transform:uppercase;letter-spacing:0.8px'>Produto</th>"
        "<th style='padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#fff;text-transform:uppercase;letter-spacing:0.8px'>Qtd.</th>"
        "<th style='padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#fff;text-transform:uppercase;letter-spacing:0.8px'>Custo Unit.</th>"
        "<th style='padding:10px 12px;text-align:right;font-size:11px;font-weight:700;color:#fff;text-transform:uppercase;letter-spacing:0.8px'>Total USD</th>"
        "</tr></thead>"
        "<tbody>" + rows + extra + total_row + frete_row + geral_row + "</tbody>"
        "</table>"
    )

def cont_meta(c):
    parts = []
    if c.get("fornecedor"): parts.append("<b>Fornecedor:</b> " + c["fornecedor"])
    if c.get("destino"):    parts.append("<b>Destino:</b> " + c["destino"])
    if c.get("pod"):        parts.append("<b>POD:</b> " + c["pod"])
    if c.get("mercadoria"): parts.append("<b>Mercadoria:</b> " + c["mercadoria"])
    if c.get("po"):         parts.append("<b>PO:</b> " + c["po"])
    if c.get("obs"):        parts.append("<b>Obs:</b> " + c["obs"])
    return "  &nbsp;|&nbsp;  ".join(parts)

def build_card(ctype, c):
    accent, bg, _ = CARD_COLORS.get(ctype, (ORANGE, "#fff8f5", ""))

    if ctype == "ext":
        field_labels = {"id": "Col B - Contentor", "seal": "Col D - SEAL", "eta": "Col H - ETA"}
        rows_parts = []
        for f, (ov, nv) in c.get("diffs", {}).items():
            rows_parts.append(
                "<tr>"
                "<td style='padding:8px 12px;font-size:12px;color:#666;font-weight:600'>" + field_labels.get(f, f) + "</td>"
                "<td style='padding:8px 12px;font-family:monospace;color:#aaa;text-decoration:line-through;font-size:12px'>" + (ov or "-") + "</td>"
                "<td style='padding:8px 12px;font-family:monospace;font-weight:700;color:" + accent + ";font-size:12px'>" + (nv or "-") + "</td>"
                "</tr>"
            )
        rows_html = "".join(rows_parts)
        return (
            "<div style='background:#fff;border-radius:10px;margin-bottom:12px;box-shadow:0 2px 8px rgba(0,0,0,0.06);overflow:hidden;border-left:5px solid " + accent + "'>"
            "<div style='padding:14px 18px'>"
            "<span style='font-size:16px;font-weight:800;color:" + DARK + ";font-family:monospace'>" + str(c.get("id","")) + "</span>"
            "<span style='font-size:11px;color:#aaa;margin-left:8px'>Linha " + str(c.get("row","")) + "</span>"
            "<table style='width:100%;border-collapse:collapse;margin-top:10px'>"
            "<tr style='background:#f8f8fa'>"
            "<th style='padding:7px 12px;font-size:10px;color:#aaa;text-align:left;text-transform:uppercase'>Campo</th>"
            "<th style='padding:7px 12px;font-size:10px;color:#aaa;text-align:left;text-transform:uppercase'>Antes</th>"
            "<th style='padding:7px 12px;font-size:10px;color:#aaa;text-align:left;text-transform:uppercase'>Agora</th>"
            "</tr>"
            + rows_html +
            "</table></div></div>"
        )

    tipo_str = " &middot; " + str(c.get("tipo","")) if c.get("tipo") else ""
    comp_str = (c.get("companhia") or "").upper()
    header = (
        "<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:10px'>"
        "<span style='font-size:20px;font-weight:800;color:" + DARK + ";font-family:monospace;letter-spacing:1.5px'>" + str(c.get("id","")) + "</span>"
        "<span style='font-size:12px;color:#aaa;font-weight:600'>" + comp_str + tipo_str + "</span></div>"
        "<div style='font-size:13px;color:#666;line-height:1.9'>" + cont_meta(c) + "</div>"
    )

    detail = ""
    if ctype == "eta":
        lines = []
        if c.get("eta_old") != c.get("eta"):
            lines.append("<b style='color:#555'>ETA:</b> <span style='color:#ccc;text-decoration:line-through'>" + fmt_date(c.get("eta_old")) + "</span> &#8594; <span style='color:" + accent + ";font-weight:700'>" + fmt_date(c.get("eta")) + "</span>")
        if c.get("entrega_old") != c.get("entrega"):
            lines.append("<b style='color:#555'>Entrega:</b> <span style='color:#ccc;text-decoration:line-through'>" + fmt_date(c.get("entrega_old")) + "</span> &#8594; <span style='color:" + accent + ";font-weight:700'>" + fmt_date(c.get("entrega")) + "</span>")
        detail = "<div style='margin-top:14px;padding:12px 16px;background:#fff3e0;border-radius:6px;border-left:4px solid " + accent + ";font-size:14px;line-height:2.2'>" + "<br>".join(lines) + "</div>"

    elif ctype == "companhia":
        detail = (
            "<div style='margin-top:14px;padding:12px 16px;background:#fff3e0;border-radius:6px;border-left:4px solid " + accent + ";font-size:14px'>"
            "<b style='color:#555'>Companhia:</b> <span style='color:#ccc;text-decoration:line-through'>" + str(c.get("companhia_old","")) + "</span>"
            " &#8594; <span style='color:" + accent + ";font-weight:700'>" + str(c.get("companhia","")) + "</span></div>"
        )

    elif ctype == "new":
        chips = []
        if c.get("etd"):     chips.append("<b>ETD:</b> " + fmt_date(c.get("etd")))
        if c.get("eta"):     chips.append("<b>ETA:</b> " + fmt_date(c.get("eta")))
        if c.get("entrega"): chips.append("<b>Entrega:</b> " + fmt_date(c.get("entrega")))
        est = ESTADO_PT.get(c.get("estado",""), c.get("estado","?"))
        chips.append("<b>Estado:</b> <span style='color:" + accent + ";font-weight:700'>" + est + "</span>")
        detail = (
            "<div style='margin-top:12px;font-size:13px;color:#555;line-height:2;"
            "padding:10px 14px;background:#fff3e0;border-radius:6px;border-left:4px solid " + accent + "'>"
            + "  &nbsp;&middot;&nbsp;  ".join(chips) + "</div>"
        )

    elif ctype == "outros":
        field_lbl = {"fornecedor":"Fornecedor","destino":"Destino","pod":"POD","po":"PO",
                     "obs":"Obs","tipo":"Tipo","mercadoria":"Mercadoria","estado":"Estado"}
        lines = [
            "<b style='color:#555'>" + field_lbl.get(f, f) + ":</b> <span style='color:#ccc;text-decoration:line-through'>" + (ov or "-") + "</span> &#8594; <span style='color:" + accent + ";font-weight:700'>" + (nv or "-") + "</span>"
            for f, (ov, nv) in c.get("diffs", {}).items()
        ]
        detail = "<div style='margin-top:14px;padding:12px 16px;background:#f5f3ff;border-radius:6px;border-left:4px solid " + accent + ";font-size:13px;line-height:2.2'>" + "<br>".join(lines) + "</div>"

    elif ctype == "removed":
        detail = "<div style='margin-top:12px;padding:10px 14px;background:#fff5f5;border-radius:6px;border-left:4px solid " + accent + ";font-size:13px;color:#888'>Contentor removido da lista de tracking.</div>"

    frete      = c.get("frete") or 0
    items_html = items_table_html(c.get("items", []), frete=frete if frete else None)

    return (
        "<div style='background:#fff;border-radius:10px;margin-bottom:16px;box-shadow:0 2px 10px rgba(0,0,0,0.07);overflow:hidden'>"
        "<div style='padding:18px 20px 4px;border-left:5px solid " + accent + "'>" + header + detail + "</div>"
        "<div style='padding:0 20px 20px'>" + items_html + "</div>"
        "</div>"
    )

def build_email_html(changes, ts):
    total = sum(len(v) for v in changes.values())
    EMOJI = {"new":"&#128994;","removed":"&#9940;","eta":"&#128197;",
             "companhia":"&#9989;","outros":"&#128260;","ext":"&#128268;"}
    sections_html = ""

    for ctype in ["new","removed","eta","companhia","ext","outros"]:
        items_list = changes.get(ctype, [])
        if not items_list:
            continue
        accent, _, label = CARD_COLORS.get(ctype, (ORANGE, "#fff8f5", ctype))
        emoji = EMOJI.get(ctype, "")
        cards = "".join(build_card(ctype, c) for c in items_list)
        sections_html += (
            "<div style='padding:20px 24px;border-bottom:1px solid #eee'>"
            "<div style='font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:1px;"
            "color:" + accent + ";margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid " + accent + "'>"
            + emoji + "&nbsp; " + label + " <span style='background:" + accent + ";color:#fff;padding:2px 8px;"
            "border-radius:20px;font-size:10px;margin-left:6px'>" + str(len(items_list)) + "</span></div>"
            + cards + "</div>"
        )

    alt_word = "alteracao" if total == 1 else "alteracoes"
    logo_b64 = _get_logo_b64()
    if logo_b64:
        logo_tag = '<img src="data:image/png;base64,' + logo_b64 + '" alt="FJMPC" style="height:48px;display:block;margin-bottom:10px" />'
    else:
        logo_tag = '<span style="color:' + ORANGE + ';font-size:26px;font-weight:900;letter-spacing:-1px">FJMPC</span>'

    return (
        '<!DOCTYPE html>\n'
        '<html lang="pt">\n'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>\n'
        '<body style="margin:0;padding:24px 16px;background:#1e1e2e;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif">\n'
        '<div style="max-width:660px;margin:0 auto">\n'
        '  <div style="background:#2d2d3f;border-radius:12px 12px 0 0;padding:24px 28px;border-bottom:3px solid ' + ORANGE + '">\n'
        '    <table width="100%" cellpadding="0" cellspacing="0"><tr>\n'
        '      <td style="vertical-align:middle">' + logo_tag + '\n'
        '        <div style="font-size:13px;color:#888;margin-top:2px">Gestao de Contentores 2026 &nbsp;&middot;&nbsp;\n'
        '          <span style="color:' + ORANGE + ';font-weight:600">' + str(total) + ' ' + alt_word + '</span></div>\n'
        '      </td>\n'
        '      <td style="vertical-align:middle;text-align:right"><div style="font-size:11px;color:#555">' + ts + '</div></td>\n'
        '    </tr></table>\n'
        '  </div>\n'
        '  <div style="background:#f8f8fa;border-radius:0 0 12px 12px;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,0.25)">\n'
        '    ' + sections_html + '\n'
        '    <div style="padding:22px 28px;background:#2d2d3f;text-align:center;border-top:1px solid #3a3a4f">\n'
        '      <a href="' + APP_URL + '" style="display:inline-block;background:' + ORANGE + ';color:#3a3a4f;text-decoration:none;\n'
        '         padding:12px 32px;border-radius:8px;font-size:14px;font-weight:800;letter-spacing:0.5px">Abrir App &#8594;</a>\n'
        '      <div style="margin-top:12px;font-size:11px;color:#555">Gerado automaticamente em ' + ts + '</div>\n'
        '    </div>\n'
        '  </div>\n'
        '</div>\n'
        '</body></html>'
    )

# -- Send email --

def send_email(changes):
    if not GMAIL_FROM or not GMAIL_APP_PASS:
        log("AVISO: Email nao configurado (GMAIL_FROM / GMAIL_APP_PASS)")
        return
    ts    = datetime.now().strftime("%d/%m/%Y %H:%M")
    parts = []
    if changes.get("new"):       parts.append(str(len(changes["new"])) + " Novo")
    if changes.get("removed"):   parts.append(str(len(changes["removed"])) + " Removido")
    if changes.get("eta"):       parts.append(str(len(changes["eta"])) + " ETA")
    if changes.get("companhia"): parts.append(str(len(changes["companhia"])) + " Companhia")
    if changes.get("ext"):       parts.append(str(len(changes["ext"])) + " Tracking")
    if changes.get("outros"):    parts.append(str(len(changes["outros"])) + " Outros")
    subject = "Contentores FJMPC - " + " | ".join(parts) + " (" + ts + ")"
    html    = build_email_html(changes, ts)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = "Contentores FJMPC <" + GMAIL_FROM + ">"
    msg["To"]      = ALERT_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    log("A enviar email para " + ALERT_TO + "...")
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo(); smtp.starttls(); smtp.ehlo()
        smtp.login(GMAIL_FROM, GMAIL_APP_PASS)
        smtp.sendmail(GMAIL_FROM, ALERT_TO, msg.as_string())
    log("Email enviado!")

# -- Deploy Cloudflare --

def deploy_to_cloudflare(data):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        log("AVISO: Credenciais Cloudflare nao configuradas")
        return
    with open(HTML_FILE, encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, ensure_ascii=False)
    worker  = html.replace(PLACEHOLDER, payload)
    log("Worker: " + str(len(worker)//1024) + " KB")
    url  = "https://api.cloudflare.com/client/v4/accounts/" + CF_ACCOUNT_ID + "/workers/scripts/" + CF_SCRIPT_NAME
    hdrs = {"Authorization": "Bearer " + CF_API_TOKEN}
    resp = requests.put(url, headers=hdrs,
                        files={"index.js": ("index.js", worker.encode(), "application/javascript")},
                        timeout=60)
    if resp.ok:
        log("Deploy concluido com sucesso")
    else:
        log("ERRO deploy: " + str(resp.status_code) + " " + resp.text[:200])

# -- Main --

def main():
    force = "--force" in sys.argv
    log("=" * 52)
    log("  Contentores 2026 - Deploy  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    log("=" * 52)

    data      = fetch_sheet_data()
    new_state = build_state(data)
    new_hash  = data_hash(data)
    old_hash  = load_hash()
    old_state = load_state()

    n_cont = len(data.get("containers", []))
    n_art  = sum(len((v or {}).get("items",[])) for v in data.get("artigos",{}).values())
    n_ext  = len(data.get("ext_rows", []))
    log("Dados: " + str(n_cont) + " contentores, " + str(n_art) + " artigos, " + str(n_ext) + " linhas tracking")

    if old_hash == new_hash and not force:
        log("Sem alteracoes - a terminar")
        return

    if old_state is None:
        log("Primeiro run - a guardar estado inicial sem enviar email")
        save_state(new_state)
        save_hash(new_hash)
        deploy_to_cloudflare(data)
        log("Tudo concluido")
        return

    changes = detect_changes(old_state, new_state, data.get("artigos", {}))
    total   = sum(len(v) for v in changes.values())

    if total > 0:
        log("Mudancas detectadas: " + str(total) + " - a enviar email")
        send_email(changes)
    else:
        log("Dados alterados mas sem mudancas relevantes - sem email")

    save_state(new_state)
    save_hash(new_hash)
    log("A fazer deploy para Cloudflare...")
    deploy_to_cloudflare(data)
    log("Tudo concluido")


if __name__ == "__main__":
    main()
