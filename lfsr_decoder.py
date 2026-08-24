
class LFSRDecoder:
    def __init__(self, width: int):
        if width == 16:
            self.seed = 0xffff
            self.taps = 0xd008
        elif width == 8:
            self.seed = 0xff
            self.taps = 0xb8
        else:
            raise ValueError("Supported widths are 8 and 16 bits")

        self.width = width
        self._decode_table = self._build_table()

    def _build_table(self):
        table = {}
        state = self.seed

        for counter in range((1 << self.width) - 1):
            table[state] = counter

            feedback = (state & self.taps).bit_count() & 1
            state >>= 1
            if feedback:
                state |= (1 << (self.width - 1))

        return table

    def decode(self, value: int):
        return self._decode_table.get(int(value))
