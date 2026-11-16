from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def focused_document_question(question: str, filenames: Iterable[str]) -> str | None:
    """Separate one named document's title from a single explicit question about it."""
    normalized = unicodedata.normalize("NFKC", question)
    matches: list[tuple[str, str]] = []
    for filename in dict.fromkeys(filenames):
        name = unicodedata.normalize("NFKC", filename)
        title = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
        if len(title) < 4:
            continue
        if re.search(re.escape(title), normalized, flags=re.IGNORECASE):
            matches.append((name, title))
    if len(matches) != 1:
        return None
    name, title = matches[0]
    focused = re.sub(
        f"{re.escape(name)}|{re.escape(title)}", "", normalized, flags=re.IGNORECASE
    ).replace("《》", "")
    focused = re.sub(r"^(?:请)?(?:根据|依据|按照|参照|基于)(?:原文)?", "", focused)
    focused = focused.lstrip(" 的，,:：\t\n")
    if len(focused) < 3 or focused.count("?") + focused.count("？") > 1:
        return None
    if re.search(
        r"[;；\n]|另外|同时|并且|以及|分别|并(?:说明|解释|比较|分析|列出|总结|介绍)", focused
    ):
        return None
    return focused


def requires_document_evidence(question: str, filenames: Iterable[str]) -> bool:
    """Recognize explicit evidence intent without forcing every attachment turn to retrieve."""
    normalized = unicodedata.normalize("NFKC", question).casefold()
    if re.search(
        r"(?:不要|不用|无需|不必)(?:再)?(?:使用|检索|参考|依据|根据|查看).{0,8}(?:pdf|附件|文档|资料)",
        normalized,
    ):
        return False
    compact = "".join(character for character in normalized if character.isalnum())
    for filename in filenames:
        name = unicodedata.normalize("NFKC", filename).casefold()
        title = name.removesuffix(".pdf")
        title_key = "".join(character for character in title if character.isalnum())
        if name in normalized or f"《{title}》" in normalized:
            return True
        if len(title_key) >= 4 and title_key in compact:
            return True
    return bool(
        re.search(
            r"(?:根据|依据|参照|按照|基于|查阅|阅读|查看).{0,16}(?:pdf|附件|文档|资料|原文)",
            normalized,
        )
        or re.search(r"(?:这份|该份|上传的|提供的)(?:\s*)(?:pdf|附件|文档|资料)", normalized)
    )
