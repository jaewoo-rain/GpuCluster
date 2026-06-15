#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docs/study/easy/ 의 초등학생용 교과서(README + 01~11)를 하나의 PDF로 묶는다.
마크다운 -> HTML -> google-chrome headless --print-to-pdf.
ASCII 그림 정렬을 위해 코드블록은 'Noto Sans Mono CJK KR'(한글 고정폭) 사용.
"""
import os
import re
import subprocess
import markdown

HERE = os.path.dirname(os.path.abspath(__file__))

# 읽는 순서
ORDER = [
    "README.md",
    "01-컴퓨터와-두뇌-GPU.md",
    "02-GPU를-왜-나눠쓰나.md",
    "03-프로그램이-GPU에게-일시키기.md",
    "04-가로채기-마법-후킹.md",
    "05-메모리-칸-나눠주기-quota.md",
    "06-각자-방-컨테이너.md",
    "07-접수창구-서버.md",
    "08-전체-설계도.md",
    "09-한번의-신청이-흘러가는-길.md",
    "10-같이쓰면-빨라질까-우리실험.md",
    "11-아직-못하는것-한계.md",
]

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Noto Sans CJK KR", "Noto Color Emoji", sans-serif;
  font-size: 12.5pt; line-height: 1.7; color: #1f2933;
  max-width: 100%;
}
.chapter { page-break-after: always; }
.chapter:last-child { page-break-after: auto; }
h1 { font-size: 21pt; color: #1d4ed8; border-bottom: 3px solid #93c5fd;
     padding-bottom: 6px; margin-top: 4px; }
h2 { font-size: 15.5pt; color: #1e3a8a; margin-top: 22px;
     border-left: 6px solid #60a5fa; padding-left: 10px; }
h3 { font-size: 13.5pt; color: #2563eb; margin-top: 16px; }
blockquote {
  background: #eff6ff; border-left: 5px solid #3b82f6;
  margin: 12px 0; padding: 8px 14px; color: #1e40af; border-radius: 4px;
}
pre {
  background: #0f172a; color: #e2e8f0;
  font-family: "Noto Sans Mono CJK KR", "Noto Color Emoji", monospace;
  font-size: 10.5pt; line-height: 1.35;
  padding: 12px 14px; border-radius: 8px; overflow: visible;
  white-space: pre; word-wrap: normal;
}
code {
  font-family: "Noto Sans Mono CJK KR", monospace;
  background: #e2e8f0; color: #b91c1c;
  padding: 1px 5px; border-radius: 4px; font-size: 0.92em;
}
pre code { background: none; color: inherit; padding: 0; font-size: 1em; }
table { border-collapse: collapse; margin: 14px 0; width: 100%; font-size: 11.5pt; }
th, td { border: 1px solid #cbd5e1; padding: 7px 10px; text-align: left; }
th { background: #dbeafe; color: #1e3a8a; }
tr:nth-child(even) td { background: #f8fafc; }
hr { border: none; border-top: 2px dashed #cbd5e1; margin: 20px 0; }
a { color: #2563eb; text-decoration: none; }
ul, ol { padding-left: 22px; }
li { margin: 4px 0; }
strong { color: #b45309; }
.cover {
  page-break-after: always; text-align: center; padding-top: 95mm;
}
.cover h1 { font-size: 32pt; border: none; color: #1d4ed8; }
.cover .sub { font-size: 15pt; color: #475569; margin-top: 18px; }
.cover .meta { font-size: 11pt; color: #94a3b8; margin-top: 60mm; }
"""

md = markdown.Markdown(extensions=["fenced_code", "tables", "sane_lists", "nl2br"])

def render_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # .md 내부 링크는 PDF 안에서 의미 없으니 텍스트로만 (앵커 깨짐 방지)
    md.reset()
    return md.convert(text)

parts = [
    '<div class="cover">',
    "<h1>🎈 아주 쉬운<br>GpuCluster 교과서</h1>",
    '<div class="sub">코딩을 한 번도 안 해본 친구를 위한<br>GPU 나눠 쓰기 이야기</div>',
    '<div class="meta">12개 챕터 · 그림과 비유로 배우는 입문 트랙</div>',
    "</div>",
]

for fname in ORDER:
    fpath = os.path.join(HERE, fname)
    if not os.path.exists(fpath):
        print(f"  ! 누락: {fname}")
        continue
    html = render_file(fpath)
    parts.append(f'<div class="chapter">{html}</div>')

full_html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>아주 쉬운 GpuCluster 교과서</title>
<style>{CSS}</style></head>
<body>{''.join(parts)}</body></html>"""

html_out = os.path.join(HERE, "_book.html")
with open(html_out, "w", encoding="utf-8") as f:
    f.write(full_html)
print(f"HTML 생성: {html_out}")

pdf_out = os.path.join(HERE, "아주쉬운-GpuCluster-교과서.pdf")
cmd = [
    "google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox",
    "--no-pdf-header-footer",
    f"--print-to-pdf={pdf_out}",
    "file://" + html_out,
]
print("크롬 렌더링 중...")
res = subprocess.run(cmd, capture_output=True, text=True)
if res.returncode != 0:
    # 일부 크롬 버전은 --no-pdf-header-footer 미지원 → 재시도
    cmd2 = [c for c in cmd if c != "--no-pdf-header-footer"]
    res = subprocess.run(cmd2, capture_output=True, text=True)
print("stderr:", res.stderr[-400:] if res.stderr else "(none)")
print(f"PDF 생성: {pdf_out}" if os.path.exists(pdf_out) else "PDF 생성 실패")
