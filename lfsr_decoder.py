class LFSRDecoder:
    """
    LFSR counter decoder.

    Supports both possible RTL shift directions because the documentation
    defines the feedback polynomial/taps, but not the physical shift
    direction.

    For 16 bit:
        feedback = D[15] ^ D[14] ^ D[12] ^ D[3]

    For 8 bit:
        feedback = D[7] ^ D[5] ^ D[4] ^ D[3]
    """

    def __init__(self, width: int):
        if width == 16:
            self.seed = 0xffff
            self.taps = (15, 14, 12, 3)
        elif width == 8:
            self.seed = 0xff
            self.taps = (7, 5, 4, 3)
        else:
            raise ValueError("Supported widths are 8 and 16 bits")

        self.width = width
        self.mask = (1 << width) - 1

        self._decode_tables = {
            "left": self._build_table("left"),
            "right": self._build_table("right"),
        }

    def _feedback(self, state):
        value = 0
        for bit in self.taps:
            value ^= (state >> bit) & 1
        return value

    def _next_state(self, state, direction):
        feedback = self._feedback(state)

        if direction == "left":
            # shift toward MSB, feedback enters LSB
            return ((state << 1) & self.mask) | feedback

        # shift toward LSB, feedback enters MSB
        return (state >> 1) | (feedback << (self.width - 1))

    def _build_table(self, direction):
        table = {}
        state = self.seed
        length = (1 << self.width) - 1

        for counter in range(length):
            table[state] = counter
            state = self._next_state(state, direction)

        return table

    def decode(self, value: int, direction: str = "auto"):
        """Decode one raw LFSR state.

        ``auto`` preserves the historical GUI behavior: try the left-shift
        interpretation first and then the right-shift table. Scientific
        measurements should pass the physically verified direction explicitly,
        because every non-zero state exists in both maximal-length sequences and
        the shift direction cannot be inferred from one raw word.
        """
        value = int(value)

        if direction not in ("auto", "left", "right"):
            raise ValueError("direction must be auto, left or right")

        if direction in ("left", "right"):
            return self._decode_tables[direction].get(value)

        result = self._decode_tables["left"].get(value)
        if result is not None:
            return result

        result = self._decode_tables["right"].get(value)
        return result
