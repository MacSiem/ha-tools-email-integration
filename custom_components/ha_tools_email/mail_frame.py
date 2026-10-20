"""Shared table-based HA Tools mail frame; light inline styles and dark tokens."""
from html import escape
from urllib.parse import urlsplit

def E(value):
    return escape(str(value) if value is not None else '', quote=True)

L = dict(bg='#f3f4f6', card='#ffffff', tx='#111827', tx2='#4b5563', tx3='#6b7280', bd='#e5e7eb', grid='#eef0f3',
         cheap='#1baf7a', exp='#2a78d6', d1='#eb6834', d2='#6250d6', d3='#e87ba4', d4='#eda100', other='#9ca3af', unmeasured='#d1d5db',
         good='#15803d', warn='#a16207', crit='#b91c1c', accent='#2a78d6', btn='#2563eb', btntx='#ffffff', chip='#f3f4f6')
D = dict(bg='#111214', card='#1c1c1e', tx='#e5e7eb', tx2='#a1a1aa', tx3='#8b8b93', bd='#2e2f33', grid='#26272b',
         cheap='#199e70', exp='#3987e5', d1='#d95926', d2='#9085e9', d3='#d55181', d4='#c98500', other='#6b7280', unmeasured='#4b5563',
         good='#22c55e', warn='#fab219', crit='#f87171', accent='#3987e5', btn='#818cf8', btntx='#0f172a', chip='#26272b')
L['unm'] = L['unmeasured']; D['unm'] = D['unmeasured']; L['room'] = '#6b7280'; D['room'] = '#8b8b93'
FONT = "Arial,'Helvetica Neue',Helvetica,sans-serif"

def css():
    pairs = [('bg', 'background-color', 'bg'), ('card', 'background-color', 'card'), ('chipbg', 'background-color', 'chip'),
             ('tx', 'color', 'tx'), ('tx2', 'color', 'tx2'), ('tx3', 'color', 'tx3'), ('bd', 'border-color', 'bd'),
             ('grid', 'background-color', 'grid'), ('cheap', 'background-color', 'cheap'), ('exp', 'background-color', 'exp'),
             ('d1', 'background-color', 'd1'), ('d2', 'background-color', 'd2'), ('d3', 'background-color', 'd3'), ('d4', 'background-color', 'd4'),
             ('other', 'background-color', 'other'), ('unm', 'background-color', 'unmeasured'), ('room', 'background-color', 'room'), ('acc', 'background-color', 'accent'),
             ('btn', 'background-color', 'btn'), ('btntx', 'color', 'btntx'), ('good', 'color', 'good'), ('warn', 'color', 'warn'), ('crit', 'color', 'crit'),
             ('tick', 'background-color', 'tx')]
    dark = '\n'.join(f'  .{c} {{ {p}: {D[k]} !important; }}' for c, p, k in pairs)
    ogsc = '\n'.join(f'[data-ogsc] .{c} {{ color: {D[k]} !important; }}' for c, p, k in pairs if p == 'color')
    ogsb = '\n'.join(f'[data-ogsb] .{c} {{ background-color: {D[k]} !important; }}' for c, p, k in pairs if p == 'background-color')
    return f'''
:root {{ color-scheme: light dark; supported-color-schemes: light dark; }}
body {{ margin:0; padding:0; -webkit-text-size-adjust:100%; }}
table {{ border-collapse:collapse; mso-table-lspace:0; mso-table-rspace:0; }}
a {{ text-decoration:none; }}
@media screen and (max-width: 480px) {{
  .wrap {{ padding:12px 8px !important; }}
  .pad {{ padding-left:16px !important; padding-right:16px !important; }}
  .kpi {{ display:block !important; width:100% !important; box-sizing:border-box; margin-bottom:8px !important; }}
  .kpigap {{ display:none !important; }}
  .h1 {{ font-size:20px !important; line-height:26px !important; }}
  .hm {{ display:none !important; }}
  .bar {{ width:14% !important; }}
  .rank-name {{ width:42% !important; }}
  .zone-hours {{ display:block !important; }}
  .zone-value {{ font-size:12px !important; }}
  .rank-value {{ width:24% !important; font-size:12px !important; padding-left:4px !important; }}
  .rank-cost {{ width:20% !important; font-size:12px !important; padding-left:4px !important; }}
}}
@media (prefers-color-scheme: dark) {{
{dark}
}}
{ogsc}
{ogsb}
'''

def c(cls, k):                     # klasa + kolor inline (jasny)
    return cls, L[k]

# ------------------------------------------------------------------ bloki
def h2(t, sub=''):
    s = f'<p class="tx2" style="margin:2px 0 0;font:400 12px/16px {FONT};color:{L["tx2"]}">{E(sub)}</p>' if sub else ''
    return f'<tr><td class="pad" style="padding:24px 24px 8px"><p class="tx" style="margin:0;font:700 16px/22px {FONT};color:{L["tx"]}">{E(t)}</p>{s}</td></tr>'

def hr():
    return f'<tr><td class="pad" style="padding:0 24px"><table role="presentation" width="100%"><tr><td class="grid" height="1" style="height:1px;line-height:1px;font-size:0;background-color:{L["bd"]}">&nbsp;</td></tr></table></td></tr>'

def kpi_cell(label, value, unit, note, note_cls='tx2'):
    return (f'<td class="kpi chipbg" valign="top" style="width:32%;padding:12px 14px;background-color:{L["chip"]};border-radius:10px">'
            f'<p class="tx2" style="margin:0;font:400 12px/16px {FONT};color:{L["tx2"]}">{E(label)}</p>'
            f'<p class="tx" style="margin:4px 0 0;font:700 26px/32px {FONT};color:{L["tx"]}">{E(value)}<span class="tx2" style="font:400 14px/20px {FONT};color:{L["tx2"]}">&nbsp;{E(unit)}</span></p>'
            f'<p class="{note_cls}" style="margin:2px 0 0;font:400 12px/16px {FONT};color:{L[note_cls]}">{E(note)}</p></td>')

def safe_external_url(value):
    """Allow absolute HTTP(S) navigation, never script/data or ambiguous URLs."""
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(c.isspace() for c in parsed.netloc) or '\\' in value):
            return None
        parsed.port
    except ValueError:
        return None
    return value


def paragraph(text, *, kind='tx2', margin='4px 0 0'):
    return (f'<p class="{kind}" style="margin:{margin};font:400 12px/18px {FONT};'
            f'color:{L[kind]}">{E(text)}</p>')


def section(content, *, padding='0 24px'):
    return f'<tr><td class="pad" style="padding:{padding}">{content}</td></tr>'


def button(url, label):
    url = safe_external_url(url)
    if not url:
        return ''
    return (f'<table role="presentation" style="margin-top:16px"><tr><td class="btn" '
            f'bgcolor="{L["btn"]}" style="border-radius:999px;background-color:{L["btn"]}">'
            f'<a class="btntx" href="{E(url)}" style="display:inline-block;padding:10px 18px;'
            f'font:700 14px/20px {FONT};color:{L["btntx"]};text-decoration:none">{E(label)}</a>'
            '</td></tr></table>')


def render_frame(*, language, title, preheader, eyebrow, subtitle, content, closing):
    """Content/closing are trusted renderer markup; plain metadata is escaped."""
    return f'''<!doctype html>
<html lang="{E(language)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">
<title>{E(title)}</title><style>{css()}</style></head>
<body class="bg" style="margin:0;padding:0;background-color:{L['bg']}">
<!-- block:0 --><div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">{E(preheader)}&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;</div>
<table role="presentation" width="100%" class="bg" bgcolor="{L['bg']}" style="background-color:{L['bg']}"><tr><td class="wrap" align="center" style="padding:24px 12px">
<table role="presentation" width="600" class="card" bgcolor="{L['card']}" style="width:100%;max-width:600px;background-color:{L['card']};border-radius:12px;overflow:hidden">
<!-- block:1 --><tr><td class="acc" height="4" bgcolor="{L['accent']}" style="height:4px;line-height:4px;font-size:0;background-color:{L['accent']}">&nbsp;</td></tr>
<!-- block:2 --><tr><td class="pad" style="padding:20px 24px 4px">
<p class="tx2" style="margin:0;font:700 11px/16px {FONT};letter-spacing:.06em;text-transform:uppercase;color:{L['tx2']}">{E(eyebrow)}</p>
<h1 class="tx h1" style="margin:6px 0 0;font:700 22px/28px {FONT};color:{L['tx']}">{E(title)}</h1>
<p class="tx2" style="margin:4px 0 0;font:400 14px/20px {FONT};color:{L['tx2']}">{E(subtitle)}</p></td></tr>
{content}{closing}
</table></td></tr></table></body></html>'''
