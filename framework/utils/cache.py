"""Account retained cache payloads and LRU index storage within a byte budget."""

from collections import OrderedDict
from collections.abc import Callable, Iterator, MutableMapping
import sys
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class ByteLRU(MutableMapping[K, V], Generic[K, V]):
    """
    Retain values within a measured byte budget and evict least-recently used keys.

    The caller measures key/value storage it owns. This cache additionally charges
    entry wrappers, stored size integers and the actual OrderedDict index growth.
    The fixed empty cache object is excluded. Values must not grow after insertion;
    shared externally owned entities can be excluded by the supplied size function.
    """

    def __init__(self, max_bytes: int, size_of: Callable[[K, V], int]) -> None:
        """
        Set a byte budget and the payload measurement used on insertion.

        Args:
            max_bytes (int): Positive retained-byte budget, excluding fixed empty storage.
            size_of   (Callable[[K, V], int]): Owned key/value bytes; shared entities may be excluded.

        Raises:
            ValueError: If the byte budget is not a positive integer.
        """
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Cache budget must be a positive integer number of bytes")
        self.max_bytes = max_bytes
        self.size_of = size_of
        self._entries: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._empty_index_bytes = sys.getsizeof(self._entries)
        self._payload_bytes = 0

    @property
    def bytes_used(self) -> int:
        """Return charged payload and index bytes, excluding fixed empty storage."""
        return self._payload_bytes + sys.getsizeof(self._entries) - self._empty_index_bytes

    @staticmethod
    def _charge(entry: tuple[V, int]) -> int:
        """Include the retained entry wrapper and its size integer."""
        return entry[1] + sys.getsizeof(entry) + sys.getsizeof(entry[1])

    def __getitem__(self, key: K) -> V:
        """Return a value and make its key most recently used."""
        entry = self._entries[key]
        self._entries.move_to_end(key)
        return entry[0]

    def __setitem__(self, key: K, value: V) -> None:
        """Insert a value, evicting old entries; oversized values are not retained."""
        size = self.size_of(key, value)
        if type(size) is not int or size < 0:
            raise ValueError("Cache payload size must be a nonnegative integer")
        if key in self._entries:
            del self[key]
        entry = (value, size)
        charge = self._charge(entry)
        if charge > self.max_bytes:
            return
        self._entries[key] = entry
        self._payload_bytes += charge
        while self._entries and self.bytes_used > self.max_bytes:
            _, removed = self._entries.popitem(last=False)
            self._payload_bytes -= self._charge(removed)
        if not self._entries:
            self.clear()

    def __delitem__(self, key: K) -> None:
        """Remove a value and release empty index allocation when appropriate."""
        entry = self._entries.pop(key)
        self._payload_bytes -= self._charge(entry)
        if not self._entries:
            self.clear()

    def __len__(self) -> int:
        """Count retained entries."""
        return len(self._entries)

    def __iter__(self) -> Iterator[K]:
        """Snapshot keys so mapping iteration remains valid as reads update LRU order."""
        return iter(tuple(self._entries))

    def clear(self) -> None:
        """Release all payload and index allocation."""
        self._entries.clear()
        self._payload_bytes = 0
