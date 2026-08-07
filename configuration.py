from mgpd import MGPDClient


class Configuration:
    def __init__(
        self,
        client: MGPDClient,
        DEFAULT_REGISTERS: dict[int, int],
        AMUX_SIGNALS: dict[str, int],
        REGS_FIELDS: dict[str, list[tuple[int, int, int]]],
        AMUX_MAP: dict[int, tuple[int, int]],
    ):
        self.client = client
        self.default_registers = DEFAULT_REGISTERS
        self.amux_signals = AMUX_SIGNALS
        self.regs_fields = REGS_FIELDS
        self.amux_map = AMUX_MAP

    def set_data(self, name: str, value: int) -> bool:
        if name not in self.regs_fields:
            raise KeyError(f"Unknown register field: {name}")

        # Порядок фрагментов в REGS_FIELDS является значимым:
        # первый элемент списка содержит младшие биты значения,
        # следующий элемент - более старшие биты.
        fields = self.regs_fields[name]
        total_bits = sum(width for _, _, width in fields)
        max_value = (1 << total_bits) - 1

        if not (0 <= value <= max_value):
            raise ValueError(
                f"Value {value} out of range for {name} (0..{max_value})"
            )

        remaining = value

        for addr, shift, width in fields:
            part = remaining & ((1 << width) - 1)
            current = self.client.read_byte(addr)

            if current is None:
                return False

            mask = ((1 << width) - 1) << shift
            new_val = (current & ~mask) | ((part << shift) & mask)

            if new_val != current:
                if not self.client.write_byte(new_val, addr):
                    return False

            remaining >>= width

        return True

    def set_amux(self, signal_name: str) -> bool:
        if signal_name not in self.amux_signals:
            raise KeyError(f"Unknown signal name: {signal_name}")

        signal_num = self.amux_signals[signal_name]
        value = 1 << signal_num

        return self.set_data("TEST_MUX", value)

    def set_default(self) -> bool:
        for addr, value in self.default_registers.items():
            if not self.client.write_byte(value, addr):
                return False

        print(
            f"Default registers loaded: "
            f"{len(self.default_registers)} registers written."
        )
        return True

    def set_ctrl(self, state: int | bool) -> bool:
        """Установить CTRL в статическое состояние 0 или 1."""
        return self.client.set_ctrl(state)

    def set_ctrl_pwm(self, frequency_khz: int, width_ns: int) -> bool:
        """Установить CTRL в ШИМ-режим."""
        return self.client.set_ctrl_pwm(frequency_khz, width_ns)