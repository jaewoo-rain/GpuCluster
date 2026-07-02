"""
MkDocs build-time hook.

문제: 교과서 마크다운은 저장소 바깥(코드/문서)을 `../../hook/src/fgpu_hook.c#L128`
같은 상대링크로 가리킨다. 이 링크는 VS Code 나 GitHub 저장소 뷰에서는 잘
작동하지만, MkDocs 로 만든 정적 사이트는 docs/ 폴더 안만 서빙하므로 404 가 된다.

해결: 빌드 시점에만 `../../` 로 시작하는 링크(= docs/ 바깥 = 저장소 루트 기준)를
GitHub blob 주소로 치환한다. 소스 마크다운 자체는 건드리지 않으므로 저장소에서
직접 볼 때의 상대링크 동작은 그대로 유지된다.

docs/onboarding/, docs/build-from-scratch/, docs/study/ 챕터는 모두 docs/ 아래
한 단계 깊이이므로 `../../X` == 저장소 루트의 X 로 일대일 대응된다.
"""

import re

GITHUB_BLOB_BASE = "https://github.com/jaewoo-rain/GpuCluster/blob/main/"

# 마크다운 링크 대상이 ../../ 로 시작하는 경우만 매칭: ](../../PATH)
_OUTSIDE_LINK = re.compile(r"\]\(\.\./\.\./([^)]+)\)")


def on_page_markdown(markdown, page, config, files):
    return _OUTSIDE_LINK.sub(
        lambda m: "](" + GITHUB_BLOB_BASE + m.group(1) + ")",
        markdown,
    )


# 렌더된 본문에서 외부(http/https) 링크에 target="_blank" 를 붙여 새 탭에서 열리게 한다.
# 이미 target 이 지정된 경우엔 건드리지 않는다.
_EXTERNAL_A_TAG = re.compile(r'<a\s+([^>]*?)href="(https?://[^"]+)"([^>]*)>')


def _add_blank_target(m):
    pre, href, post = m.group(1), m.group(2), m.group(3)
    if "target=" in (pre + post):
        return m.group(0)
    return f'<a {pre}href="{href}"{post} target="_blank" rel="noopener noreferrer">'


def on_page_content(html, page, config, files):
    return _EXTERNAL_A_TAG.sub(_add_blank_target, html)
