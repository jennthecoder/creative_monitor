"""Local progress dashboard: http://localhost:8765

Shows the build log (PROGRESS.md), database contents, run history and the latest
digest. Read-only; safe to leave running while the pipeline executes.
"""
from __future__ import annotations

import json
from pathlib import Path

import markdown
from flask import Flask, jsonify

from src import store

ROOT = Path(__file__).resolve().parent
app = Flask(__name__)


def _md(text: str) -> str:
    return markdown.markdown(text, extensions=["tables", "fenced_code"])


def _state() -> dict:
    conn = store.connect()
    counts = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ["videos", "classifications", "metrics", "comment_themes", "quarantine", "runs"]
    }
    runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 10")]
    videos = []
    for r in conn.execute(
        """SELECT v.*, c.values_json, c.confidence, c.rationale,
                  q.reason AS quarantine_reason, t.themes_json,
                  (SELECT views FROM metrics m WHERE m.video_id = v.video_id
                   ORDER BY measured_at DESC LIMIT 1) AS views,
                  (SELECT likes FROM metrics m WHERE m.video_id = v.video_id
                   ORDER BY measured_at DESC LIMIT 1) AS likes,
                  (SELECT comment_count FROM metrics m WHERE m.video_id = v.video_id
                   ORDER BY measured_at DESC LIMIT 1) AS comment_count
           FROM videos v
           LEFT JOIN classifications c ON c.video_id = v.video_id
           LEFT JOIN quarantine q ON q.video_id = v.video_id
           LEFT JOIN comment_themes t ON t.video_id = v.video_id
           ORDER BY v.published_at DESC LIMIT 100"""
    ):
        d = dict(r)
        d["values"] = json.loads(d.pop("values_json")) if d.get("values_json") else None
        d["themes"] = json.loads(d.pop("themes_json")) if d.get("themes_json") else None
        videos.append(d)

    reports = sorted((ROOT / "reports").glob("digest-*.md"), reverse=True) or \
        sorted((ROOT / "reports").glob("sample-digest.md"))
    digest = _md(reports[0].read_text()) if reports else ""
    progress = (ROOT / "PROGRESS.md").read_text() if (ROOT / "PROGRESS.md").exists() else ""
    return {
        "progress_html": _md(progress),
        "counts": counts,
        "runs": runs,
        "videos": videos,
        "digest_html": digest,
        "digest_name": reports[0].name if reports else None,
    }


@app.get("/api/state")
def api_state():
    return jsonify(_state())


@app.get("/")
def index():
    return PAGE


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Creative Monitor — Progress</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e4df;--accent:#3b5bdb;--warn:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ececea;--mute:#9a9a94;--line:#33332f;--accent:#8ea2ff;--warn:#fb923c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,system-ui,sans-serif}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
h1{font-size:18px;margin:0}.mute{color:var(--mute)}
nav{display:flex;gap:4px;padding:8px 24px;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{background:none;border:1px solid transparent;color:var(--mute);padding:6px 12px;border-radius:6px;cursor:pointer;font:inherit}
nav button.on{border-color:var(--line);background:var(--card);color:var(--ink)}
main{padding:24px;max-width:1200px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px;overflow-x:auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.stat b{display:block;font-size:24px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:500}code{font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;border:1px solid var(--line);font-size:12px;margin:1px}
.low{color:var(--warn);border-color:var(--warn)}
.prose h1,.prose h2{border-bottom:1px solid var(--line);padding-bottom:4px}
.hidden{display:none}
</style></head><body>
<header><h1>Competitor Creative Monitor</h1><span class="mute" id="ts">loading…</span></header>
<nav>
 <button data-tab="progress" class="on">Build progress</button>
 <button data-tab="data">Database</button>
 <button data-tab="runs">Runs</button>
 <button data-tab="digest">Latest digest</button>
</nav>
<main>
 <section id="progress"><div class="card prose" id="progress_html"></div></section>
 <section id="data" class="hidden"><div class="stats" id="stats"></div><div class="card" id="videos"></div></section>
 <section id="runs" class="hidden"><div class="card" id="runs_tbl"></div></section>
 <section id="digest" class="hidden"><div class="card prose" id="digest_html"></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
 document.querySelectorAll('main section').forEach(s=>s.classList.toggle('hidden',s.id!==b.dataset.tab));
});
async function load(){
 try{
 const s=await (await fetch('/api/state')).json();
 $('progress_html').innerHTML=s.progress_html;
 $('stats').innerHTML=Object.entries(s.counts).map(([k,v])=>`<div class="stat"><span class="mute">${k}</span><b>${v}</b></div>`).join('');
 $('videos').innerHTML=s.videos.length?`<table><tr><th>Published</th><th>Brand</th><th>Title</th><th>Views</th><th>Likes</th><th>Comments</th><th>Classification</th><th>Comment themes</th></tr>`+
  s.videos.map(v=>`<tr><td>${esc((v.published_at||'').slice(0,10))}</td><td>${esc(v.brand)}</td>
  <td><a href="https://youtu.be/${esc(v.video_id)}" target="_blank">${esc(v.title)}</a></td>
  <td>${v.views??'—'}</td><td>${v.likes??'—'}</td><td>${v.comment_count??'—'}</td>
  <td>${v.values?Object.values(v.values).map(x=>`<span class="pill">${esc(x)}</span>`).join('')+` <span class="pill ${v.confidence==='low'?'low':''}">${esc(v.confidence)}</span>`:(v.quarantine_reason?`<span class="pill low">quarantined</span>`:'<span class="mute">—</span>')}</td>
  <td>${v.themes?esc((v.themes.recurring_themes||[]).slice(0,2).join('; ')):'<span class="mute">—</span>'}</td></tr>`).join('')+'</table>'
  :'<p class="mute">No videos yet — waiting for the first fetch.</p>';
 $('runs_tbl').innerHTML=s.runs.length?`<table><tr><th>#</th><th>Started</th><th>Completed</th><th>Found</th><th>Classified</th><th>Errors</th><th>Status</th><th>Message</th></tr>`+
  s.runs.map(r=>`<tr><td>${r.run_id}</td><td>${esc(r.started_at)}</td><td>${esc(r.completed_at)}</td><td>${r.videos_found??''}</td><td>${r.videos_classified??''}</td><td>${r.errors??''}</td><td>${esc(r.status)}</td><td>${esc(r.message)}</td></tr>`).join('')+'</table>'
  :'<p class="mute">No runs yet.</p>';
 $('digest_html').innerHTML=s.digest_html?`<p class="mute">${esc(s.digest_name)}</p>`+s.digest_html:'<p class="mute">No digest generated yet (Phase 6).</p>';
 $('ts').textContent='updated '+new Date().toLocaleTimeString();
 }catch(e){$('ts').textContent='dashboard offline'}
}
load();setInterval(load,5000);
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
