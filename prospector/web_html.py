"""HTML do painel (Jinja, autoescape ligado)."""

BASE = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prospector</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--muted:#6b7280;--card:#fff;--line:#e5e7eb;--acc:#0f766e;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e5e7eb;--muted:#9ca3af;--card:#1a1f26;--line:#2a313b;--acc:#2dd4bf;--warn:#f59e0b;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);flex-wrap:wrap}
header a{color:var(--fg);text-decoration:none;font-weight:600}header a.on{color:var(--acc)}
a{color:var(--acc)}main{padding:16px;max-width:1300px;margin:0 auto}
.funnel{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.step{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}
.step b{display:block;font-size:22px}.step span{color:var(--muted);font-size:12px}
.alert{background:var(--card);border:2px solid var(--bad);border-radius:8px;padding:10px 14px;margin-bottom:16px}
form.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input,select,textarea,button{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
button{cursor:pointer}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}button.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted)}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:12px}
.muted{color:var(--muted)}.wrap{overflow-x:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:10px}
.card textarea{width:100%;min-height:90px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
</style></head><body>
<header><a href="{{ url_for('leads') }}" class="{{ 'on' if page=='leads' }}">Leads</a>
<a href="{{ url_for('fila') }}" class="{{ 'on' if page=='fila' }}">Fila de aprovação</a>
<a href="{{ url_for('envios') }}" class="{{ 'on' if page=='envios' }}">Log de envios</a>
<span class="muted" style="margin-left:auto">prospector</span></header>
<main>
{% if parado %}<div class="alert"><b>Envio PARADO:</b> {{ parado }}<br>
<span class="muted">Confira a sessão no WA-AKG e rode <code>prospector retomar</code>.</span></div>{% endif %}
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="card">{{ m }}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}
</main></body></html>"""

LEADS = """{% extends 'base.html' %}{% block body %}
<div class="funnel">
{% for k, label in [('encontrados','encontrados'),('aprovados','aprovados'),('enviados','enviados'),('responderam','responderam'),('interessados','interessados')] %}
<div class="step"><b>{{ funil[k] }}</b><span>{{ label }}</span></div>{% endfor %}
<div class="step"><b>{{ funil['validos'] }}</b><span>válidos p/ WhatsApp</span></div>
</div>
<form class="filters" method="get">
<select name="busca"><option value="">todas as buscas</option>{% for b in buscas %}<option {{ 'selected' if b==f.busca }}>{{ b }}</option>{% endfor %}</select>
<select name="cidade"><option value="">todas as cidades</option>{% for c in cidades %}<option {{ 'selected' if c==f.cidade }}>{{ c }}</option>{% endfor %}</select>
<select name="status"><option value="">todos os status</option>{% for s in statuses %}<option {{ 'selected' if s==f.status }}>{{ s }}</option>{% endfor %}</select>
<input name="q" placeholder="nome contém…" value="{{ f.q or '' }}">
<button>Filtrar</button>
<a href="{{ url_for('export_csv', **f) }}"><button type="button">Exportar CSV</button></a>
</form>
<p class="muted">{{ leads|length }} leads{% if leads|length == limite %} (mostrando os {{ limite }} primeiros){% endif %}</p>
<div class="wrap"><table><tr><th>Nome</th><th>Cidade</th><th>Telefone</th><th>Oferta</th><th>Nota</th><th>Status</th><th>Resposta</th><th>Ação</th></tr>
{% for l in leads %}<tr>
<td><b>{{ l.nome }}</b><br><span class="muted">{{ l.categoria or '' }}</span>
{% if l.maps_link %}<br><a href="{{ l.maps_link }}" target="_blank" rel="noopener noreferrer">Maps</a>{% endif %}
{% if l.site %} · <a href="{{ l.site if l.site.startswith('http') else 'http://' ~ l.site }}" target="_blank" rel="noopener noreferrer">site</a>{% endif %}</td>
<td>{{ l.cidade or '' }}</td><td>{{ pretty(l.telefone or l.telefone_raw) }}</td>
<td><span class="tag">{{ l.oferta }}</span></td>
<td>{{ l.nota or '' }}{% if l.avaliacoes %} <span class="muted">({{ l.avaliacoes }})</span>{% endif %}</td>
<td><span class="tag">{{ l.status }}</span>{% if l.motivo %}<br><span class="muted">{{ l.motivo }}</span>{% endif %}</td>
<td class="muted">{{ (l.resposta or '')[:140] }}</td>
<td><form method="post" action="{{ url_for('set_status', lead_id=l.id) }}" class="row">
<select name="status">{% if l.status not in manual_statuses %}<option value="" selected disabled>{{ l.status }}</option>{% endif %}
{% for s in manual_statuses %}<option {{ 'selected' if s==l.status }}>{{ s }}</option>{% endfor %}</select>
<button>ok</button></form></td></tr>{% endfor %}
</table></div>
{% endblock %}"""

FILA = """{% extends 'base.html' %}{% block body %}
<div class="row" style="margin-bottom:12px">
<form method="post" action="{{ url_for('gerar_fila') }}"><button>Gerar fila do dia</button></form>
{% if pendentes %}<form method="post" action="{{ url_for('aprovar_todos') }}"><button class="pri">Aprovar todos os pendentes ({{ pendentes }})</button></form>{% endif %}
<span class="muted">Aprovar não envia: o envio acontece com <code>prospector enviar</code>, respeitando horários e limites.</span>
</div>
{% if not itens %}<p class="muted">Fila vazia. Clique em "Gerar fila do dia" ou rode <code>prospector fila</code>.</p>{% endif %}
{% for i in itens %}<div class="card">
<div class="row"><b>#{{ i.id }} {{ i.nome }}</b><span class="tag">{{ i.tipo }}</span><span class="tag">{{ i.oferta }}</span>
<span class="tag">{{ i.status }}</span><span class="muted">{{ pretty(i.telefone) }} · {{ i.cidade or '' }}
{% if i.nota %} · nota {{ i.nota }} ({{ i.avaliacoes or 0 }}){% endif %}</span></div>
<form method="post" action="{{ url_for('aprovar_item', item_id=i.id) }}">
<textarea name="texto" {{ 'readonly' if i.status != 'pendente' }}>{{ i.texto }}</textarea>
<div class="row">{% if i.status == 'pendente' %}<button class="pri">Aprovar (com este texto)</button>{% endif %}
<button class="bad" formaction="{{ url_for('rejeitar_item', item_id=i.id) }}">Rejeitar</button></div>
</form></div>{% endfor %}
{% endblock %}"""

ENVIOS = """{% extends 'base.html' %}{% block body %}
<div class="wrap"><table><tr><th>Quando</th><th>Número</th><th>Tipo</th><th>Texto</th><th>Resultado</th></tr>
{% for e in envios %}<tr><td>{{ e.enviado_em }}</td><td>{{ pretty(e.telefone) }}</td>
<td>{{ e.tipo }}{% if e.dry_run %} <span class="tag">dry-run</span>{% endif %}</td>
<td>{{ e.texto }}</td><td>{{ 'ok' if e.ok else 'ERRO' }} {{ e.http_status or '' }}<br>
<span class="muted">{{ (e.resposta_api or '')[:200] }}</span></td></tr>{% endfor %}
</table></div>
{% endblock %}"""
