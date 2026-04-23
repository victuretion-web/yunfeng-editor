from typing import Union


def decode_process_output(data: Union[bytes, str, None]) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return repair_mojibake_text(data)

    for encoding in ("utf-8", "utf-8-sig", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    decoded = data.decode("utf-8", errors="replace")
    return repair_mojibake_text(decoded)


def repair_mojibake_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text

    candidates = [text]
    for source_encoding, target_encoding, error_mode in (
        ("gbk", "utf-8", "ignore"),
        ("cp936", "utf-8", "ignore"),
        ("latin-1", "utf-8", "ignore"),
    ):
        try:
            candidate = text.encode(source_encoding, errors="strict").decode(target_encoding, errors=error_mode)
        except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
            continue
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    def _score_display_text(value: str) -> int:
        readable_cjk = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
        basic_ascii = sum(1 for ch in value if 32 <= ord(ch) <= 126)
        mojibake_markers = sum(1 for ch in value if ch in "锟斤拷鈥滃鏈鏃璺闊挱")
        replacement_chars = value.count("\ufffd")
        suspicious_chunks = (
            value.count("鍙")
            + value.count("璺")
            + value.count("鏈")
            + value.count("鏃")
            + value.count("闊")
        )
        return readable_cjk * 4 + basic_ascii - mojibake_markers * 3 - suspicious_chunks * 4 - replacement_chars * 6

    return max(candidates, key=_score_display_text)
