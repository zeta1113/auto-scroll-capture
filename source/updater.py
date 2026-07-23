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
    'download_url': 설치 파일(.exe) 직접 다운로드 URL(있으면),
    'html_url': 릴리즈 페이지 URL,
    'notes': 릴리즈 노트(본문),
  }
"""

import re
import json
import urllib.request


def parse_version(s):
    s = (s or "").strip().lstrip("vV")
    nums = re.findall(r"\d+", s)[:3]
    nums = [int(x) for x in nums]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def check_latest(repo, current_version, timeout=6):
    if not repo or "/" not in repo:
        return None
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "ScrollCapture-Updater",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None

    tag = data.get("tag_name") or data.get("name") or ""
    latest = parse_version(tag)
    cur = parse_version(current_version)

    download_url = None
    for a in data.get("assets", []) or []:
        name = (a.get("name") or "").lower()
        if name.endswith(".exe"):
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
