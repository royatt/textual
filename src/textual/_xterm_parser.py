from __future__ import annotations

import re
from typing import Any, Callable, Generator, Iterable

from typing_extensions import Final

from . import events, messages
from ._ansi_sequences import ANSI_SEQUENCES_KEYS, IGNORE_SEQUENCE
from ._keyboard_protocol import FUNCTIONAL_KEYS
from ._parser import Awaitable, Parser, TokenCallback
from .keys import KEY_NAME_REPLACEMENTS, Keys, _character_to_key

# When trying to determine whether the current sequence is a supported/valid
# escape sequence, at which length should we give up and consider our search
# to be unsuccessful?
_MAX_SEQUENCE_SEARCH_THRESHOLD = 20

_re_mouse_event = re.compile("^" + re.escape("\x1b[") + r"(<?[\d;]+[mM]|M...)\Z")
_re_terminal_mode_response = re.compile(
    "^" + re.escape("\x1b[") + r"\?(?P<mode_id>\d+);(?P<setting_parameter>\d)\$y"
)

_re_cursor_position = re.compile(r"\x1b\[(?P<row>\d+);(?P<col>\d+)R")

BRACKETED_PASTE_START: Final[str] = "\x1b[200~"
"""Sequence received when a bracketed paste event starts."""
BRACKETED_PASTE_END: Final[str] = "\x1b[201~"
"""Sequence received when a bracketed paste event ends."""
FOCUSIN: Final[str] = "\x1b[I"
"""Sequence received when the terminal receives focus."""
FOCUSOUT: Final[str] = "\x1b[O"
"""Sequence received when focus is lost from the terminal."""

_re_extended_key: Final = re.compile(r"\x1b\[(?:(\d+)(?:;(\d+))?)?([u~ABCDEFHPQRS])")


class XTermParser(Parser[events.Event]):
    _re_sgr_mouse = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

    def __init__(self, more_data: Callable[[], bool], debug: bool = False) -> None:
        self.more_data = more_data
        self.last_x = 0
        self.last_y = 0

        self._debug_log_file = open("keys.log", "at") if debug else None

        super().__init__()

        self.debug_log("---")

    def debug_log(self, *args: Any) -> None:  # pragma: no cover
        if self._debug_log_file is not None:
            self._debug_log_file.write(" ".join(args) + "\n")
            self._debug_log_file.flush()

    def feed(self, data: str) -> Iterable[events.Event]:
        self.debug_log(f"FEED {data!r}")
        return super().feed(data)

    def parse_mouse_code(self, code: str) -> events.Event | None:
        sgr_match = self._re_sgr_mouse.match(code)
        if sgr_match:
            _buttons, _x, _y, state = sgr_match.groups()
            buttons = int(_buttons)
            x = int(_x) - 1
            y = int(_y) - 1
            delta_x = x - self.last_x
            delta_y = y - self.last_y
            self.last_x = x
            self.last_y = y
            event_class: type[events.MouseEvent]

            if buttons & 64:
                event_class = (
                    events.MouseScrollDown if buttons & 1 else events.MouseScrollUp
                )
                button = 0
            else:
                button = (buttons + 1) & 3
                # XTerm events for mouse movement can look like mouse button down events. But if there is no key pressed,
                # it's a mouse move event.
                if buttons & 32 or button == 0:
                    event_class = events.MouseMove
                else:
                    event_class = events.MouseDown if state == "M" else events.MouseUp

            event = event_class(
                x,
                y,
                delta_x,
                delta_y,
                button,
                bool(buttons & 4),
                bool(buttons & 8),
                bool(buttons & 16),
                screen_x=x,
                screen_y=y,
            )
            return event
        return None

    _reissued_sequence_debug_book: Callable[[str], None] | None = None
    """INTERNAL USE ONLY!

    If this property is set to a callable, it will be called *instead* of
    the reissued sequence being emitted as key events.
    """

    def parse(self, _on_token: TokenCallback) -> Generator[Awaitable, str, None]:
        ESC = "\x1b"
        read1 = self.read1
        sequence_to_key_events = self._sequence_to_key_events
        more_data = self.more_data
        paste_buffer: list[str] = []
        bracketed_paste = False
        use_prior_escape = False

        def on_token(token: events.Event) -> None:
            """Hook to log events."""
            self.debug_log(str(token))
            _on_token(token)

        def on_key_token(event: events.Key) -> None:
            """Token callback wrapper for handling keys.

            Args:
                event: The key event to send to the callback.

            This wrapper looks for keys that should be ignored, and filters
            them out, logging the ignored sequence when it does.
            """
            if event.key == Keys.Ignore:
                self.debug_log(f"ignored={event.character!r}")
            else:
                on_token(event)

        def reissue_sequence_as_keys(reissue_sequence: str) -> None:
            if self._reissued_sequence_debug_book is not None:
                self._reissued_sequence_debug_book(reissue_sequence)
                return
            for character in reissue_sequence:
                key_events = sequence_to_key_events(character)
                for event in key_events:
                    if event.key == "escape":
                        event = events.Key("circumflex_accent", "^")
                    on_token(event)

        while not self.is_eof:
            if not bracketed_paste and paste_buffer:
                # We're at the end of the bracketed paste.
                # The paste buffer has content, but the bracketed paste has finished,
                # so we flush the paste buffer. We have to remove the final character
                # since if bracketed paste has come to an end, we'll have added the
                # ESC from the closing bracket, since at that point we didn't know what
                # the full escape code was.
                pasted_text = "".join(paste_buffer[:-1])
                # Note the removal of NUL characters: https://github.com/Textualize/textual/issues/1661
                on_token(events.Paste(pasted_text.replace("\x00", "")))
                paste_buffer.clear()

            character = ESC if use_prior_escape else (yield read1())
            use_prior_escape = False

            if bracketed_paste:
                paste_buffer.append(character)

            self.debug_log(f"character={character!r}")
            if character == ESC:
                # Could be the escape key was pressed OR the start of an escape sequence
                sequence: str = character
                if not bracketed_paste:
                    # TODO: There's nothing left in the buffer at the moment,
                    #  but since we're on an escape, how can we be sure that the
                    #  data that next gets fed to the parser isn't an escape sequence?

                    #  This problem arises when an ESC falls at the end of a chunk.
                    #  We'll be at an escape, but peek_buffer will return an empty
                    #  string because there's nothing in the buffer yet.

                    #  This code makes an assumption that an escape sequence will never be
                    #  "chopped up", so buffers would never contain partial escape sequences.
                    peek_buffer = yield self.peek_buffer()
                    if not peek_buffer:
                        # An escape arrived without any following characters
                        on_token(events.Key("escape", "\x1b"))
                        continue
                    if peek_buffer and peek_buffer[0] == ESC:
                        # There is an escape in the buffer, so ESC ESC has arrived
                        yield read1()
                        on_token(events.Key("escape", "\x1b"))
                        # If there is no further data, it is not part of a sequence,
                        # So we don't need to go in to the loop
                        if len(peek_buffer) == 1 and not more_data():
                            continue

                # Look ahead through the suspected escape sequence for a match
                while True:
                    # If we run into another ESC at this point, then we've failed
                    # to find a match, and should issue everything we've seen within
                    # the suspected sequence as Key events instead.
                    sequence_character = yield read1()
                    new_sequence = sequence + sequence_character

                    threshold_exceeded = len(sequence) > _MAX_SEQUENCE_SEARCH_THRESHOLD
                    found_escape = sequence_character and sequence_character == ESC

                    if threshold_exceeded:
                        # We exceeded the sequence length threshold, so reissue all the
                        # characters in that sequence as key-presses.
                        reissue_sequence_as_keys(new_sequence)
                        break

                    if found_escape:
                        # We've hit an escape, so we need to reissue all the keys
                        # up to but not including it, since this escape could be
                        # part of an upcoming control sequence.
                        use_prior_escape = True
                        reissue_sequence_as_keys(sequence)
                        break

                    sequence = new_sequence

                    self.debug_log(f"sequence={sequence!r}")

                    if sequence == FOCUSIN:
                        on_token(events.AppFocus())
                        break

                    if sequence == FOCUSOUT:
                        on_token(events.AppBlur())
                        break

                    if sequence == BRACKETED_PASTE_START:
                        bracketed_paste = True
                        break

                    if sequence == BRACKETED_PASTE_END:
                        bracketed_paste = False
                        break

                    if not bracketed_paste:
                        # Check cursor position report
                        if (
                            cursor_position_match := _re_cursor_position.match(sequence)
                        ) is not None:
                            row, column = cursor_position_match.groups()
                            # Cursor position report conflicts with f3 key
                            # If it is a keypress, "row" will be 1, so ignore
                            if int(row) != 1:
                                on_token(
                                    events.CursorPosition(
                                        x=int(column) - 1, y=int(row) - 1
                                    )
                                )
                                break

                        # Was it a pressed key event that we received?
                        key_events = list(sequence_to_key_events(sequence))
                        for key_event in key_events:
                            on_key_token(key_event)
                        if key_events:
                            break
                        # Or a mouse event?
                        if (mouse_match := _re_mouse_event.match(sequence)) is not None:
                            mouse_code = mouse_match.group(0)
                            event = self.parse_mouse_code(mouse_code)
                            if event:
                                on_token(event)
                            break

                        # Or a mode report?
                        # (i.e. the terminal saying it supports a mode we requested)
                        if (
                            mode_report_match := _re_terminal_mode_response.match(
                                sequence
                            )
                        ) is not None:
                            if (
                                mode_report_match["mode_id"] == "2026"
                                and int(mode_report_match["setting_parameter"]) > 0
                            ):
                                on_token(messages.TerminalSupportsSynchronizedOutput())
                            break

            else:
                if not bracketed_paste:
                    for event in sequence_to_key_events(character):
                        on_key_token(event)

        if self._debug_log_file is not None:
            self._debug_log_file.close()
            self._debug_log_file = None

    def _sequence_to_key_events(self, sequence: str) -> Iterable[events.Key]:
        """Map a sequence of code points on to a sequence of keys.

        Args:
            sequence: Sequence of code points.

        Returns:
            Keys
        """

        if (match := _re_extended_key.match(sequence)) is not None:
            number, modifiers, end = match.groups()
            number = number or 1
            if not (key := FUNCTIONAL_KEYS.get(f"{number}{end}", "")):
                try:
                    key = _character_to_key(chr(int(number)))
                except Exception:
                    key = chr(int(number))
            key_tokens: list[str] = []
            if modifiers:
                modifier_bits = int(modifiers) - 1
                MODIFIERS = (
                    "shift",
                    "alt",
                    "ctrl",
                    "hyper",
                    "meta",
                    "caps_lock",
                    "num_lock",
                )
                for bit, modifier in zip(range(8), MODIFIERS):
                    if modifier_bits & (1 << bit):
                        key_tokens.append(modifier)
            key_tokens.sort()
            key_tokens.append(key)
            yield events.Key(
                f'{"+".join(key_tokens)}', sequence if len(sequence) == 1 else None
            )
            return

        keys = ANSI_SEQUENCES_KEYS.get(sequence)
        # If we're being asked to ignore the key...
        if keys is IGNORE_SEQUENCE:
            # ...build a special ignore key event, which has the ignore
            # name as the key (that is, the key this sequence is bound
            # to is the ignore key) and the sequence that was ignored as
            # the character.
            yield events.Key(Keys.Ignore, sequence)
            return
        if isinstance(keys, tuple):
            # If the sequence mapped to a tuple, then it's values from the
            # `Keys` enum. Raise key events from what we find in the tuple.
            for key in keys:
                yield events.Key(key.value, sequence if len(sequence) == 1 else None)
            return
        # If keys is a string, the intention is that it's a mapping to a
        # character, which should really be treated as the sequence for the
        # purposes of the next step...
        if isinstance(keys, str):
            sequence = keys
        # If the sequence is a single character, attempt to process it as a
        # key.
        if len(sequence) == 1:
            try:
                if not sequence.isalnum():
                    name = _character_to_key(sequence)
                else:
                    name = sequence
                name = KEY_NAME_REPLACEMENTS.get(name, name)
                yield events.Key(name, sequence)
            except Exception:
                yield events.Key(sequence, sequence)
