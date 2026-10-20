# -*- coding: utf-8 -*-
"""
GitHub 릴리즈 기반 업데이트 확인.
표준 라이브러리만 사용(urllib) — 추가 의존성 없음.

check_latest(repo, current_version) -> dict 또는 None
  repo            : "owner/name"
  current_version : "1.1.0"
  반환: {
    'tag': 'v1.2.0',
    'version': (1,2,0),
    'newer': True/False,
    'download_url': 설치 파일(.exe / macOS .dmg) 직접 다운로드 URL(있으면),
    'html_url': 릴리즈 페이지 URL,
    'notes': 릴리즈 노트(본문),
  }
"""

import re
import ssl
import sys
import json
import platform
import urllib.request


def parse_version(s):
    s = (s or "").strip().lstrip("vV")
    nums = re.findall(r"\d+", s)[:3]
    nums = [int(x) for x in nums]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _asset_matches(name):
    """현재 OS/CPU 에 맞는 설치 파일인지 (Windows: .exe, macOS: 아키텍처별 .dmg)."""
    if sys.platform == "darwin":
        arch = platform.machine().lower()          # arm64 / x86_64
        return name.endswith(".dmg") and arch in name
    return name.endswith(".exe")


def ssl_context():
    """HTTPS 인증서 검증용 컨텍스트.
    macOS 앱에 포함된 python.org Python 은 시스템 인증서를 읽지 못하므로
    함께 묶은 certifi 인증서 묶음을 사용한다(없으면 기본값)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def check_latest(repo, current_version, timeout=6):
    if not repo or "/" not in repo:
        return None
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ScrollCapture-Updater",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as r:
            data = json.load(r)
    except Exception:
        return None

    tag = data.get("tag_name") or data.get("name") or ""
    latest = parse_version(tag)
    cur = parse_version(current_version)

    download_url = None
    for a in data.get("assets", []) or []:
        name = (a.get("name") or "").lower()
        if _asset_matches(name):
            download_url = a.get("browser_download_url")
            break

    return {
        "tag": tag,
        "version": latest,
        "newer": latest > cur,
        "download_url": download_url,
        "html_url": data.get("html_url"),
        "notes": data.get("body") or "",
    }
