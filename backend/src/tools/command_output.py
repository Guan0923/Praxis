"""Model-visible command output budgets, separate from process buffering."""

from backend.providers.token_usage import _encode_length


def limit_output(text: str, tokens: int, chars: int = 20000) -> tuple[str, int]:
    def fits(value: str) -> bool:
        return len(value) <= chars and _encode_length(value, "unknown") <= tokens

    if fits(text):
        return text, 0
    low, high = 0, min(len(text), chars)
    best = ""
    omitted_bytes = len(text.encode("utf-8"))
    while low <= high:
        count = (low + high) // 2
        left = (count + 1) // 2
        right = count // 2
        omitted = len(text[left : len(text) - right if right else len(text)].encode("utf-8"))
        marker = f"\n[... {omitted} bytes omitted ...]\n"
        value = text[:left] + marker + (text[-right:] if right else "")
        if fits(value):
            best = value
            omitted_bytes = omitted
            low = count + 1
        else:
            high = count - 1
    return best, omitted_bytes
