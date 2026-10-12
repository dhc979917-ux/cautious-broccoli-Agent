"""Bounded per-call output collection with the real beginning and end."""


class ToolOutputBuffer:
    def __init__(self, head: int = 4000, tail: int = 1000):
        self._head_limit = head
        self._tail_limit = tail
        self._head = ""
        self._tail = ""
        self._total = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self._total += len(text)
        remaining = self._head_limit - len(self._head)
        if remaining > 0:
            self._head += text[:remaining]
        self._tail = (self._tail + text)[-self._tail_limit:]

    def render(self) -> str:
        if self._total <= self._head_limit:
            return self._head
        if self._total <= self._head_limit + self._tail_limit:
            return self._head + self._tail[-(self._total - self._head_limit):]
        omitted = self._total - self._head_limit - self._tail_limit
        return self._head + f"\n…[中间省略 {omitted} 字符]…\n" + self._tail
