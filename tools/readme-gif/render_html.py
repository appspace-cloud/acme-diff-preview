"""Render the produced comment as a Bitbucket-style PR page, for the GIF."""
import os
import re

import markdown

HERE = os.path.dirname(os.path.abspath(__file__))
md_text = open(os.path.join(HERE, "comment.md")).read()

# Colourise the unified-diff bodies before markdown sees them, so added and
# removed lines read the way they do in a real PR.
def _colour(match):
    out = []
    for line in match.group(1).split("\n"):
        cls = ("add" if line.startswith("+") and not line.startswith("+++")
               else "del" if line.startswith("-") and not line.startswith("---")
               else "meta" if line.startswith("@@") else "ctx")
        out.append(f'<span class="{cls}">{line or " "}</span>')
    return '<pre class="diff">' + "\n".join(out) + "</pre>"


blocked = "DO NOT MERGE" in md_text
BADGE = ('<div class="badge bad">&#9940; BUILD FAILED</div>' if blocked
         else '<div class="badge ok">&#10003; 1 REVIEW ITEM</div>')

html_body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
html_body = re.sub(r"<pre><code>(.*?)</code></pre>", _colour, html_body,
                   flags=re.S)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
* { box-sizing: border-box; }
body { margin:0; background:#f1f2f6; font-family:-apple-system,BlinkMacSystemFont,
  "Segoe UI",Helvetica,Arial,sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:980px; margin:0 auto; padding:26px 20px 60px; }
.pr { background:#fff; border:1px solid #dfe1e6; border-radius:8px;
  box-shadow:0 1px 3px rgba(9,30,66,.13); overflow:hidden; }
.prhead { display:flex; align-items:center; gap:11px; padding:13px 20px;
  border-bottom:1px solid #ebecf0; background:#fafbfc; }
.avatar { width:30px;height:30px;border-radius:50%;
  background:linear-gradient(135deg,#0052cc,#2684ff); color:#fff; font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:12px; }
.who { font-weight:600; color:#172b4d; font-size:14px; }
.when { color:#6b778c; font-size:13px; }
.badge { margin-left:auto; font-weight:700; font-size:11px; padding:4px 10px;
  border-radius:3px; letter-spacing:.4px; }
.badge.bad { background:#ffebe6; color:#bf2600; }
.badge.ok  { background:#e3fcef; color:#006644; }
.md { padding:6px 26px 26px; color:#172b4d; font-size:14.5px; line-height:1.62; }
.md h2 { font-size:19px; margin:26px 0 12px; padding-bottom:7px;
  border-bottom:1px solid #ebecf0; color:#091e42; }
.md h2:first-child { margin-top:14px; }
.md h3,.md h4 { font-size:16px; margin:20px 0 9px; color:#091e42; }
.md p { margin:11px 0; }
.md ul { margin:11px 0; padding-left:22px; } .md li { margin:6px 0; }
.md code { background:#f4f5f7; color:#172b4d; padding:2px 5px; border-radius:3px;
  font-size:12.5px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.md hr { border:0; border-top:1px solid #ebecf0; margin:22px 0; }
.md table { border-collapse:collapse; width:100%; margin:14px 0; font-size:13px; }
.md th { background:#f4f5f7; text-align:left; font-weight:700; color:#5e6c84; }
.md th,.md td { border:1px solid #dfe1e6; padding:8px 11px; vertical-align:top; }
.md a { color:#0052cc; text-decoration:none; }
pre.diff { background:#fbfbfc; border:1px solid #dfe1e6; border-radius:5px;
  padding:12px 14px; overflow-x:auto; font-size:12.5px; line-height:1.55;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; margin:12px 0; }
pre.diff span { display:block; padding:1px 6px; margin:0 -6px; white-space:pre; }
.add { background:#e3fcef; color:#006644; } .del { background:#ffebe6; color:#bf2600; }
.meta { color:#6b778c; } .ctx { color:#42526e; }
</style></head><body><div class="wrap"><div class="pr">
<div class="prhead">
  <div class="avatar">AD</div>
  <div><div class="who">acme-diff-preview</div>
       <div class="when">commented on pull request #4312 &middot; just now</div></div>
  __BADGE__
</div>
<div class="md">__BODY__</div></div></div></body></html>"""

out = os.path.join(HERE, "comment.html")
with open(out, "w") as f:
    f.write(PAGE.replace("__BODY__", html_body).replace("__BADGE__", BADGE))
print("wrote", out, os.path.getsize(out), "bytes")
