---
name: presentation-design
description: "Use when making slides, decks, or presentations look polished or follow a specific brand/visual aesthetic — especially Apple/glass (frosted vibrancy, SF Pro), editorial-minimal, terminal-core, cinematic-dark, data-dense, playful, or brutalist. Also for turning a README or project doc into a pitch deck, writing speaker notes, or producing a single beautiful HTML slide. Provides brand DESIGN.md references, prompt packs, and recipes. Pair with the `pptx` skill to export the result to a real .pptx file."
---

# Presentation Design

브랜드 미감/디자인 시스템에 맞춰 **예쁜 슬라이드·덱**을 만들기 위한 레퍼런스 번들.
실제 `.pptx` 파일 생성/편집이 필요하면 형제 스킬 **`pptx`** 와 함께 쓴다
(이 스킬 = "어떻게 보이게", pptx 스킬 = "어떤 파일로 출력").

## 빠른 사용 (Quick Reference)

| 원하는 것 | 읽을 파일 |
|---|---|
| **Apple 형식**(frosted/vibrancy, SF Pro, HIG 타입스케일) | [design-md/glass/apple.md](design-md/glass/apple.md) |
| 다른 브랜드 미감(35종) | `design-md/<family>/<brand>.md` 아래 표 참고 |
| 한 장짜리 **예쁜 HTML 슬라이드** (CSS 규칙 + few-shot) | [slide-prompt-html.md](slide-prompt-html.md) |
| **README → 12장 피치덱** (Keynote 느낌, PDF/PPTX export) | [recipes/pitch-deck-from-readme.md](recipes/pitch-deck-from-readme.md) |
| 발표자 노트 작성 | [recipes/speaker-notes-pitch-deck.md](recipes/speaker-notes-pitch-deck.md) |
| 어느 미감을 쓸지 고르기 | [prompts/family-picker.md](prompts/family-picker.md) |
| 기존 브랜드를 DESIGN.md 로 추출 | [prompts/brand-to-design-md.md](prompts/brand-to-design-md.md) |
| AI-티 나는 기본 미감 깨기 | [prompts/break-default-aesthetic.md](prompts/break-default-aesthetic.md) |

## 사용 방법

1. **미감 고르기** — Apple 풍이면 `design-md/glass/apple.md`. 다른 느낌이면
   `prompts/family-picker.md` 로 후보를 좁힌다. 패밀리:
   - `glass/` (Apple, Arc — frosted/soft-futurism)
   - `editorial/` (Linear, Vercel — 미니멀)
   - `terminal/` (Ollama, Warp, opencode — mono/dark)
   - `cinematic/` (NVIDIA, Ferrari, Runway 등 — 극적/다크)
   - `data-dense/` (ClickHouse, Datadog, MongoDB, PostHog)
   - `playful/` (Figma, Canva, Toss), `warm/` (Claude, Mercury),
     `brutalist/` (The Verge), `indie/` (Granola), `remix/` (브랜드 조합)
2. **DESIGN.md 를 컨텍스트로** — 고른 `*.md` 의 색/타이포/컴포넌트/Do·Don't 규칙을
   그대로 따른다. (예: Apple → SF Pro, system tint 1개/표면, `backdrop-filter: blur(30px)
   saturate(180%)`, 카드 그림자 금지, 1px 하드 보더 금지)
3. **슬라이드 생성** —
   - HTML 슬라이드(브라우저 발표/스크린셰어/PDF export): `slide-prompt-html.md` 의
     INSTRUCTION + CSS 포맷을 따르고, 위에서 고른 DESIGN.md 의 색/폰트로 치환한다.
   - 실제 PowerPoint 파일: **`pptx` 스킬**의 `pptxgenjs.md`(scratch) 또는 `editing.md`(템플릿)
     를 읽어 `.pptx` 로 출력한다. DESIGN.md 의 팔레트/타입스케일을 pptx 테마에 반영.
4. **덱 단위** — README/문서를 통째로 덱으로: `recipes/pitch-deck-from-readme.md`
   (감사: 1줄 피치 → 문제 → 차별점 → 증거 순으로 README 를 먼저 다듬고 → DESIGN.md 페어링
   → 덱 프롬프트 → 핵심 슬라이드 인라인 수정 → PDF/PPTX export).

## 이 프로젝트(GpuCluster)에 적용 예

- "이 레포 `README.md` 를 Apple 형식 12장 피치덱으로 만들어줘" →
  `recipes/pitch-deck-from-readme.md` + `design-md/glass/apple.md` 조합.
- "fGPU fractional 공유 실험 결과(`fgpu_analysis` 산출 그래프)를 슬라이드로" →
  `slide-prompt-html.md` 로 한 장씩, 또는 `pptx` 스킬로 `.pptx`.

## 라이선스 / 출처

번들 자료는 **MIT** (awesome-claude-design, presentation_claude_prompt). 자세한 출처와
원저작자 표기는 [ATTRIBUTION.md](ATTRIBUTION.md) 참조. (형제 `pptx` 스킬은 Anthropic
**proprietary** — 그쪽 `LICENSE.txt` 별도.)
