"""Read CLI output as a user sees it, not as the terminal encodes it.

Rich decorates what it prints, and two of those decorations break naive
substring assertions:

* **Wrapping.** A long line is broken to the console width, so a URL or a
  sentence arrives with newlines inside it.
* **Highlighting.** Rich styles punctuation independently, so
  ``flights[browser]`` is emitted as ``"flights`` + ``[`` + ``browser`` +
  ``]`` with a colour escape between every part. The literal string is
  therefore *not* a substring of the output even though the user plainly
  sees it.

The second only bites when colour is on. Locally pytest captures a
non-tty and Rich stays plain, so assertions pass; CI sets
``FORCE_COLOR: 1`` and they fail. That divergence hid five broken
assertions in this suite until CI ran the directory for the first time.

Assert against :func:`plain` rather than raw output, and a test then
checks what was communicated instead of how it was painted.
"""

from __future__ import annotations

import re

#: CSI SGR sequences — the colour/style codes Rich emits.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Return ``text`` with styling removed and wrapping undone.

    Args:
        text: Captured stdout, as ``CliRunner`` or ``capsys`` returns it.

    Returns:
        The same text with ANSI style escapes stripped and newlines
        removed, so a message split across lines matches whole.

    """
    return _ANSI.sub("", text).replace("\n", "")


def collapsed(text: str) -> str:
    """Return :func:`plain` output with runs of whitespace collapsed to one.

    For matching a sentence whose internal spacing depends on where the
    console happened to wrap it.

    Args:
        text: Captured stdout.

    Returns:
        Unstyled text with each whitespace run reduced to a single space.

    """
    return " ".join(_ANSI.sub("", text).split())
